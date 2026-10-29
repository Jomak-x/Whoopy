"""Safe, revisioned storage for user-consented local voice references."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from whoopy.model_packs.operations import ModelPackOperationError, safe_managed_path

_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_TARGET_RATE = 24_000
_MAX_SOURCE_BYTES = 100 * 1024 * 1024
_MAX_TRANSCRIPT_CHARACTERS = 10_000


class VoiceReferenceError(ValueError):
    """A voice reference was invalid or an operation was unsafe."""


class VoiceConsent(BaseModel):
    """Explicit, durable rights attestation for local voice cloning."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    confirmed: Literal[True]
    scope: Literal["local_voice_cloning_experiment_only"]
    source_kind: Literal["user_provided", "project_generated"]
    rights_attestation: Literal["I own this voice or have permission to clone it."]
    recorded_at: AwareDatetime


class VoiceReferencePreview(BaseModel):
    """Bounded range a UI can audition without interpreting the WAV."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    audio_path: Path
    start_ms: Literal[0] = 0
    duration_ms: int = Field(gt=0)
    sample_rate: Literal[24_000] = 24_000
    channels: Literal[1] = 1


class VoiceReferenceRevision(BaseModel):
    """Immutable evidence for one imported or replacement reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    reference_id: str
    display_name: str = Field(min_length=1, max_length=120)
    revision: int = Field(ge=1)
    revision_id: str
    parent_revision_id: str | None
    created_at: AwareDatetime
    audio_path: Path
    transcript_path: Path
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_ms: int = Field(ge=1_000, le=60_000)
    sample_rate: Literal[24_000] = 24_000
    channels: Literal[1] = 1
    sample_width_bytes: Literal[2] = 2
    source_sample_rate: int = Field(ge=8_000, le=192_000)
    source_channels: Literal[1, 2]
    trim_start_ms: int = Field(ge=0)
    trim_end_ms: int = Field(gt=0)
    consent: VoiceConsent
    preview: VoiceReferencePreview


class VoiceReferenceSummary(BaseModel):
    """One active reference returned by list operations."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: str
    display_name: str
    active_revision: int
    revision_id: str
    duration_ms: int
    sample_rate: int
    audio_sha256: str
    preview: VoiceReferencePreview


class VoiceReferenceTrashReceipt(BaseModel):
    """Receipt for a recoverable reference deletion."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    trash_id: str
    reference_id: str
    deleted_at: AwareDatetime
    original_path: Path
    payload_path: Path


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, document: dict[str, Any], *, root: Path) -> None:
    try:
        safe_managed_path(root, path, label="voice-reference JSON")
        safe_managed_path(root, path.parent, label="voice-reference directory")
    except ModelPackOperationError as error:
        raise VoiceReferenceError(str(error)) from error
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(document, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _source_bytes(source: Path) -> bytes:
    if source.is_symlink():
        raise VoiceReferenceError("voice-reference source may not be a symlink")
    try:
        size = source.stat().st_size
    except OSError as error:
        raise VoiceReferenceError(f"could not inspect voice-reference source: {error}") from error
    if not source.is_file():
        raise VoiceReferenceError("voice-reference source must be a regular WAV file")
    if size <= 0 or size > _MAX_SOURCE_BYTES:
        raise VoiceReferenceError("voice-reference WAV must be between 1 byte and 100 MiB")
    try:
        return source.read_bytes()
    except OSError as error:
        raise VoiceReferenceError(f"could not read voice-reference source: {error}") from error


def _canonical_wav(
    source: Path,
    *,
    trim_start_ms: int,
    trim_end_ms: int | None,
) -> tuple[bytes, int, Literal[1, 2], int, int]:
    try:
        with wave.open(str(source), "rb") as reader:
            channels = reader.getnchannels()
            sample_width = reader.getsampwidth()
            source_rate = reader.getframerate()
            frame_count = reader.getnframes()
            compression = reader.getcomptype()
            pcm = reader.readframes(frame_count)
    except (EOFError, OSError, wave.Error) as error:
        raise VoiceReferenceError(f"invalid WAV container: {error}") from error
    if channels not in (1, 2):
        raise VoiceReferenceError("voice-reference WAV must be mono or stereo")
    validated_channels = cast(Literal[1, 2], channels)
    if sample_width != 2 or compression != "NONE":
        raise VoiceReferenceError("voice-reference WAV must contain uncompressed 16-bit PCM")
    if not 8_000 <= source_rate <= 192_000:
        raise VoiceReferenceError("voice-reference sample rate must be 8-192 kHz")
    if len(pcm) != frame_count * channels * sample_width:
        raise VoiceReferenceError("voice-reference WAV contains truncated PCM")
    source_duration_ms = frame_count * 1_000 // source_rate
    effective_end_ms = source_duration_ms if trim_end_ms is None else trim_end_ms
    if trim_start_ms < 0 or effective_end_ms <= trim_start_ms:
        raise VoiceReferenceError("trim range must have a positive duration")
    if effective_end_ms > source_duration_ms:
        raise VoiceReferenceError("trim end exceeds the WAV duration")
    start_frame = trim_start_ms * source_rate // 1_000
    end_frame = min(frame_count, effective_end_ms * source_rate // 1_000)
    if end_frame <= start_frame:
        raise VoiceReferenceError("trim range contains no audio frames")

    samples = array("h")
    samples.frombytes(pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    if channels == 2:
        mono = array(
            "h",
            (
                max(-32_768, min(32_767, (samples[index] + samples[index + 1]) // 2))
                for index in range(0, len(samples), 2)
            ),
        )
    else:
        mono = samples
    trimmed = mono[start_frame:end_frame]
    output_frames = max(1, round(len(trimmed) * _TARGET_RATE / source_rate))
    resampled = array("h")
    for output_index in range(output_frames):
        position_numerator = output_index * source_rate
        left = min(len(trimmed) - 1, position_numerator // _TARGET_RATE)
        right = min(len(trimmed) - 1, left + 1)
        remainder = position_numerator % _TARGET_RATE
        value = (
            trimmed[left] * (_TARGET_RATE - remainder) + trimmed[right] * remainder
        ) // _TARGET_RATE
        resampled.append(max(-32_768, min(32_767, value)))
    duration_ms = len(resampled) * 1_000 // _TARGET_RATE
    if not 1_000 <= duration_ms <= 60_000:
        raise VoiceReferenceError("trimmed voice reference must be 1-60 seconds long")
    if not any(abs(sample) >= 32 for sample in resampled):
        raise VoiceReferenceError("voice-reference WAV has no audible signal")

    with tempfile.SpooledTemporaryFile(max_size=4 * 1024 * 1024) as output:
        with wave.open(output, "wb") as writer:
            writer.setnchannels(1)
            writer.setsampwidth(2)
            writer.setframerate(_TARGET_RATE)
            writer.writeframes(resampled.tobytes())
        output.seek(0)
        canonical = output.read()
    if not canonical:
        raise VoiceReferenceError("could not create canonical voice-reference WAV")
    return canonical, source_rate, validated_channels, effective_end_ms, duration_ms


class VoiceReferenceManager:
    """Own immutable revisions and recoverable deletion under one managed root."""

    def __init__(self, root: Path) -> None:
        self.root = Path(os.path.abspath(root))
        try:
            safe_managed_path(self.root, self.root, label="voice-reference root")
        except ModelPackOperationError as error:
            raise VoiceReferenceError(str(error)) from error

    @staticmethod
    def delete_confirmation(reference_id: str) -> str:
        return f"delete voice reference {reference_id}"

    @staticmethod
    def restore_confirmation(trash_id: str) -> str:
        return f"restore voice reference {trash_id}"

    def _id(self, reference_id: str) -> str:
        if _SAFE_ID.fullmatch(reference_id) is None:
            raise VoiceReferenceError(f"invalid voice reference ID: {reference_id!r}")
        return reference_id

    def _reference_directory(self, reference_id: str) -> Path:
        candidate = self.root / "references" / self._id(reference_id)
        try:
            return safe_managed_path(self.root, candidate, label="voice-reference path")
        except ModelPackOperationError as error:
            raise VoiceReferenceError(str(error)) from error

    def import_wav(
        self,
        *,
        reference_id: str,
        display_name: str,
        source_wav: Path,
        transcript: str,
        consent: VoiceConsent,
        trim_start_ms: int = 0,
        trim_end_ms: int | None = None,
    ) -> VoiceReferenceRevision:
        """Create the first immutable revision of a new reference."""

        if self._reference_directory(reference_id).exists():
            raise VoiceReferenceError(f"voice reference {reference_id} already exists")
        return self._create_revision(
            reference_id=reference_id,
            display_name=display_name,
            source_wav=source_wav,
            transcript=transcript,
            consent=consent,
            trim_start_ms=trim_start_ms,
            trim_end_ms=trim_end_ms,
            parent=None,
        )

    def replace(
        self,
        *,
        reference_id: str,
        display_name: str,
        source_wav: Path,
        transcript: str,
        consent: VoiceConsent,
        trim_start_ms: int = 0,
        trim_end_ms: int | None = None,
    ) -> VoiceReferenceRevision:
        """Activate a new immutable revision without overwriting prior evidence."""

        parent = self.load(reference_id)
        return self._create_revision(
            reference_id=reference_id,
            display_name=display_name,
            source_wav=source_wav,
            transcript=transcript,
            consent=consent,
            trim_start_ms=trim_start_ms,
            trim_end_ms=trim_end_ms,
            parent=parent,
        )

    def _create_revision(
        self,
        *,
        reference_id: str,
        display_name: str,
        source_wav: Path,
        transcript: str,
        consent: VoiceConsent,
        trim_start_ms: int,
        trim_end_ms: int | None,
        parent: VoiceReferenceRevision | None,
    ) -> VoiceReferenceRevision:
        reference_id = self._id(reference_id)
        display_name = " ".join(display_name.split())
        transcript = transcript.strip()
        if not display_name or len(display_name) > 120:
            raise VoiceReferenceError("display name must contain 1-120 characters")
        if not transcript or len(transcript) > _MAX_TRANSCRIPT_CHARACTERS:
            raise VoiceReferenceError("transcript must contain 1-10,000 characters")
        source_payload = _source_bytes(source_wav)
        canonical, source_rate, source_channels, effective_end_ms, duration_ms = _canonical_wav(
            source_wav,
            trim_start_ms=trim_start_ms,
            trim_end_ms=trim_end_ms,
        )
        transcript_payload = (transcript + "\n").encode("utf-8")
        revision = 1 if parent is None else parent.revision + 1
        revision_id = f"r{revision:06d}-{_sha256_bytes(canonical)[:12]}"
        reference_directory = self._reference_directory(reference_id)
        revisions_directory = reference_directory / "revisions"
        destination = revisions_directory / f"{revision:06d}"
        if destination.exists():
            raise VoiceReferenceError(f"voice reference revision {revision} already exists")
        revisions_directory.mkdir(parents=True, exist_ok=True)
        staging = Path(tempfile.mkdtemp(prefix=".revision-", dir=revisions_directory))
        try:
            (staging / "reference.wav").write_bytes(canonical)
            (staging / "transcript.txt").write_bytes(transcript_payload)
            relative = destination.relative_to(self.root)
            audio_path = relative / "reference.wav"
            transcript_path = relative / "transcript.txt"
            preview = VoiceReferencePreview(
                audio_path=audio_path,
                duration_ms=min(duration_ms, 10_000),
            )
            record = VoiceReferenceRevision(
                reference_id=reference_id,
                display_name=display_name,
                revision=revision,
                revision_id=revision_id,
                parent_revision_id=parent.revision_id if parent is not None else None,
                created_at=datetime.now(UTC),
                audio_path=audio_path,
                transcript_path=transcript_path,
                audio_sha256=_sha256_bytes(canonical),
                transcript_sha256=_sha256_bytes(transcript_payload),
                source_sha256=_sha256_bytes(source_payload),
                duration_ms=duration_ms,
                source_sample_rate=source_rate,
                source_channels=source_channels,
                trim_start_ms=trim_start_ms,
                trim_end_ms=effective_end_ms,
                consent=consent,
                preview=preview,
            )
            (staging / "manifest.json").write_text(
                record.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            staging.replace(destination)
            _atomic_json(
                reference_directory / "active.json",
                {
                    "schema_version": 1,
                    "active_revision": revision,
                    "revision_id": revision_id,
                },
                root=self.root,
            )
            return record
        except BaseException:
            if staging.exists():
                for child in staging.iterdir():
                    child.unlink(missing_ok=True)
                staging.rmdir()
            raise

    def load(self, reference_id: str) -> VoiceReferenceRevision:
        directory = self._reference_directory(reference_id)
        active_path = directory / "active.json"
        try:
            active = json.loads(active_path.read_text(encoding="utf-8"))
            revision = int(active["active_revision"])
        except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError) as error:
            raise VoiceReferenceError(
                f"could not load voice reference {reference_id}: {error}"
            ) from error
        return self.load_revision(reference_id, revision)

    def load_revision(self, reference_id: str, revision: int) -> VoiceReferenceRevision:
        """Load one immutable revision rather than silently following the active pointer."""

        if revision < 1:
            raise VoiceReferenceError("voice-reference revision must be positive")
        directory = self._reference_directory(reference_id)
        manifest_path = directory / "revisions" / f"{revision:06d}" / "manifest.json"
        try:
            safe_managed_path(self.root, manifest_path, label="voice-reference manifest")
            record = VoiceReferenceRevision.model_validate_json(
                manifest_path.read_text(encoding="utf-8")
            )
        except (ModelPackOperationError, OSError, ValueError) as error:
            raise VoiceReferenceError(
                f"could not load voice reference {reference_id} revision {revision}: {error}"
            ) from error
        if record.reference_id != reference_id or record.revision != revision:
            raise VoiceReferenceError("voice-reference revision manifest identity does not match")
        for relative, expected, label in (
            (record.audio_path, record.audio_sha256, "audio"),
            (record.transcript_path, record.transcript_sha256, "transcript"),
        ):
            try:
                path = safe_managed_path(self.root, self.root / relative, label=label)
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except (ModelPackOperationError, OSError) as error:
                raise VoiceReferenceError(
                    f"could not verify voice-reference {label}: {error}"
                ) from error
            if actual != expected:
                raise VoiceReferenceError(f"voice-reference {label} hash does not match manifest")
        return record

    def list(self) -> tuple[VoiceReferenceSummary, ...]:
        references = self.root / "references"
        if not references.exists():
            return ()
        try:
            safe_managed_path(self.root, references, label="voice-reference collection")
        except ModelPackOperationError as error:
            raise VoiceReferenceError(str(error)) from error
        summaries: list[VoiceReferenceSummary] = []
        for path in sorted(references.iterdir(), key=lambda item: item.name):
            if path.is_symlink() or not path.is_dir():
                raise VoiceReferenceError(f"unsafe entry in voice-reference collection: {path}")
            record = self.load(path.name)
            summaries.append(
                VoiceReferenceSummary(
                    reference_id=record.reference_id,
                    display_name=record.display_name,
                    active_revision=record.revision,
                    revision_id=record.revision_id,
                    duration_ms=record.duration_ms,
                    sample_rate=record.sample_rate,
                    audio_sha256=record.audio_sha256,
                    preview=record.preview,
                )
            )
        return tuple(summaries)

    def delete(self, reference_id: str, *, confirmation: str) -> VoiceReferenceTrashReceipt:
        reference_id = self._id(reference_id)
        if confirmation != self.delete_confirmation(reference_id):
            raise VoiceReferenceError("voice-reference deletion confirmation did not match")
        source = self._reference_directory(reference_id)
        if not source.is_dir() or source.is_symlink():
            raise VoiceReferenceError(f"voice reference {reference_id} does not exist safely")
        trash_id = f"{datetime.now(UTC).strftime('%Y%m%dt%H%M%Sz')}-{uuid.uuid4().hex[:12]}"
        entry = self.root / ".trash" / trash_id
        payload = entry / "reference"
        try:
            safe_managed_path(self.root, payload, label="voice-reference trash")
        except ModelPackOperationError as error:
            raise VoiceReferenceError(str(error)) from error
        receipt = VoiceReferenceTrashReceipt(
            trash_id=trash_id,
            reference_id=reference_id,
            deleted_at=datetime.now(UTC),
            original_path=source.relative_to(self.root),
            payload_path=payload.relative_to(self.root),
        )
        entry.mkdir(parents=True, exist_ok=False)
        _atomic_json(entry / "trash.json", receipt.model_dump(mode="json"), root=self.root)
        source.replace(payload)
        return receipt

    def restore(self, trash_id: str, *, confirmation: str) -> VoiceReferenceRevision:
        if _SAFE_ID.fullmatch(trash_id) is None:
            raise VoiceReferenceError(f"invalid trash ID: {trash_id!r}")
        if confirmation != self.restore_confirmation(trash_id):
            raise VoiceReferenceError("voice-reference restore confirmation did not match")
        entry = self.root / ".trash" / trash_id
        try:
            safe_managed_path(self.root, entry, label="voice-reference trash entry")
            receipt = VoiceReferenceTrashReceipt.model_validate_json(
                (entry / "trash.json").read_text(encoding="utf-8")
            )
        except (ModelPackOperationError, OSError, ValueError) as error:
            raise VoiceReferenceError(
                f"could not load voice-reference trash receipt: {error}"
            ) from error
        expected_original = Path("references") / receipt.reference_id
        expected_payload = Path(".trash") / trash_id / "reference"
        if (
            receipt.trash_id != trash_id
            or receipt.original_path != expected_original
            or receipt.payload_path != expected_payload
        ):
            raise VoiceReferenceError(
                "voice-reference trash receipt targets do not match its entry"
            )
        destination = self._reference_directory(receipt.reference_id)
        payload = self.root / receipt.payload_path
        if destination.exists():
            raise VoiceReferenceError("cannot restore over an existing voice reference")
        try:
            safe_managed_path(self.root, payload, label="voice-reference trash payload")
        except ModelPackOperationError as error:
            raise VoiceReferenceError(str(error)) from error
        if not payload.is_dir() or payload.is_symlink():
            raise VoiceReferenceError("voice-reference trash payload is missing or unsafe")
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload.replace(destination)
        (entry / "trash.json").unlink()
        entry.rmdir()
        return self.load(receipt.reference_id)
