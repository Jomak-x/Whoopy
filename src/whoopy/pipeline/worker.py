"""The smallest local worker that turns a queued run into a timeline."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from whoopy.pipeline.runs import (
    TIMELINE_FILENAME,
    RunRecord,
    RunStatus,
    RunStore,
    RunStoreError,
)
from whoopy.timeline import Timeline, build_prompt_timeline

Clock = Callable[[], datetime]
TimelineBuilder = Callable[[RunRecord, datetime], Timeline]


class WorkerError(RuntimeError):
    """Raised when a run cannot be claimed or processed."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_timeline_builder(record: RunRecord, created_at: datetime) -> Timeline:
    return build_prompt_timeline(
        run_id=record.run_id,
        prompt=record.prompt,
        created_at=created_at,
    )


class LocalWorker:
    """Process one explicitly selected run in the foreground.

    Phase 1 is intentionally single-worker and has no polling daemon or queue
    lease. The durable state transition is established now; concurrency control
    arrives with the real background queue.
    """

    def __init__(
        self,
        store: RunStore,
        *,
        clock: Clock = _utc_now,
        timeline_builder: TimelineBuilder = _default_timeline_builder,
    ) -> None:
        self.store = store
        self.clock = clock
        self.timeline_builder = timeline_builder

    def process(self, run_id: UUID | str) -> RunRecord:
        """Move one queued run through running to completed or failed."""

        record = self.store.load(run_id)
        if record.status is not RunStatus.QUEUED:
            raise WorkerError(
                f"Run {record.run_id} is {record.status.value}; only queued runs can be processed"
            )

        running = record.transition(
            RunStatus.RUNNING,
            updated_at=self.clock(),
        )
        self.store.save(running)

        try:
            completed_at = self.clock()
            timeline = self.timeline_builder(running, completed_at)
            self.store.write_timeline(running.run_id, timeline)
            completed = running.transition(
                RunStatus.COMPLETED,
                updated_at=completed_at,
                timeline_artifact=TIMELINE_FILENAME,
            )
            self.store.save(completed)
        except Exception as error:
            failed = running.transition(
                RunStatus.FAILED,
                updated_at=self.clock(),
                error=f"{type(error).__name__}: {error}",
            )
            try:
                self.store.save(failed)
            except RunStoreError as save_error:
                raise WorkerError(
                    f"Run {running.run_id} failed and its failure state could not be saved: "
                    f"{save_error}"
                ) from error
            raise WorkerError(f"Run {running.run_id} failed: {error}") from error

        return completed
