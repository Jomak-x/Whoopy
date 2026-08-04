"""Bounded structured event history for durable run diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from whoopy.pipeline.logs import DEFAULT_LOG_MAX_BYTES, RotatingRunLog
from whoopy.pipeline.runs import RunStage, RunStore


class RunEvent(BaseModel):
    """One inspectable state change or recovery event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID = Field(default_factory=uuid4)
    run_id: UUID
    occurred_at: AwareDatetime
    kind: str = Field(min_length=1, max_length=100)
    stage: RunStage | None = None
    attempt_id: UUID | None = None
    segment_id: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=2_000)
    details: dict[str, Any] = Field(default_factory=dict)


class RunEventLog:
    """Append valid JSONL events and retain one complete rotated predecessor."""

    def __init__(
        self,
        store: RunStore,
        *,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    ) -> None:
        self._log = RotatingRunLog(store, filename="events.jsonl", max_bytes=max_bytes)

    def append(self, event: RunEvent) -> None:
        encoded = (event.model_dump_json() + "\n").encode("utf-8")
        self._log.append_line(event.run_id, encoded)

    def record(
        self,
        run_id: UUID,
        *,
        occurred_at: datetime,
        kind: str,
        message: str,
        stage: RunStage | None = None,
        attempt_id: UUID | None = None,
        segment_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> RunEvent:
        event = RunEvent(
            run_id=run_id,
            occurred_at=occurred_at,
            kind=kind,
            stage=stage,
            attempt_id=attempt_id,
            segment_id=segment_id,
            message=message,
            details={} if details is None else details,
        )
        self.append(event)
        return event
