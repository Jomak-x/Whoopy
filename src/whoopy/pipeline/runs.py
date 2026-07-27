"""Durable local run records and filesystem artifact storage."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from whoopy.audio import AudioManifest, AudioQualityReport
from whoopy.timeline import Timeline

RUN_RECORD_FILENAME: Literal["run.json"] = "run.json"
TIMELINE_FILENAME: Literal["timeline.json"] = "timeline.json"
AUDIO_FILENAME: Literal["narration.wav"] = "narration.wav"
AUDIO_MANIFEST_FILENAME: Literal["audio-manifest.json"] = "audio-manifest.json"
QUALITY_FILENAME: Literal["quality.json"] = "quality.json"
PromptText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
]


class RunStoreError(RuntimeError):
    """Base error for local run persistence."""


class RunNotFoundError(RunStoreError):
    """Raised when a requested run directory or record does not exist."""


class InvalidRunIdError(RunStoreError):
    """Raised before an unsafe or malformed value can become a filesystem path."""


class RunStatus(StrEnum):
    """Small Phase 1 lifecycle understood by both control plane and worker."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class RunRecovery(BaseModel):
    """Phase 3 progress and reuse counters saved in the run record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    process_attempts: int = Field(ge=0)
    resume_count: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    cache_misses: int = Field(ge=0)
    checkpoint_reuses: int = Field(ge=0)
    speech_segments_total: int = Field(ge=0)
    speech_segments_completed: int = Field(ge=0)
    failed_segment_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )

    @model_validator(mode="after")
    def validate_progress(self) -> RunRecovery:
        if self.speech_segments_completed > self.speech_segments_total:
            raise ValueError("completed speech segments cannot exceed the total")
        if self.resume_count > self.process_attempts:
            raise ValueError("resume count cannot exceed process attempts")
        return self


class RunRecord(BaseModel):
    """The durable control-plane record for one local generation request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2, 3] = 3
    run_id: UUID
    status: RunStatus
    prompt: PromptText
    created_at: AwareDatetime
    updated_at: AwareDatetime
    timeline_artifact: Literal["timeline.json"] | None = None
    audio_artifact: Literal["narration.wav"] | None = None
    audio_manifest_artifact: Literal["audio-manifest.json"] | None = None
    quality_artifact: Literal["quality.json"] | None = None
    recovery: RunRecovery | None = None
    error: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_status_fields(self) -> RunRecord:
        """Prevent contradictory records from being written to disk."""

        artifacts = (
            self.timeline_artifact,
            self.audio_artifact,
            self.audio_manifest_artifact,
            self.quality_artifact,
        )
        if self.status is RunStatus.COMPLETED:
            if self.timeline_artifact is None:
                raise ValueError("a completed run must reference its timeline artifact")
            if self.schema_version in (2, 3) and any(artifact is None for artifact in artifacts):
                raise ValueError(
                    "a completed run schema v2 or v3 must reference every audio artifact"
                )
        elif any(artifact is not None for artifact in artifacts):
            raise ValueError("only a completed run may reference artifacts")
        if self.schema_version == 1 and any(artifact is not None for artifact in artifacts[1:]):
            raise ValueError("run schema v1 does not support audio artifacts")
        if self.schema_version in (1, 2):
            if self.recovery is not None:
                raise ValueError("run schema v1 and v2 do not support recovery metadata")
        elif self.recovery is None:
            raise ValueError("run schema v3 requires recovery metadata")
        else:
            if self.status is RunStatus.QUEUED and self.recovery.process_attempts != 0:
                raise ValueError("a queued run cannot have processing attempts")
            if self.status is not RunStatus.QUEUED and self.recovery.process_attempts == 0:
                raise ValueError("a started run must have at least one processing attempt")
            if (
                self.status is RunStatus.COMPLETED
                and self.recovery.speech_segments_completed != self.recovery.speech_segments_total
            ):
                raise ValueError("a completed run must complete every speech segment")
            if self.status is not RunStatus.FAILED and self.recovery.failed_segment_id is not None:
                raise ValueError("only a failed run may name its failed segment")
        if self.status is RunStatus.FAILED and self.error is None:
            raise ValueError("a failed run must contain an error")
        if self.status is not RunStatus.FAILED and self.error is not None:
            raise ValueError("only a failed run may contain an error")
        return self

    def transition(
        self,
        status: RunStatus,
        *,
        updated_at: datetime,
        timeline_artifact: Literal["timeline.json"] | None = None,
        audio_artifact: Literal["narration.wav"] | None = None,
        audio_manifest_artifact: Literal["audio-manifest.json"] | None = None,
        quality_artifact: Literal["quality.json"] | None = None,
        recovery: RunRecovery | None = None,
        error: str | None = None,
    ) -> RunRecord:
        """Create a fully revalidated record for the next lifecycle state."""

        values = self.model_dump()
        values.update(
            {
                "status": status,
                "updated_at": updated_at,
                "timeline_artifact": timeline_artifact,
                "audio_artifact": audio_artifact,
                "audio_manifest_artifact": audio_manifest_artifact,
                "quality_artifact": quality_artifact,
                "recovery": self.recovery if recovery is None else recovery,
                "error": error,
            }
        )
        return RunRecord.model_validate(values)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RunStore:
    """Persist each run in an inspectable directory under the configured root."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def create(
        self,
        prompt: str,
        *,
        run_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> RunRecord:
        """Create a queued run and write its initial record before returning."""

        active_run_id = run_id or uuid4()
        timestamp = created_at or _utc_now()
        try:
            record = RunRecord(
                run_id=active_run_id,
                status=RunStatus.QUEUED,
                prompt=prompt,
                created_at=timestamp,
                updated_at=timestamp,
                recovery=RunRecovery(
                    process_attempts=0,
                    resume_count=0,
                    cache_hits=0,
                    cache_misses=0,
                    checkpoint_reuses=0,
                    speech_segments_total=0,
                    speech_segments_completed=0,
                ),
            )
        except ValidationError as error:
            raise RunStoreError(f"Invalid run request:\n{error}") from error
        run_directory = self.run_directory(active_run_id)
        try:
            run_directory.mkdir(parents=True, exist_ok=False)
        except FileExistsError as error:
            raise RunStoreError(f"Run directory already exists: {run_directory}") from error
        except OSError as error:
            message = f"Could not create run directory {run_directory}: {error}"
            raise RunStoreError(message) from error
        self.save(record)
        return record

    def load(self, run_id: UUID | str) -> RunRecord:
        """Load and validate a run record from disk."""

        path = self.record_path(run_id)
        try:
            return RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RunNotFoundError(f"Run record not found: {path}") from error
        except (OSError, ValidationError) as error:
            raise RunStoreError(f"Could not load run record {path}: {error}") from error

    def save(self, record: RunRecord) -> None:
        """Atomically replace a run record so readers never see partial JSON."""

        path = self.record_path(record.run_id)
        if not path.parent.is_dir():
            raise RunNotFoundError(f"Run directory not found: {path.parent}")
        self._write_json(path, record.model_dump_json(indent=2) + "\n")

    def write_timeline(self, run_id: UUID | str, timeline: Timeline) -> Path:
        """Validate ownership and atomically write the canonical artifact."""

        parsed_run_id = self.parse_run_id(run_id)
        if timeline.run_id != parsed_run_id:
            raise RunStoreError(
                f"Timeline run ID {timeline.run_id} does not match directory {parsed_run_id}"
            )
        path = self.timeline_path(parsed_run_id)
        if not path.parent.is_dir():
            raise RunNotFoundError(f"Run directory not found: {path.parent}")
        self._write_json(path, timeline.model_dump_json(indent=2) + "\n")
        return path

    def load_timeline(self, run_id: UUID | str) -> Timeline:
        """Load and validate a previously written timeline artifact."""

        path = self.timeline_path(run_id)
        try:
            return Timeline.model_validate_json(path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RunNotFoundError(f"Timeline artifact not found: {path}") from error
        except (OSError, ValidationError) as error:
            raise RunStoreError(f"Could not load timeline artifact {path}: {error}") from error

    def write_audio(self, run_id: UUID | str, wave_bytes: bytes) -> Path:
        """Atomically write the assembled PCM WAV container."""

        path = self.audio_path(run_id)
        if not path.parent.is_dir():
            raise RunNotFoundError(f"Run directory not found: {path.parent}")
        self._write_bytes(path, wave_bytes)
        return path

    def write_audio_manifest(self, run_id: UUID | str, manifest: AudioManifest) -> Path:
        """Persist exact frame ranges for every assembled timeline segment."""

        parsed_run_id = self.parse_run_id(run_id)
        if manifest.run_id != parsed_run_id:
            raise RunStoreError(
                f"Audio manifest run ID {manifest.run_id} does not match directory {parsed_run_id}"
            )
        path = self.audio_manifest_path(parsed_run_id)
        self._write_json(path, manifest.model_dump_json(indent=2) + "\n")
        return path

    def write_quality(self, run_id: UUID | str, report: AudioQualityReport) -> Path:
        """Persist the basic quality gate result beside the WAV."""

        path = self.quality_path(run_id)
        self._write_json(path, report.model_dump_json(indent=2) + "\n")
        return path

    def run_directory(self, run_id: UUID | str) -> Path:
        return self.root / str(self.parse_run_id(run_id))

    def record_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / RUN_RECORD_FILENAME

    def timeline_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / TIMELINE_FILENAME

    def audio_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / AUDIO_FILENAME

    def audio_manifest_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / AUDIO_MANIFEST_FILENAME

    def quality_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / QUALITY_FILENAME

    @staticmethod
    def parse_run_id(value: UUID | str) -> UUID:
        """Require a UUID so user input cannot traverse outside the run root."""

        if isinstance(value, UUID):
            return value
        try:
            return UUID(value)
        except (ValueError, AttributeError) as error:
            raise InvalidRunIdError(f"Invalid run ID: {value}") from error

    @staticmethod
    def _write_json(path: Path, payload: str) -> None:
        """Write beside the destination, then atomically replace it."""

        RunStore._write_bytes(path, payload.encode("utf-8"))

    @staticmethod
    def _write_bytes(path: Path, payload: bytes) -> None:
        """Write bytes beside the destination, then atomically replace it."""

        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary_path.write_bytes(payload)
            temporary_path.replace(path)
        except OSError as error:
            raise RunStoreError(f"Could not write {path}: {error}") from error
        finally:
            temporary_path.unlink(missing_ok=True)
