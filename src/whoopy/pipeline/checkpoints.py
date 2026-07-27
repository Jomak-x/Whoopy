"""Per-run speech checkpoints used to resume after interruption or failure."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError, model_validator

from whoopy.audio.models import PcmAudio
from whoopy.audio.quality import pcm_integrity_error
from whoopy.pipeline.runs import RunNotFoundError, RunStore

SEGMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
CHECKPOINT_FILENAME = "checkpoint.json"
SEGMENT_AUDIO_FILENAME = "audio.pcm"


class CheckpointStatus(StrEnum):
    """Durable state of one speech-segment synthesis operation."""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class FailureKind(StrEnum):
    """Why an individual synthesis attempt failed."""

    TRANSIENT = "transient"
    FATAL = "fatal"
    QUALITY = "quality"
    UNEXPECTED = "unexpected"


class SegmentAttemptFailure(BaseModel):
    """One retained failure reason, even when a later retry succeeds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: int = Field(gt=0)
    kind: FailureKind
    error: str = Field(min_length=1)
    occurred_at: AwareDatetime


class SegmentCheckpoint(BaseModel):
    """Auditable progress record for one speech segment in one run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    run_id: UUID
    segment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    cache_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: CheckpointStatus
    attempt_count: int = Field(ge=0)
    cache_hit: bool = False
    sample_rate: int | None = Field(default=None, gt=0)
    frame_count: int | None = Field(default=None, gt=0)
    byte_count: int | None = Field(default=None, gt=0)
    pcm_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    started_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None = None
    failures: list[SegmentAttemptFailure] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_status_metadata(self) -> SegmentCheckpoint:
        """Make the checkpoint file a trustworthy commit marker."""

        audio_fields = (self.sample_rate, self.frame_count, self.byte_count, self.pcm_sha256)
        if self.status is CheckpointStatus.COMPLETED:
            if any(value is None for value in audio_fields) or self.completed_at is None:
                raise ValueError("a completed segment checkpoint requires verified audio metadata")
        else:
            if any(value is not None for value in audio_fields) or self.completed_at is not None:
                raise ValueError("only a completed segment checkpoint may reference audio")
        if self.status is CheckpointStatus.FAILED and not self.failures:
            raise ValueError("a failed segment checkpoint requires a failure reason")
        if self.attempt_count < len(self.failures):
            raise ValueError("failure history cannot exceed the attempt count")
        return self


@dataclass(frozen=True)
class CheckpointedSegment:
    """One completed checkpoint whose PCM digest has been reverified."""

    audio: PcmAudio
    checkpoint: SegmentCheckpoint


class SegmentCheckpointStore:
    """Persist speech checkpoints beneath a run without trusting path input."""

    def __init__(self, run_store: RunStore) -> None:
        self.run_store = run_store

    def load_optional(
        self,
        run_id: UUID | str,
        segment_id: str,
    ) -> SegmentCheckpoint | None:
        path = self.checkpoint_path(run_id, segment_id)
        try:
            return SegmentCheckpoint.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValidationError):
            return None

    def load_completed(
        self,
        run_id: UUID | str,
        segment_id: str,
        *,
        cache_key: str,
    ) -> CheckpointedSegment | None:
        """Return only a completed checkpoint with matching, healthy PCM."""

        checkpoint = self.load_optional(run_id, segment_id)
        if (
            checkpoint is None
            or checkpoint.status is not CheckpointStatus.COMPLETED
            or checkpoint.cache_key != cache_key
        ):
            return None
        try:
            pcm_bytes = self.audio_path(run_id, segment_id).read_bytes()
        except OSError:
            return None
        if len(pcm_bytes) != checkpoint.byte_count:
            return None
        if hashlib.sha256(pcm_bytes).hexdigest() != checkpoint.pcm_sha256:
            return None
        try:
            audio = PcmAudio(pcm_s16le=pcm_bytes, sample_rate=checkpoint.sample_rate or 0)
        except ValueError:
            return None
        if audio.frame_count != checkpoint.frame_count:
            return None
        if pcm_integrity_error(audio) is not None:
            return None
        return CheckpointedSegment(audio=audio, checkpoint=checkpoint)

    def save(
        self,
        checkpoint: SegmentCheckpoint,
        *,
        audio: PcmAudio | None = None,
    ) -> None:
        """Write PCM before the completed checkpoint that commits it."""

        run_directory = self.run_store.run_directory(checkpoint.run_id)
        if not run_directory.is_dir():
            raise RunNotFoundError(f"Run directory not found: {run_directory}")
        directory = self.segment_directory(checkpoint.run_id, checkpoint.segment_id)
        directory.mkdir(parents=True, exist_ok=True)
        if checkpoint.status is CheckpointStatus.COMPLETED:
            if audio is None:
                raise ValueError("completed checkpoint save requires audio")
            if hashlib.sha256(audio.pcm_s16le).hexdigest() != checkpoint.pcm_sha256:
                raise ValueError("checkpoint PCM digest does not match supplied audio")
            self._write_bytes(directory / SEGMENT_AUDIO_FILENAME, audio.pcm_s16le)
        elif audio is not None:
            raise ValueError("non-completed checkpoint cannot save audio")
        self._write_bytes(
            directory / CHECKPOINT_FILENAME,
            (checkpoint.model_dump_json(indent=2) + "\n").encode("utf-8"),
        )

    def segment_directory(self, run_id: UUID | str, segment_id: str) -> Path:
        self._validate_segment_id(segment_id)
        return self.run_store.run_directory(run_id) / "segments" / segment_id

    def checkpoint_path(self, run_id: UUID | str, segment_id: str) -> Path:
        return self.segment_directory(run_id, segment_id) / CHECKPOINT_FILENAME

    def audio_path(self, run_id: UUID | str, segment_id: str) -> Path:
        return self.segment_directory(run_id, segment_id) / SEGMENT_AUDIO_FILENAME

    @staticmethod
    def completed_checkpoint(
        *,
        run_id: UUID,
        segment_id: str,
        cache_key: str,
        audio: PcmAudio,
        attempt_count: int,
        cache_hit: bool,
        started_at: datetime,
        completed_at: datetime,
        failures: list[SegmentAttemptFailure] | None = None,
    ) -> SegmentCheckpoint:
        return SegmentCheckpoint(
            run_id=run_id,
            segment_id=segment_id,
            cache_key=cache_key,
            status=CheckpointStatus.COMPLETED,
            attempt_count=attempt_count,
            cache_hit=cache_hit,
            sample_rate=audio.sample_rate,
            frame_count=audio.frame_count,
            byte_count=len(audio.pcm_s16le),
            pcm_sha256=hashlib.sha256(audio.pcm_s16le).hexdigest(),
            started_at=started_at,
            updated_at=completed_at,
            completed_at=completed_at,
            failures=failures or [],
        )

    @staticmethod
    def _validate_segment_id(segment_id: str) -> None:
        if SEGMENT_ID_PATTERN.fullmatch(segment_id) is None:
            raise ValueError(f"Invalid segment ID: {segment_id}")

    @staticmethod
    def _write_bytes(path: Path, payload: bytes) -> None:
        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.write_bytes(payload)
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)
