"""Auditable preparation for manually regenerating one speech segment.

This module deliberately stops before synthesis.  It validates and archives the
current evidence, then returns an explicit cache-bypass contract for the worker
that performs the resumed attempt.
"""

from __future__ import annotations

import hashlib
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from whoopy.pipeline.checkpoints import CheckpointStatus, SegmentCheckpointStore
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.runs import (
    RunExecution,
    RunRecord,
    RunRecovery,
    RunStage,
    RunStatus,
    RunStore,
    RunStoreError,
)
from whoopy.timeline import SpeechSegment

REGENERATION_HISTORY_DIRECTORY = "history/manual-regeneration"
REGENERATION_RECORD_FILENAME = "request.json"
FINAL_EVIDENCE_FILENAMES = (
    "narration.wav",
    "audio-manifest.json",
    "quality.json",
)


class SegmentRegenerationError(RunStoreError):
    """Raised when a manual segment regeneration cannot be prepared safely."""


class ArchivedRegenerationFile(BaseModel):
    """Digest and location of one file copied into regeneration history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str = Field(min_length=1)
    archived_as: str = Field(min_length=1)
    byte_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class SegmentRegenerationArchive(BaseModel):
    """Machine-readable provenance committed with an evidence snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    regeneration_attempt_id: UUID
    run_id: UUID
    segment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    requested_at: datetime
    original_status: RunStatus
    original_execution_attempt_id: UUID | None = None
    checkpoint_status: CheckpointStatus | None = None
    bypass_checkpoint: bool = True
    bypass_shared_cache: bool = True
    files: tuple[ArchivedRegenerationFile, ...]


@dataclass(frozen=True)
class SegmentRegenerationPreparation:
    """Handoff from safe preparation to a cache-bypassing worker resume."""

    run_id: UUID
    segment_id: str
    regeneration_attempt_id: UUID
    archive_directory: Path
    record: RunRecord
    bypass_checkpoint: bool = True
    bypass_shared_cache: bool = True

    @property
    def bypass_cache_segment_ids(self) -> frozenset[str]:
        """Return the exact segment allowlist a worker must synthesize afresh."""

        return frozenset((self.segment_id,))


def prepare_segment_regeneration(
    store: RunStore,
    run_id: UUID | str,
    segment_id: str,
    *,
    requested_at: datetime | None = None,
    regeneration_attempt_id: UUID | None = None,
) -> SegmentRegenerationPreparation:
    """Archive prior evidence and make one stopped run resumable.

    The function never removes the current checkpoint or final artifacts.  A
    durable archive is committed first, and ``run.json`` is the final commit
    marker for preparation.  The caller must pass ``bypass_cache_segment_ids``
    to the worker so both the checkpoint and shared cache are skipped for the
    requested segment only.
    """

    parsed_run_id = store.parse_run_id(run_id)
    checkpoint_store = SegmentCheckpointStore(store)
    # Validate before any history directory is created.  This is also the only
    # user-controlled path component beneath the run directory.
    checkpoint_store.segment_directory(parsed_run_id, segment_id)
    timestamp = requested_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise SegmentRegenerationError("Regeneration request time must include a timezone")
    active_attempt_id = regeneration_attempt_id or uuid4()

    try:
        with RunLock(store.run_directory(parsed_run_id)):
            record = store.load(parsed_run_id)
            _validate_stopped_run(record)
            timeline = store.load_timeline(parsed_run_id)
            if timeline.run_id != parsed_run_id:
                raise SegmentRegenerationError(
                    f"Timeline {timeline.run_id} does not belong to run {parsed_run_id}"
                )
            matching = [segment for segment in timeline.segments if segment.id == segment_id]
            if not matching:
                raise SegmentRegenerationError(
                    f"Segment {segment_id} is not present in run {parsed_run_id}"
                )
            if not isinstance(matching[0], SpeechSegment):
                raise SegmentRegenerationError(
                    f"Segment {segment_id} is not a speech segment and cannot be regenerated"
                )

            archive_directory = _archive_evidence(
                store,
                checkpoint_store,
                record,
                segment_id=segment_id,
                requested_at=timestamp,
                regeneration_attempt_id=active_attempt_id,
            )
            interrupted = _manual_regeneration_record(
                record,
                checkpoint_store=checkpoint_store,
                segment_id=segment_id,
                requested_at=timestamp,
            )
            # This atomic record replacement is the preparation commit marker.
            # If it fails, the original run stays authoritative and the archive
            # remains as harmless, auditable evidence of the attempted request.
            store.save(interrupted)
    except RunLockUnavailable as error:
        raise SegmentRegenerationError(
            f"Run {parsed_run_id} is owned by an active worker"
        ) from error

    return SegmentRegenerationPreparation(
        run_id=parsed_run_id,
        segment_id=segment_id,
        regeneration_attempt_id=active_attempt_id,
        archive_directory=archive_directory,
        record=interrupted,
    )


def _validate_stopped_run(record: RunRecord) -> None:
    execution = record.execution
    if record.status is RunStatus.RUNNING or (execution is not None and execution.is_active):
        raise SegmentRegenerationError(f"Run {record.run_id} is owned by an active worker")
    if record.status not in (
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.INTERRUPTED,
    ):
        raise SegmentRegenerationError(
            f"Run {record.run_id} is {record.status.value}; only stopped runs regenerate"
        )
    if record.recovery is None:
        raise SegmentRegenerationError(
            f"Run {record.run_id} has no recovery metadata and cannot be resumed"
        )


def _archive_evidence(
    store: RunStore,
    checkpoints: SegmentCheckpointStore,
    record: RunRecord,
    *,
    segment_id: str,
    requested_at: datetime,
    regeneration_attempt_id: UUID,
) -> Path:
    run_directory = store.run_directory(record.run_id)
    history_root = run_directory / REGENERATION_HISTORY_DIRECTORY
    archive_directory = history_root / str(regeneration_attempt_id)
    staging = history_root / f".{regeneration_attempt_id}.tmp"
    if archive_directory.exists() or staging.exists():
        raise SegmentRegenerationError(
            f"Regeneration attempt already exists: {regeneration_attempt_id}"
        )

    checkpoint = checkpoints.load_optional(record.run_id, segment_id)
    archived_files: list[ArchivedRegenerationFile] = []
    try:
        staging.mkdir(parents=True)
        segment_directory = checkpoints.segment_directory(record.run_id, segment_id)
        if segment_directory.is_dir():
            _copy_directory_evidence(
                segment_directory,
                staging / "segment",
                run_directory=run_directory,
                archive_root=staging,
                output=archived_files,
            )
        for filename in FINAL_EVIDENCE_FILENAMES:
            source = run_directory / filename
            if source.is_file():
                destination = staging / "final" / filename
                _copy_file_evidence(
                    source,
                    destination,
                    run_directory=run_directory,
                    archive_root=staging,
                    output=archived_files,
                )

        archive = SegmentRegenerationArchive(
            regeneration_attempt_id=regeneration_attempt_id,
            run_id=record.run_id,
            segment_id=segment_id,
            requested_at=requested_at,
            original_status=record.status,
            original_execution_attempt_id=(
                None if record.execution is None else record.execution.attempt_id
            ),
            checkpoint_status=None if checkpoint is None else checkpoint.status,
            files=tuple(archived_files),
        )
        _write_bytes(
            staging / REGENERATION_RECORD_FILENAME,
            (archive.model_dump_json(indent=2) + "\n").encode("utf-8"),
        )
        staging.replace(archive_directory)
    except (OSError, ValueError) as error:
        raise SegmentRegenerationError(
            f"Could not archive regeneration evidence for {record.run_id}: {error}"
        ) from error
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return archive_directory


def _copy_directory_evidence(
    source: Path,
    destination: Path,
    *,
    run_directory: Path,
    archive_root: Path,
    output: list[ArchivedRegenerationFile],
) -> None:
    for path in sorted(source.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Evidence contains an unsafe symbolic link: {path}")
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        _copy_file_evidence(
            path,
            destination / relative,
            run_directory=run_directory,
            archive_root=archive_root,
            output=output,
        )


def _copy_file_evidence(
    source: Path,
    destination: Path,
    *,
    run_directory: Path,
    archive_root: Path,
    output: list[ArchivedRegenerationFile],
) -> None:
    if source.is_symlink():
        raise ValueError(f"Evidence contains an unsafe symbolic link: {source}")
    payload = source.read_bytes()
    destination.parent.mkdir(parents=True, exist_ok=True)
    _write_bytes(destination, payload)
    output.append(
        ArchivedRegenerationFile(
            source=source.relative_to(run_directory).as_posix(),
            archived_as=destination.relative_to(archive_root).as_posix(),
            byte_count=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
    )


def _manual_regeneration_record(
    record: RunRecord,
    *,
    checkpoint_store: SegmentCheckpointStore,
    segment_id: str,
    requested_at: datetime,
) -> RunRecord:
    recovery = record.recovery
    assert recovery is not None
    checkpoint = checkpoint_store.load_optional(record.run_id, segment_id)
    completed = recovery.speech_segments_completed
    if checkpoint is not None and checkpoint.status is CheckpointStatus.COMPLETED:
        completed = max(0, completed - 1)
    completed = min(completed, max(0, recovery.speech_segments_total - 1))
    updated_recovery = RunRecovery.model_validate(
        {
            **recovery.model_dump(),
            "speech_segments_completed": completed,
            "failed_segment_id": None,
        }
    )
    message = f"Manual regeneration requested for speech segment {segment_id}."
    if record.execution is None:
        execution = RunExecution(
            stage=RunStage.SYNTHESIZING,
            current_segment_id=segment_id,
            interruption_kind="manual_regeneration",
            message=message,
        )
    else:
        execution = record.execution.finish(
            stage=RunStage.SYNTHESIZING,
            current_segment_id=segment_id,
            interruption_kind="manual_regeneration",
            message=message,
        )
    return record.transition(
        RunStatus.INTERRUPTED,
        updated_at=requested_at,
        recovery=updated_recovery,
        execution=execution,
    )


def _write_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
