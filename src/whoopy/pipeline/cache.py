"""Content-addressed, integrity-checked cache for synthesized speech segments."""

from __future__ import annotations

import hashlib
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from whoopy.audio.models import PcmAudio
from whoopy.audio.quality import pcm_integrity_error
from whoopy.audio.synthesis import SpeechSynthesizer

CACHE_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CACHE_METADATA_FILENAME = "metadata.json"
CACHE_AUDIO_FILENAME = "audio.pcm"


class SegmentCacheError(RuntimeError):
    """Raised when a valid cache entry cannot be persisted."""


class SegmentCacheMetadata(BaseModel):
    """Integrity and provenance information committed after cached PCM."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    synthesis_input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    synthesizer_identity: str = Field(min_length=1)
    sample_rate: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    byte_count: int = Field(gt=0)
    pcm_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime


class CacheStats(BaseModel):
    """Small machine-readable summary used by the CLI and tests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    entries: int = Field(ge=0)
    valid_entries: int = Field(ge=0)
    corrupt_entries: int = Field(ge=0)
    audio_bytes: int = Field(ge=0)


@dataclass(frozen=True)
class CachedSegment:
    """One verified cache hit."""

    audio: PcmAudio
    metadata: SegmentCacheMetadata


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class SegmentCache:
    """Store verified PCM under a key derived only from synthesis inputs."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def load(
        self,
        cache_key: str,
        *,
        expected_synthesizer: SpeechSynthesizer | None = None,
    ) -> CachedSegment | None:
        """Return a verified hit; missing or corrupt entries behave as misses."""

        entry_directory = self.entry_directory(cache_key)
        metadata_path = entry_directory / CACHE_METADATA_FILENAME
        audio_path = entry_directory / CACHE_AUDIO_FILENAME
        try:
            metadata = SegmentCacheMetadata.model_validate_json(
                metadata_path.read_text(encoding="utf-8")
            )
            pcm_bytes = audio_path.read_bytes()
        except (FileNotFoundError, OSError, ValidationError):
            return None

        if metadata.cache_key != cache_key:
            return None
        if metadata.synthesis_input_sha256 != cache_key:
            return None
        if expected_synthesizer is not None:
            if metadata.synthesizer_identity != expected_synthesizer.cache_identity:
                return None
            if metadata.sample_rate != expected_synthesizer.sample_rate:
                return None
        if len(pcm_bytes) != metadata.byte_count:
            return None
        if _sha256(pcm_bytes) != metadata.pcm_sha256:
            return None

        try:
            audio = PcmAudio(pcm_s16le=pcm_bytes, sample_rate=metadata.sample_rate)
        except ValueError:
            return None
        if audio.frame_count != metadata.frame_count:
            return None
        if pcm_integrity_error(audio) is not None:
            return None
        return CachedSegment(audio=audio, metadata=metadata)

    def store(
        self,
        cache_key: str,
        audio: PcmAudio,
        *,
        synthesis_inputs: bytes,
        synthesizer_identity: str,
        created_at: datetime | None = None,
    ) -> SegmentCacheMetadata:
        """Atomically commit PCM first and metadata last as the validity marker."""

        self._validate_key(cache_key)
        integrity_error = pcm_integrity_error(audio)
        if integrity_error is not None:
            raise SegmentCacheError(f"Refusing to cache invalid PCM: {integrity_error}")
        input_digest = _sha256(synthesis_inputs)
        if input_digest != cache_key:
            raise SegmentCacheError("Cache key does not match the canonical synthesis inputs")

        metadata = SegmentCacheMetadata(
            cache_key=cache_key,
            synthesis_input_sha256=input_digest,
            synthesizer_identity=synthesizer_identity,
            sample_rate=audio.sample_rate,
            frame_count=audio.frame_count,
            byte_count=len(audio.pcm_s16le),
            pcm_sha256=_sha256(audio.pcm_s16le),
            created_at=created_at or datetime.now(UTC),
        )
        entry_directory = self.entry_directory(cache_key)
        try:
            entry_directory.mkdir(parents=True, exist_ok=True)
            self._write_bytes(entry_directory / CACHE_AUDIO_FILENAME, audio.pcm_s16le)
            self._write_bytes(
                entry_directory / CACHE_METADATA_FILENAME,
                (metadata.model_dump_json(indent=2) + "\n").encode("utf-8"),
            )
        except OSError as error:
            raise SegmentCacheError(f"Could not store segment cache entry: {error}") from error
        return metadata

    def stats(self) -> CacheStats:
        """Inspect cache entries without deleting or repairing anything."""

        entries = 0
        valid_entries = 0
        audio_bytes = 0
        if not self.root.exists():
            return CacheStats(entries=0, valid_entries=0, corrupt_entries=0, audio_bytes=0)

        for metadata_path in self.root.rglob(CACHE_METADATA_FILENAME):
            entries += 1
            entry_directory = metadata_path.parent
            audio_path = entry_directory / CACHE_AUDIO_FILENAME
            with suppress(OSError):
                audio_bytes += audio_path.stat().st_size
            cache_key = entry_directory.name
            if CACHE_KEY_PATTERN.fullmatch(cache_key) and self.load(cache_key) is not None:
                valid_entries += 1
        return CacheStats(
            entries=entries,
            valid_entries=valid_entries,
            corrupt_entries=entries - valid_entries,
            audio_bytes=audio_bytes,
        )

    def entry_directory(self, cache_key: str) -> Path:
        """Shard entries by their first two hex characters."""

        self._validate_key(cache_key)
        return self.root / cache_key[:2] / cache_key

    @staticmethod
    def _validate_key(cache_key: str) -> None:
        if CACHE_KEY_PATTERN.fullmatch(cache_key) is None:
            raise SegmentCacheError(f"Invalid segment cache key: {cache_key}")

    @staticmethod
    def _write_bytes(path: Path, payload: bytes) -> None:
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.write_bytes(payload)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
