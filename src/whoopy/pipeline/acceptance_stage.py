"""Opt-in production stage observation for destructive acceptance testing.

Normal product runs never set these environment variables.  The Local V1
acceptance harness uses them to stop a real worker only after its authoritative
``run.json`` has committed the requested stage.  A structured, atomically
written marker makes the interruption target auditable and prevents timing
based tests from claiming that they stopped the intended work.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

STAGE_READY_FILE_ENV = "LOCAL_V1_ACCEPTANCE_STAGE_READY_FILE"
PAUSE_STAGE_ENV = "LOCAL_V1_ACCEPTANCE_PAUSE_STAGE"


class AcceptanceStageMarker(BaseModel):
    """Evidence that a particular process committed a durable production stage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    run_id: UUID
    stage: str = Field(min_length=1)
    process_id: int = Field(ge=1)
    observed_at: datetime
    durable_run_status: str
    durable_run_stage: str


def observe_acceptance_stage(
    *,
    run_id: UUID,
    stage: str,
    durable_run_status: str,
    durable_run_stage: str,
) -> None:
    """Publish and optionally pause at one exact, already-durable stage.

    The marker path and requested pause stage are accepted only together.  This
    keeps an accidentally inherited marker variable from adding I/O to ordinary
    runs.  The wait is deliberately unbounded: the acceptance parent owns the
    child and terminates it as the destructive action under test.
    """

    marker_value = os.environ.get(STAGE_READY_FILE_ENV)
    pause_stage = os.environ.get(PAUSE_STAGE_ENV)
    if not marker_value or not pause_stage or stage != pause_stage:
        return
    marker_path = Path(marker_value).expanduser().resolve()
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker = AcceptanceStageMarker(
        run_id=run_id,
        stage=stage,
        process_id=os.getpid(),
        observed_at=datetime.now(UTC),
        durable_run_status=durable_run_status,
        durable_run_stage=durable_run_stage,
    )
    temporary = marker_path.with_name(f".{marker_path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(marker.model_dump_json(indent=2).encode("utf-8") + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, marker_path)
        _fsync_directory(marker_path.parent)
    finally:
        temporary.unlink(missing_ok=True)

    while True:
        time.sleep(1)


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
