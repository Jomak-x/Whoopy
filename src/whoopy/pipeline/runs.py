"""Durable local run records and filesystem artifact storage."""

from __future__ import annotations

import os
import shutil
import threading
from datetime import UTC, datetime, timedelta
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
from whoopy.meditation import MeditationGenerationResult
from whoopy.pipeline.generation import (
    PendingGenerationConfig,
    RunModelMetadata,
    ScriptRunConfig,
)
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.timeline import Timeline

RUN_RECORD_FILENAME: Literal["run.json"] = "run.json"
TIMELINE_FILENAME: Literal["timeline.json"] = "timeline.json"
AUDIO_FILENAME: Literal["narration.wav"] = "narration.wav"
AUDIO_MANIFEST_FILENAME: Literal["audio-manifest.json"] = "audio-manifest.json"
QUALITY_FILENAME: Literal["quality.json"] = "quality.json"
SCRIPT_FILENAME: Literal["script.md"] = "script.md"
RESOLVED_CONFIG_FILENAME: Literal["resolved-config.json"] = "resolved-config.json"
MODEL_METADATA_FILENAME: Literal["model-metadata.json"] = "model-metadata.json"
PLAN_FILENAME: Literal["plan.json"] = "plan.json"
RAW_MODEL_OUTPUT_DIRECTORY: Literal["raw-model-output"] = "raw-model-output"
DRAFT_SECTIONS_DIRECTORY: Literal["draft-sections"] = "draft-sections"
EVENTS_FILENAME: Literal["events.jsonl"] = "events.jsonl"
GENERATION_REQUEST_FILENAME: Literal["generation-request.json"] = "generation-request.json"
PromptText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
]

_record_locks_guard = threading.Lock()
_record_locks: dict[Path, threading.RLock] = {}


def _record_lock(path: Path) -> threading.RLock:
    """Serialize read-modify-write lifecycle operations inside this process."""

    resolved = path.resolve()
    with _record_locks_guard:
        return _record_locks.setdefault(resolved, threading.RLock())


class RunStoreError(RuntimeError):
    """Base error for local run persistence."""


class RunNotFoundError(RunStoreError):
    """Raised when a requested run directory or record does not exist."""


class InvalidRunIdError(RunStoreError):
    """Raised before an unsafe or malformed value can become a filesystem path."""


class RunStatus(StrEnum):
    """Durable lifecycle shared by the control plane and local workers."""

    QUEUED = "queued"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"


class RunStage(StrEnum):
    """Fine-grained progress that remains useful after a process exits."""

    QUEUED = "queued"
    PLANNING = "planning"
    DRAFTING = "drafting"
    COMPILING = "compiling"
    WAITING_FOR_MODEL_SLOT = "waiting_for_model_slot"
    MODEL_STARTUP = "model_startup"
    SYNTHESIZING = "synthesizing"
    ASSEMBLING = "assembling"
    QUALITY_CHECK = "quality_check"
    COMPLETED = "completed"


class RunExecution(BaseModel):
    """Ownership and heartbeat data for the current or most recent attempt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: RunStage
    attempt_id: UUID | None = None
    owner_id: str | None = Field(default=None, min_length=1, max_length=200)
    pid: int | None = Field(default=None, ge=1)
    current_segment_id: str | None = Field(
        default=None,
        pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$",
    )
    started_at: AwareDatetime | None = None
    heartbeat_at: AwareDatetime | None = None
    lease_expires_at: AwareDatetime | None = None
    interruption_kind: str | None = Field(default=None, min_length=1, max_length=100)
    message: str | None = Field(default=None, min_length=1, max_length=2_000)

    @model_validator(mode="after")
    def validate_active_lease(self) -> RunExecution:
        identity_values = (
            self.attempt_id,
            self.owner_id,
            self.pid,
            self.started_at,
        )
        lease_values = (
            self.heartbeat_at,
            self.lease_expires_at,
        )
        identity_populated = sum(value is not None for value in identity_values)
        if identity_populated not in (0, len(identity_values)):
            raise ValueError("execution ownership fields must be all present or all absent")
        lease_populated = sum(value is not None for value in lease_values)
        if lease_populated not in (0, len(lease_values)):
            raise ValueError("heartbeat and lease expiry must be both present or both absent")
        if lease_populated and not identity_populated:
            raise ValueError("an execution lease requires attempt ownership")
        if self.heartbeat_at is not None:
            assert self.started_at is not None
            assert self.lease_expires_at is not None
            if self.heartbeat_at < self.started_at:
                raise ValueError("heartbeat cannot precede attempt start")
            if self.lease_expires_at <= self.heartbeat_at:
                raise ValueError("lease expiry must follow the heartbeat")
        return self

    @property
    def is_active(self) -> bool:
        """Return whether this execution carries a live-attempt identity."""

        return self.heartbeat_at is not None

    def heartbeat(
        self,
        *,
        now: datetime,
        lease_seconds: float,
        stage: RunStage | None = None,
        current_segment_id: str | None = None,
        message: str | None = None,
    ) -> RunExecution:
        """Renew an active lease while preserving its stable ownership."""

        if not self.is_active:
            raise ValueError("cannot heartbeat an execution without an active attempt")
        values = self.model_dump()
        values.update(
            {
                "stage": self.stage if stage is None else stage,
                "current_segment_id": current_segment_id,
                "heartbeat_at": now,
                "lease_expires_at": now + timedelta(seconds=lease_seconds),
                "message": message,
            }
        )
        return RunExecution.model_validate(values)

    def finish(
        self,
        *,
        stage: RunStage | None = None,
        current_segment_id: str | None = None,
        interruption_kind: str | None = None,
        message: str | None = None,
    ) -> RunExecution:
        """Retain attempt provenance but release its renewable lease."""

        values = self.model_dump()
        values.update(
            {
                "stage": self.stage if stage is None else stage,
                "current_segment_id": current_segment_id,
                "heartbeat_at": None,
                "lease_expires_at": None,
                "interruption_kind": interruption_kind,
                "message": message,
            }
        )
        return RunExecution.model_validate(values)


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

    schema_version: Literal[1, 2, 3, 4, 5, 6] = 3
    run_id: UUID
    status: RunStatus
    prompt: PromptText
    source_kind: Literal["fixture_prompt", "script_file", "generated_prompt"] = "fixture_prompt"
    created_at: AwareDatetime
    updated_at: AwareDatetime
    script_artifact: Literal["script.md"] | None = None
    resolved_config_artifact: Literal["resolved-config.json"] | None = None
    model_metadata_artifact: Literal["model-metadata.json"] | None = None
    plan_artifact: Literal["plan.json"] | None = None
    raw_model_output_artifact: Literal["raw-model-output"] | None = None
    draft_sections_artifact: Literal["draft-sections"] | None = None
    generation_request_artifact: Literal["generation-request.json"] | None = None
    timeline_artifact: Literal["timeline.json"] | None = None
    audio_artifact: Literal["narration.wav"] | None = None
    audio_manifest_artifact: Literal["audio-manifest.json"] | None = None
    quality_artifact: Literal["quality.json"] | None = None
    recovery: RunRecovery | None = None
    execution: RunExecution | None = None
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
            if self.schema_version in (2, 3, 4, 5, 6) and any(
                artifact is None for artifact in artifacts
            ):
                raise ValueError("a completed audio run must reference every audio artifact")
        elif any(artifact is not None for artifact in artifacts):
            raise ValueError("only a completed run may reference artifacts")
        if self.schema_version == 1 and any(artifact is not None for artifact in artifacts[1:]):
            raise ValueError("run schema v1 does not support audio artifacts")
        input_artifacts = (
            self.script_artifact,
            self.resolved_config_artifact,
            self.model_metadata_artifact,
        )
        generated_artifacts = (
            self.plan_artifact,
            self.raw_model_output_artifact,
            self.draft_sections_artifact,
        )
        if self.schema_version < 4:
            if (
                self.source_kind != "fixture_prompt"
                or any(artifact is not None for artifact in input_artifacts + generated_artifacts)
                or self.generation_request_artifact is not None
            ):
                raise ValueError("run schemas v1-v3 do not support real-script input artifacts")
        elif self.schema_version == 4:
            if (
                self.source_kind != "script_file"
                or any(artifact is None for artifact in input_artifacts)
                or any(artifact is not None for artifact in generated_artifacts)
                or self.generation_request_artifact is not None
            ):
                raise ValueError("run schema v4 requires only script-file input artifacts")
        elif self.schema_version == 5:
            if (
                self.source_kind != "generated_prompt"
                or any(artifact is None for artifact in input_artifacts)
                or any(artifact is None for artifact in generated_artifacts)
                or self.generation_request_artifact is not None
            ):
                raise ValueError("run schema v5 requires every generated-prompt input artifact")
        elif self.source_kind == "script_file":
            if (
                any(artifact is None for artifact in input_artifacts)
                or any(artifact is not None for artifact in generated_artifacts)
                or self.generation_request_artifact is not None
            ):
                raise ValueError("run schema v6 script state requires only script inputs")
        elif self.source_kind == "generated_prompt":
            if self.generation_request_artifact is None:
                raise ValueError("run schema v6 prompt state requires its generation request")
            has_generated_inputs = [
                artifact is not None for artifact in input_artifacts + generated_artifacts
            ]
            if any(has_generated_inputs) and not all(has_generated_inputs):
                raise ValueError(
                    "run schema v6 generated state requires every generated input artifact"
                )
            if self.status is RunStatus.COMPLETED and not all(has_generated_inputs):
                raise ValueError("a completed schema v6 prompt run must have generated inputs")
        else:
            raise ValueError("run schema v6 does not support fixture-prompt state")

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
        if self.schema_version == 6 and self.execution is None:
            raise ValueError("run schema v6 requires execution metadata")
        if self.execution is not None:
            if self.status is RunStatus.QUEUED:
                if self.execution.stage is not RunStage.QUEUED or self.execution.is_active:
                    raise ValueError("a queued run must have an inactive queued execution")
            elif self.status is RunStatus.RUNNING:
                if not self.execution.is_active:
                    raise ValueError("a running run execution must have an active lease")
                if self.execution.stage in (RunStage.QUEUED, RunStage.COMPLETED):
                    raise ValueError("a running run must have an active processing stage")
            elif self.execution.is_active:
                raise ValueError("only a running run may hold an active execution lease")
            if (
                self.status is RunStatus.COMPLETED
                and self.execution.stage is not RunStage.COMPLETED
            ):
                raise ValueError("a completed run must have a completed execution stage")
            if self.status is RunStatus.INTERRUPTED and self.execution.interruption_kind is None:
                raise ValueError("an interrupted run must record its interruption kind")
            if self.schema_version == 6:
                planning_stages = (RunStage.PLANNING, RunStage.DRAFTING)
                record_has_generated_inputs = self.script_artifact is not None
                if self.source_kind == "script_file" and self.execution.stage in planning_stages:
                    raise ValueError("a schema v6 script run cannot use a planning stage")
                if self.source_kind == "generated_prompt":
                    if not record_has_generated_inputs and self.execution.stage not in (
                        RunStage.QUEUED,
                        *planning_stages,
                    ):
                        raise ValueError("a pending schema v6 prompt run must remain in planning")
                    if record_has_generated_inputs and self.execution.stage in planning_stages:
                        raise ValueError(
                            "a generated schema v6 prompt run cannot remain in planning"
                        )
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
        execution: RunExecution | None = None,
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
                "execution": self.execution if execution is None else execution,
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
                execution=RunExecution(stage=RunStage.QUEUED),
            )
        except ValidationError as error:
            raise RunStoreError(f"Invalid run request:\n{error}") from error
        run_directory = self.run_directory(active_run_id)
        try:
            run_directory.mkdir(parents=True, exist_ok=False)
            self._fsync_directory(run_directory.parent)
        except FileExistsError as error:
            raise RunStoreError(f"Run directory already exists: {run_directory}") from error
        except OSError as error:
            message = f"Could not create run directory {run_directory}: {error}"
            raise RunStoreError(message) from error
        self.save(record)
        return record

    def create_pending_generation_run(
        self,
        *,
        prompt: str,
        generation_request: PendingGenerationConfig,
        run_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> RunRecord:
        """Atomically save a schema-v6 prompt run before its first LLM call."""

        active_run_id = run_id or uuid4()
        timestamp = created_at or _utc_now()
        try:
            record = RunRecord(
                schema_version=6,
                run_id=active_run_id,
                status=RunStatus.QUEUED,
                prompt=prompt,
                source_kind="generated_prompt",
                created_at=timestamp,
                updated_at=timestamp,
                generation_request_artifact=GENERATION_REQUEST_FILENAME,
                recovery=RunRecovery(
                    process_attempts=0,
                    resume_count=0,
                    cache_hits=0,
                    cache_misses=0,
                    checkpoint_reuses=0,
                    speech_segments_total=0,
                    speech_segments_completed=0,
                ),
                execution=RunExecution(stage=RunStage.QUEUED),
            )
        except ValidationError as error:
            raise RunStoreError(f"Invalid pending generation request:\n{error}") from error

        self.root.mkdir(parents=True, exist_ok=True)
        run_directory = self.run_directory(active_run_id)
        if run_directory.exists():
            raise RunStoreError(f"Run directory already exists: {run_directory}")
        temporary = self.root / f".{active_run_id}.{uuid4().hex}.tmp"
        try:
            temporary.mkdir()
            self._write_json(
                temporary / GENERATION_REQUEST_FILENAME,
                generation_request.model_dump_json(indent=2) + "\n",
            )
            self._write_json(
                temporary / RUN_RECORD_FILENAME,
                record.model_dump_json(indent=2) + "\n",
            )
            temporary.replace(run_directory)
            self._fsync_directory(self.root)
        except OSError as error:
            raise RunStoreError(
                f"Could not create pending generation run {active_run_id}: {error}"
            ) from error
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return record

    def create_script_run(
        self,
        *,
        script: str,
        source_name: str,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
        run_id: UUID | None = None,
        created_at: datetime | None = None,
    ) -> RunRecord:
        """Atomically create a queued schema-v4 run and its immutable inputs."""

        active_run_id = run_id or uuid4()
        timestamp = created_at or _utc_now()
        try:
            record = RunRecord(
                schema_version=4,
                run_id=active_run_id,
                status=RunStatus.QUEUED,
                prompt=f"Render local script: {source_name}",
                source_kind="script_file",
                created_at=timestamp,
                updated_at=timestamp,
                script_artifact=SCRIPT_FILENAME,
                resolved_config_artifact=RESOLVED_CONFIG_FILENAME,
                model_metadata_artifact=MODEL_METADATA_FILENAME,
                recovery=RunRecovery(
                    process_attempts=0,
                    resume_count=0,
                    cache_hits=0,
                    cache_misses=0,
                    checkpoint_reuses=0,
                    speech_segments_total=0,
                    speech_segments_completed=0,
                ),
                execution=RunExecution(stage=RunStage.QUEUED),
            )
        except ValidationError as error:
            raise RunStoreError(f"Invalid script run request:\n{error}") from error

        self.root.mkdir(parents=True, exist_ok=True)
        run_directory = self.run_directory(active_run_id)
        if run_directory.exists():
            raise RunStoreError(f"Run directory already exists: {run_directory}")
        temporary = self.root / f".{active_run_id}.{uuid4().hex}.tmp"
        try:
            temporary.mkdir()
            self._write_bytes(temporary / SCRIPT_FILENAME, script.encode("utf-8"))
            self._write_json(
                temporary / RESOLVED_CONFIG_FILENAME,
                resolved_config.model_dump_json(indent=2) + "\n",
            )
            self._write_json(
                temporary / MODEL_METADATA_FILENAME,
                model_metadata.model_dump_json(indent=2) + "\n",
            )
            self._write_json(
                temporary / RUN_RECORD_FILENAME,
                record.model_dump_json(indent=2) + "\n",
            )
            temporary.replace(run_directory)
            self._fsync_directory(self.root)
        except OSError as error:
            raise RunStoreError(f"Could not create script run {active_run_id}: {error}") from error
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return record

    def create_generated_run(
        self,
        *,
        prompt: str,
        generated: MeditationGenerationResult,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
        run_id: UUID,
        created_at: datetime,
    ) -> RunRecord:
        """Atomically promote validated draft artifacts into a queued schema-v5 run."""

        if generated.timeline.run_id != run_id:
            raise RunStoreError("Generated timeline does not belong to the requested run ID.")
        try:
            record = RunRecord(
                schema_version=5,
                run_id=run_id,
                status=RunStatus.QUEUED,
                prompt=prompt,
                source_kind="generated_prompt",
                created_at=created_at,
                updated_at=created_at,
                script_artifact=SCRIPT_FILENAME,
                resolved_config_artifact=RESOLVED_CONFIG_FILENAME,
                model_metadata_artifact=MODEL_METADATA_FILENAME,
                plan_artifact=PLAN_FILENAME,
                raw_model_output_artifact=RAW_MODEL_OUTPUT_DIRECTORY,
                draft_sections_artifact=DRAFT_SECTIONS_DIRECTORY,
                recovery=RunRecovery(
                    process_attempts=0,
                    resume_count=0,
                    cache_hits=0,
                    cache_misses=0,
                    checkpoint_reuses=0,
                    speech_segments_total=0,
                    speech_segments_completed=0,
                ),
                execution=RunExecution(stage=RunStage.QUEUED),
            )
        except ValidationError as error:
            raise RunStoreError(f"Invalid generated run request:\n{error}") from error

        self.root.mkdir(parents=True, exist_ok=True)
        run_directory = self.run_directory(run_id)
        if run_directory.exists():
            raise RunStoreError(f"Run directory already exists: {run_directory}")
        temporary = self.root / f".{run_id}.{uuid4().hex}.tmp"
        try:
            temporary.mkdir()
            (temporary / RAW_MODEL_OUTPUT_DIRECTORY).mkdir()
            (temporary / DRAFT_SECTIONS_DIRECTORY).mkdir()
            self._write_bytes(temporary / SCRIPT_FILENAME, generated.script.encode("utf-8"))
            self._write_json(
                temporary / PLAN_FILENAME,
                generated.plan.model_dump_json(indent=2) + "\n",
            )
            for attempt_number, attempt in enumerate(generated.raw_attempts, start=1):
                self._write_json(
                    temporary
                    / RAW_MODEL_OUTPUT_DIRECTORY
                    / f"{attempt_number:03d}-{attempt.stage.replace(':', '-')}.json",
                    attempt.model_dump_json(indent=2) + "\n",
                )
            for section in generated.sections:
                self._write_json(
                    temporary / DRAFT_SECTIONS_DIRECTORY / f"{section.section_id}.json",
                    section.model_dump_json(indent=2) + "\n",
                )
            self._write_json(
                temporary / RESOLVED_CONFIG_FILENAME,
                resolved_config.model_dump_json(indent=2) + "\n",
            )
            self._write_json(
                temporary / MODEL_METADATA_FILENAME,
                model_metadata.model_dump_json(indent=2) + "\n",
            )
            self._write_json(
                temporary / RUN_RECORD_FILENAME,
                record.model_dump_json(indent=2) + "\n",
            )
            temporary.replace(run_directory)
            self._fsync_directory(self.root)
        except OSError as error:
            raise RunStoreError(f"Could not create generated run {run_id}: {error}") from error
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
        return record

    def promote_queued_script_run(
        self,
        run_id: UUID | str,
        *,
        script: str,
        source_name: str,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
        promoted_at: datetime | None = None,
    ) -> RunRecord:
        """Lock and commit script inputs into a pre-created run UUID."""

        parsed_run_id = self.parse_run_id(run_id)
        try:
            with (
                RunLock(self.run_directory(parsed_run_id)),
                _record_lock(self.record_path(parsed_run_id)),
            ):
                return self._promote_queued_script_run_unlocked(
                    parsed_run_id,
                    script=script,
                    source_name=source_name,
                    resolved_config=resolved_config,
                    model_metadata=model_metadata,
                    promoted_at=promoted_at,
                )
        except RunLockUnavailable as error:
            raise RunStoreError(f"Run {parsed_run_id} is owned by another process") from error

    def _promote_queued_script_run_unlocked(
        self,
        run_id: UUID | str,
        *,
        script: str,
        source_name: str,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
        promoted_at: datetime | None = None,
    ) -> RunRecord:
        """Commit script inputs into a pre-created, never-started run UUID."""

        placeholder = self._promotable_placeholder(run_id, allow_partial_artifacts=True)
        if (
            placeholder.generation_request_artifact is not None
            or self.generation_request_path(placeholder.run_id).exists()
        ):
            raise RunStoreError(
                f"Run {placeholder.run_id} is a prompt-generation request, not a script run"
            )
        timestamp = promoted_at or _utc_now()
        try:
            promoted = RunRecord(
                schema_version=6,
                run_id=placeholder.run_id,
                status=RunStatus.QUEUED,
                prompt=f"Render local script: {source_name}",
                source_kind="script_file",
                created_at=placeholder.created_at,
                updated_at=timestamp,
                script_artifact=SCRIPT_FILENAME,
                resolved_config_artifact=RESOLVED_CONFIG_FILENAME,
                model_metadata_artifact=MODEL_METADATA_FILENAME,
                recovery=placeholder.recovery,
                execution=RunExecution(stage=RunStage.QUEUED),
            )
        except ValidationError as error:
            raise RunStoreError(f"Invalid script run promotion:\n{error}") from error
        directory = self.run_directory(placeholder.run_id)
        self._write_idempotent_bundle(
            directory,
            files={
                Path(SCRIPT_FILENAME): script.encode("utf-8"),
                Path(RESOLVED_CONFIG_FILENAME): (
                    resolved_config.model_dump_json(indent=2) + "\n"
                ).encode("utf-8"),
                Path(MODEL_METADATA_FILENAME): (
                    model_metadata.model_dump_json(indent=2) + "\n"
                ).encode("utf-8"),
            },
        )
        # The record is the commit marker and is replaced last. Readers therefore
        # observe either the original placeholder or the complete promoted run.
        self.save(promoted)
        return promoted

    def promote_queued_generated_run(
        self,
        run_id: UUID | str,
        *,
        generated: MeditationGenerationResult,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
        promoted_at: datetime | None = None,
    ) -> RunRecord:
        """Lock and commit generated inputs into a never-started run UUID."""

        parsed_run_id = self.parse_run_id(run_id)
        try:
            with (
                RunLock(self.run_directory(parsed_run_id)),
                _record_lock(self.record_path(parsed_run_id)),
            ):
                return self._promote_queued_generated_run_unlocked(
                    parsed_run_id,
                    generated=generated,
                    resolved_config=resolved_config,
                    model_metadata=model_metadata,
                    promoted_at=promoted_at,
                )
        except RunLockUnavailable as error:
            raise RunStoreError(f"Run {parsed_run_id} is owned by another process") from error

    def _promote_queued_generated_run_unlocked(
        self,
        run_id: UUID | str,
        *,
        generated: MeditationGenerationResult,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
        promoted_at: datetime | None = None,
    ) -> RunRecord:
        """Commit generated inputs into a pre-created, never-started run UUID."""

        placeholder = self._promotable_placeholder(run_id, allow_partial_artifacts=True)
        request = self.load_generation_request(placeholder.run_id)
        if generated.timeline.run_id != placeholder.run_id:
            raise RunStoreError("Generated timeline does not belong to the placeholder run ID.")
        self._validate_generated_promotion(
            request,
            resolved_config=resolved_config,
            model_metadata=model_metadata,
        )
        timestamp = promoted_at or _utc_now()
        try:
            promoted = RunRecord(
                schema_version=6,
                run_id=placeholder.run_id,
                status=RunStatus.QUEUED,
                prompt=placeholder.prompt,
                source_kind="generated_prompt",
                created_at=placeholder.created_at,
                updated_at=timestamp,
                script_artifact=SCRIPT_FILENAME,
                resolved_config_artifact=RESOLVED_CONFIG_FILENAME,
                model_metadata_artifact=MODEL_METADATA_FILENAME,
                plan_artifact=PLAN_FILENAME,
                raw_model_output_artifact=RAW_MODEL_OUTPUT_DIRECTORY,
                draft_sections_artifact=DRAFT_SECTIONS_DIRECTORY,
                generation_request_artifact=GENERATION_REQUEST_FILENAME,
                recovery=placeholder.recovery,
                execution=RunExecution(stage=RunStage.QUEUED),
            )
        except ValidationError as error:
            raise RunStoreError(f"Invalid generated run promotion:\n{error}") from error

        self._write_generated_promotion_artifacts(
            promoted,
            generated=generated,
            resolved_config=resolved_config,
            model_metadata=model_metadata,
        )
        self.save(promoted)
        return promoted

    def start_generation(
        self,
        run_id: UUID | str,
        *,
        owner_id: str,
        pid: int,
        started_at: datetime | None = None,
        attempt_id: UUID | None = None,
        lease_seconds: float = 15.0,
    ) -> RunRecord:
        """Claim a queued placeholder before any potentially long LLM work."""

        placeholder = self._promotable_placeholder(run_id)
        self.load_generation_request(placeholder.run_id)
        recovery = placeholder.recovery
        assert recovery is not None
        timestamp = started_at or _utc_now()
        execution = RunExecution(
            stage=RunStage.PLANNING,
            attempt_id=attempt_id or uuid4(),
            owner_id=owner_id,
            pid=pid,
            started_at=timestamp,
            heartbeat_at=timestamp,
            lease_expires_at=timestamp + timedelta(seconds=lease_seconds),
            message="Planning the meditation structure.",
        )
        claimed = placeholder.transition(
            RunStatus.RUNNING,
            updated_at=timestamp,
            recovery=RunRecovery.model_validate(
                {
                    **recovery.model_dump(),
                    "process_attempts": recovery.process_attempts + 1,
                }
            ),
            execution=execution,
        )
        self.save(claimed)
        return claimed

    def update_generation_stage(
        self,
        run_id: UUID | str,
        *,
        attempt_id: UUID,
        stage: Literal[RunStage.PLANNING, RunStage.DRAFTING],
        now: datetime | None = None,
        lease_seconds: float = 15.0,
        message: str | None = None,
    ) -> RunRecord:
        """Heartbeat a claimed generation while exposing planning or drafting."""

        timestamp = now or _utc_now()
        record = self.load(run_id)
        execution = self._owned_active_execution(record, attempt_id)
        return self.update_execution(
            record.run_id,
            execution.heartbeat(
                now=timestamp,
                lease_seconds=lease_seconds,
                stage=RunStage(stage),
                message=message,
            ),
            updated_at=timestamp,
        )

    def restart_generation(
        self,
        run_id: UUID | str,
        *,
        owner_id: str,
        pid: int,
        started_at: datetime | None = None,
        attempt_id: UUID | None = None,
        lease_seconds: float = 15.0,
    ) -> RunRecord:
        """Restart an interrupted placeholder without losing its durable UUID."""

        record = self.load(run_id)
        if record.status not in (RunStatus.INTERRUPTED, RunStatus.FAILED):
            raise RunStoreError(
                f"Run {record.run_id} is {record.status.value}; only stopped generation restarts"
            )
        if (
            record.schema_version != 6
            or record.source_kind != "generated_prompt"
            or record.generation_request_artifact is None
        ):
            raise RunStoreError(f"Run {record.run_id} already has renderable immutable inputs")
        self._assert_no_immutable_inputs(record, allow_partial_artifacts=True)
        self.load_generation_request(record.run_id)
        recovery = record.recovery
        if recovery is None:
            raise RunStoreError(f"Run {record.run_id} has no recovery metadata")
        timestamp = started_at or _utc_now()
        execution = RunExecution(
            stage=RunStage.PLANNING,
            attempt_id=attempt_id or uuid4(),
            owner_id=owner_id,
            pid=pid,
            started_at=timestamp,
            heartbeat_at=timestamp,
            lease_expires_at=timestamp + timedelta(seconds=lease_seconds),
            message="Restarting meditation planning.",
        )
        restarted_recovery = RunRecovery.model_validate(
            {
                **recovery.model_dump(),
                "process_attempts": recovery.process_attempts + 1,
                "resume_count": recovery.resume_count + 1,
                "failed_segment_id": None,
            }
        )
        restarted = record.transition(
            RunStatus.RUNNING,
            updated_at=timestamp,
            recovery=restarted_recovery,
            execution=execution,
        )
        self.save(restarted)
        return restarted

    def promote_active_generated_run(
        self,
        run_id: UUID | str,
        *,
        attempt_id: UUID,
        generated: MeditationGenerationResult,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
        promoted_at: datetime | None = None,
        lease_seconds: float = 15.0,
    ) -> RunRecord:
        """Commit an active promotion while serializing heartbeat writes."""

        with _record_lock(self.record_path(run_id)):
            return self._promote_active_generated_run_unlocked(
                run_id,
                attempt_id=attempt_id,
                generated=generated,
                resolved_config=resolved_config,
                model_metadata=model_metadata,
                promoted_at=promoted_at,
                lease_seconds=lease_seconds,
            )

    def _promote_active_generated_run_unlocked(
        self,
        run_id: UUID | str,
        *,
        attempt_id: UUID,
        generated: MeditationGenerationResult,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
        promoted_at: datetime | None = None,
        lease_seconds: float = 15.0,
    ) -> RunRecord:
        """Commit generated inputs while preserving the active generation lease."""

        active = self.load(run_id)
        execution = self._owned_active_execution(active, attempt_id)
        if execution.stage not in (RunStage.PLANNING, RunStage.DRAFTING):
            raise RunStoreError(
                f"Run {active.run_id} is in {execution.stage.value}, not generation"
            )
        self._assert_no_immutable_inputs(active, allow_partial_artifacts=True)
        request = self.load_generation_request(active.run_id)
        if generated.timeline.run_id != active.run_id:
            raise RunStoreError("Generated timeline does not belong to the active run ID.")
        self._validate_generated_promotion(
            request,
            resolved_config=resolved_config,
            model_metadata=model_metadata,
        )
        timestamp = promoted_at or _utc_now()
        promoted_execution = execution.heartbeat(
            now=timestamp,
            lease_seconds=lease_seconds,
            stage=RunStage.COMPILING,
            message="Generation finished; preparing local audio processing.",
        )
        try:
            promoted = RunRecord(
                schema_version=6,
                run_id=active.run_id,
                status=RunStatus.RUNNING,
                prompt=active.prompt,
                source_kind="generated_prompt",
                created_at=active.created_at,
                updated_at=timestamp,
                script_artifact=SCRIPT_FILENAME,
                resolved_config_artifact=RESOLVED_CONFIG_FILENAME,
                model_metadata_artifact=MODEL_METADATA_FILENAME,
                plan_artifact=PLAN_FILENAME,
                raw_model_output_artifact=RAW_MODEL_OUTPUT_DIRECTORY,
                draft_sections_artifact=DRAFT_SECTIONS_DIRECTORY,
                generation_request_artifact=GENERATION_REQUEST_FILENAME,
                recovery=active.recovery,
                execution=promoted_execution,
            )
        except ValidationError as error:
            raise RunStoreError(f"Invalid active generated run promotion:\n{error}") from error
        self._write_generated_promotion_artifacts(
            promoted,
            generated=generated,
            resolved_config=resolved_config,
            model_metadata=model_metadata,
        )
        self.save(promoted)
        return promoted

    def interrupt_active_run(
        self,
        run_id: UUID | str,
        *,
        attempt_id: UUID,
        kind: str,
        message: str,
        interrupted_at: datetime | None = None,
    ) -> RunRecord:
        """Persist a resumable interruption during planning, drafting, or audio."""

        timestamp = interrupted_at or _utc_now()
        record = self.load(run_id)
        execution = self._owned_active_execution(record, attempt_id)
        interrupted = record.transition(
            RunStatus.INTERRUPTED,
            updated_at=timestamp,
            recovery=record.recovery,
            execution=execution.finish(
                current_segment_id=execution.current_segment_id,
                interruption_kind=kind,
                message=message,
            ),
        )
        self.save(interrupted)
        return interrupted

    def fail_active_run(
        self,
        run_id: UUID | str,
        *,
        attempt_id: UUID,
        error: str,
        failed_at: datetime | None = None,
    ) -> RunRecord:
        """Persist a terminal generation or processing failure with ownership checks."""

        timestamp = failed_at or _utc_now()
        record = self.load(run_id)
        execution = self._owned_active_execution(record, attempt_id)
        failed = record.transition(
            RunStatus.FAILED,
            updated_at=timestamp,
            recovery=record.recovery,
            execution=execution.finish(
                current_segment_id=execution.current_segment_id,
                message=error,
            ),
            error=error,
        )
        self.save(failed)
        return failed

    def _write_generated_promotion_artifacts(
        self,
        record: RunRecord,
        *,
        generated: MeditationGenerationResult,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
    ) -> None:
        files: dict[Path, bytes] = {
            Path(SCRIPT_FILENAME): generated.script.encode("utf-8"),
            Path(PLAN_FILENAME): (generated.plan.model_dump_json(indent=2) + "\n").encode(),
            Path(RESOLVED_CONFIG_FILENAME): (
                resolved_config.model_dump_json(indent=2) + "\n"
            ).encode(),
            Path(MODEL_METADATA_FILENAME): (
                model_metadata.model_dump_json(indent=2) + "\n"
            ).encode(),
        }
        for attempt_number, attempt in enumerate(generated.raw_attempts, start=1):
            files[
                Path(RAW_MODEL_OUTPUT_DIRECTORY)
                / f"{attempt_number:03d}-{attempt.stage.replace(':', '-')}.json"
            ] = (attempt.model_dump_json(indent=2) + "\n").encode()
        for section in generated.sections:
            files[Path(DRAFT_SECTIONS_DIRECTORY) / f"{section.section_id}.json"] = (
                section.model_dump_json(indent=2) + "\n"
            ).encode()
        self._write_idempotent_bundle(
            self.run_directory(record.run_id),
            files=files,
            directories=(
                Path(RAW_MODEL_OUTPUT_DIRECTORY),
                Path(DRAFT_SECTIONS_DIRECTORY),
            ),
        )

    def _write_idempotent_bundle(
        self,
        directory: Path,
        *,
        files: dict[Path, bytes],
        directories: tuple[Path, ...] = (),
    ) -> None:
        """Finish an identical partial promotion without replacing existing data."""

        expected_paths = set(files)
        try:
            for relative_directory in directories:
                target = directory / relative_directory
                if target.exists() and not target.is_dir():
                    raise RunStoreError(f"Promotion path is not a directory: {target}")
                target.mkdir(exist_ok=True)
                unexpected = {
                    path.relative_to(directory)
                    for path in target.rglob("*")
                    if path.is_file() and path.relative_to(directory) not in expected_paths
                }
                if unexpected:
                    names = ", ".join(sorted(path.as_posix() for path in unexpected))
                    raise RunStoreError(f"Promotion directory contains unrecognized files: {names}")
            for relative_path, payload in files.items():
                target = directory / relative_path
                if target.exists():
                    if target.is_symlink() or not target.is_file():
                        raise RunStoreError(f"Promotion path is not a regular file: {target}")
                    if target.read_bytes() != payload:
                        raise RunStoreError(
                            f"Refusing to replace different partial promotion data: {target}"
                        )
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                self._write_bytes(target, payload)
            for relative_directory in directories:
                self._fsync_directory(directory / relative_directory)
            self._fsync_directory(directory)
        except OSError as error:
            raise RunStoreError(
                f"Could not write promotion bundle in {directory}: {error}"
            ) from error

    @staticmethod
    def _validate_generated_promotion(
        request: PendingGenerationConfig,
        *,
        resolved_config: ScriptRunConfig,
        model_metadata: RunModelMetadata,
    ) -> None:
        """Refuse a promotion that changes any durable pre-planning input."""

        generation = resolved_config.generation
        if generation is None:
            raise RunStoreError("Generated promotion requires resolved generation settings")
        if resolved_config.profile == "basic":
            raise RunStoreError("Generated promotion requires a local LLM profile")
        actual = PendingGenerationConfig(
            profile=resolved_config.profile,
            target=resolved_config.target,
            tts=resolved_config.tts,
            processing=resolved_config.processing,
            duration_seconds=generation.duration_seconds,
            seed=generation.seed,
            plan_prompt_id=generation.plan_prompt_id,
            plan_prompt_version=generation.plan_prompt_version,
            section_prompt_id=generation.section_prompt_id,
            section_prompt_version=generation.section_prompt_version,
            max_parallel_sections=generation.max_parallel_sections,
            model_metadata=model_metadata,
        )
        if actual != request:
            raise RunStoreError(
                "Generated promotion settings differ from the immutable generation request"
            )

    @staticmethod
    def _owned_active_execution(record: RunRecord, attempt_id: UUID) -> RunExecution:
        execution = record.execution
        if record.status is not RunStatus.RUNNING or execution is None or not execution.is_active:
            raise RunStoreError(f"Run {record.run_id} has no active execution")
        if execution.attempt_id != attempt_id:
            raise RunStoreError(f"Run {record.run_id} is owned by a different attempt")
        return execution

    def _promotable_placeholder(
        self,
        run_id: UUID | str,
        *,
        allow_partial_artifacts: bool = False,
    ) -> RunRecord:
        """Validate that promotion cannot overwrite started or immutable work."""

        placeholder = self.load(run_id)
        recovery = placeholder.recovery
        if placeholder.status is not RunStatus.QUEUED:
            raise RunStoreError(
                f"Run {placeholder.run_id} is {placeholder.status.value}; only queued runs promote"
            )
        if recovery is None or recovery.process_attempts != 0:
            raise RunStoreError(f"Run {placeholder.run_id} has already started and cannot promote")
        self._assert_no_immutable_inputs(
            placeholder,
            allow_partial_artifacts=allow_partial_artifacts,
        )
        return placeholder

    def _assert_no_immutable_inputs(
        self,
        placeholder: RunRecord,
        *,
        allow_partial_artifacts: bool = False,
    ) -> None:
        """Refuse to overwrite either referenced or orphaned immutable inputs."""

        immutable_paths = (
            self.script_path(placeholder.run_id),
            self.resolved_config_path(placeholder.run_id),
            self.model_metadata_path(placeholder.run_id),
            self.plan_path(placeholder.run_id),
            self.raw_model_output_path(placeholder.run_id),
            self.draft_sections_path(placeholder.run_id),
        )
        pending_prompt = (
            placeholder.schema_version == 6
            and placeholder.source_kind == "generated_prompt"
            and placeholder.generation_request_artifact == GENERATION_REQUEST_FILENAME
        )
        if (placeholder.source_kind != "fixture_prompt" and not pending_prompt) or any(
            value is not None
            for value in (
                placeholder.script_artifact,
                placeholder.resolved_config_artifact,
                placeholder.model_metadata_artifact,
                placeholder.plan_artifact,
                placeholder.raw_model_output_artifact,
                placeholder.draft_sections_artifact,
            )
        ):
            raise RunStoreError(f"Run {placeholder.run_id} already contains immutable inputs")
        if not allow_partial_artifacts and any(path.exists() for path in immutable_paths):
            raise RunStoreError(f"Run {placeholder.run_id} already contains immutable artifacts")

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

    def write_generation_request(
        self,
        run_id: UUID | str,
        request: PendingGenerationConfig,
        *,
        written_at: datetime | None = None,
    ) -> Path:
        """Initialize an untouched placeholder, never replacing different inputs.

        The atomic ``create_pending_generation_run`` API is preferred. This
        method exists for control planes that already allocated a legacy UUID;
        an identical retry is idempotent and repairs a crash between artifact
        and record commits.
        """

        try:
            with RunLock(self.run_directory(run_id)):
                record = self.load(run_id)
                path = self.generation_request_path(record.run_id)
                if path.exists():
                    existing = self.load_generation_request(record.run_id)
                    if existing != request:
                        raise RunStoreError(
                            f"Run {record.run_id} already has a different generation request"
                        )
                    if record.generation_request_artifact == GENERATION_REQUEST_FILENAME:
                        return path
                if record.schema_version != 3:
                    raise RunStoreError(
                        f"Run {record.run_id} cannot be initialized as a generation request"
                    )
                placeholder = self._promotable_placeholder(record.run_id)
                timestamp = written_at or _utc_now()
                try:
                    upgraded = RunRecord(
                        schema_version=6,
                        run_id=placeholder.run_id,
                        status=RunStatus.QUEUED,
                        prompt=placeholder.prompt,
                        source_kind="generated_prompt",
                        created_at=placeholder.created_at,
                        updated_at=timestamp,
                        generation_request_artifact=GENERATION_REQUEST_FILENAME,
                        recovery=placeholder.recovery,
                        execution=RunExecution(stage=RunStage.QUEUED),
                    )
                except ValidationError as error:
                    raise RunStoreError(f"Invalid generation request upgrade:\n{error}") from error
                if not path.exists():
                    self._write_json(path, request.model_dump_json(indent=2) + "\n")
                self.save(upgraded)
                return path
        except RunLockUnavailable as error:
            raise RunStoreError(
                f"Run {self.parse_run_id(run_id)} is being initialized by another process"
            ) from error

    def update_execution(
        self,
        run_id: UUID | str,
        execution: RunExecution,
        *,
        updated_at: datetime,
    ) -> RunRecord:
        """Atomically persist execution progress without changing run artifacts."""

        with _record_lock(self.record_path(run_id)):
            record = self.load(run_id)
            if record.status is not RunStatus.RUNNING:
                raise RunStoreError(
                    f"Cannot update execution for {record.run_id}: run is {record.status.value}"
                )
            current = record.execution
            if (
                current is None
                or current.attempt_id is None
                or execution.attempt_id != current.attempt_id
            ):
                raise RunStoreError(
                    f"Run {record.run_id} is owned by a different processing attempt"
                )
            updated = record.transition(
                RunStatus.RUNNING,
                updated_at=updated_at,
                recovery=record.recovery,
                execution=execution,
            )
            self.save(updated)
            return updated

    def heartbeat(
        self,
        run_id: UUID | str,
        *,
        attempt_id: UUID,
        now: datetime,
        lease_seconds: float = 15.0,
    ) -> RunRecord:
        """Renew one attempt, refusing to overwrite a newer worker's ownership."""

        with _record_lock(self.record_path(run_id)):
            record = self.load(run_id)
            execution = record.execution
            if record.status is not RunStatus.RUNNING or execution is None:
                raise RunStoreError(f"Run {record.run_id} has no active execution to heartbeat")
            if execution.attempt_id != attempt_id:
                raise RunStoreError(
                    f"Run {record.run_id} is owned by a different processing attempt"
                )
            updated = record.transition(
                RunStatus.RUNNING,
                updated_at=now,
                recovery=record.recovery,
                execution=execution.heartbeat(now=now, lease_seconds=lease_seconds),
            )
            self.save(updated)
            return updated

    def reconcile_stale_runs(
        self,
        *,
        now: datetime | None = None,
        legacy_lease_seconds: float = 15.0,
    ) -> tuple[RunRecord, ...]:
        """Convert expired or legacy abandoned running records to interrupted."""

        current_time = now or _utc_now()
        reconciled: list[RunRecord] = []
        if not self.root.is_dir():
            return ()
        for directory in sorted(self.root.iterdir(), key=lambda item: item.name):
            if not directory.is_dir():
                continue
            try:
                run_id = self.parse_run_id(directory.name)
                before = self.load(run_id)
                record = self.reconcile_stale_run(
                    run_id,
                    now=current_time,
                    legacy_lease_seconds=legacy_lease_seconds,
                )
            except (InvalidRunIdError, RunStoreError):
                continue
            if before.status is RunStatus.RUNNING and record.status is RunStatus.INTERRUPTED:
                reconciled.append(record)
        return tuple(reconciled)

    def reconcile_stale_run(
        self,
        run_id: UUID | str,
        *,
        now: datetime | None = None,
        legacy_lease_seconds: float = 15.0,
    ) -> RunRecord:
        """Reconcile one run, returning its current durable record."""

        parsed_run_id = self.parse_run_id(run_id)
        try:
            with RunLock(self.run_directory(parsed_run_id)):
                return self.reconcile_stale_run_locked(
                    parsed_run_id,
                    now=now,
                    legacy_lease_seconds=legacy_lease_seconds,
                )
        except RunLockUnavailable:
            # The OS lock is stronger evidence of a live owner than an expired
            # wall-clock lease. Leave the record untouched and try again later.
            return self.load(parsed_run_id)

    def reconcile_stale_run_locked(
        self,
        run_id: UUID | str,
        *,
        now: datetime | None = None,
        legacy_lease_seconds: float = 15.0,
    ) -> RunRecord:
        """Reconcile one run when the caller already owns its OS-level lock."""

        with _record_lock(self.record_path(run_id)):
            record = self.load(run_id)
            if record.status is not RunStatus.RUNNING:
                return record
            return (
                self._reconcile_record_if_stale(
                    record,
                    now=now or _utc_now(),
                    legacy_lease_seconds=legacy_lease_seconds,
                )
                or record
            )

    def _reconcile_record_if_stale(
        self,
        record: RunRecord,
        *,
        now: datetime,
        legacy_lease_seconds: float,
    ) -> RunRecord | None:
        execution = record.execution
        expires_at = (
            execution.lease_expires_at
            if execution is not None and execution.lease_expires_at is not None
            else record.updated_at + timedelta(seconds=legacy_lease_seconds)
        )
        if expires_at > now:
            return None
        message = (
            "The previous worker stopped updating its lease. "
            "Completed segment checkpoints were kept and the run can be resumed."
        )
        if execution is None:
            interrupted_execution = RunExecution(
                stage=RunStage.COMPILING,
                interruption_kind="lease_expired",
                message=message,
            )
        else:
            interrupted_execution = execution.finish(
                current_segment_id=execution.current_segment_id,
                interruption_kind="lease_expired",
                message=message,
            )
        interrupted = record.transition(
            RunStatus.INTERRUPTED,
            updated_at=now,
            recovery=record.recovery,
            execution=interrupted_execution,
        )
        self.save(interrupted)
        return interrupted

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

    def load_script(self, run_id: UUID | str) -> str:
        path = self.script_path(run_id)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as error:
            raise RunStoreError(f"Could not load script artifact {path}: {error}") from error

    def load_resolved_config(self, run_id: UUID | str) -> ScriptRunConfig:
        path = self.resolved_config_path(run_id)
        try:
            return ScriptRunConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise RunStoreError(f"Could not load resolved config {path}: {error}") from error

    def load_model_metadata(self, run_id: UUID | str) -> RunModelMetadata:
        path = self.model_metadata_path(run_id)
        try:
            return RunModelMetadata.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise RunStoreError(f"Could not load model metadata {path}: {error}") from error

    def load_generation_request(self, run_id: UUID | str) -> PendingGenerationConfig:
        """Load and validate the immutable inputs for prompt planning."""

        path = self.generation_request_path(run_id)
        try:
            return PendingGenerationConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            raise RunStoreError(f"Could not load generation request {path}: {error}") from error

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

    def script_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / SCRIPT_FILENAME

    def resolved_config_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / RESOLVED_CONFIG_FILENAME

    def model_metadata_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / MODEL_METADATA_FILENAME

    def generation_request_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / GENERATION_REQUEST_FILENAME

    def plan_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / PLAN_FILENAME

    def raw_model_output_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / RAW_MODEL_OUTPUT_DIRECTORY

    def draft_sections_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / DRAFT_SECTIONS_DIRECTORY

    def events_path(self, run_id: UUID | str) -> Path:
        return self.run_directory(run_id) / EVENTS_FILENAME

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
        """Flush bytes, atomically replace the destination, then flush its directory."""

        temporary_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary_path.open("wb") as temporary_file:
                temporary_file.write(payload)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.replace(temporary_path, path)
            RunStore._fsync_directory(path.parent)
        except OSError as error:
            raise RunStoreError(f"Could not write {path}: {error}") from error
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        """Persist directory-entry changes where the platform supports it."""

        if os.name == "nt":
            # Windows does not support opening a directory with os.open. The
            # flushed file plus atomic os.replace is the strongest portable path.
            return
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        descriptor = os.open(directory, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
