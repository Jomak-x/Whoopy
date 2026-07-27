from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.synthesis import FatalSynthesisError, TransientSynthesisError
from whoopy.pipeline.cache import SegmentCache
from whoopy.pipeline.checkpoints import CheckpointStatus, SegmentCheckpointStore
from whoopy.pipeline.runs import RunStatus, RunStore
from whoopy.pipeline.worker import LocalWorker, RetryPolicy, WorkerError
from whoopy.timeline import SpeechSegment

RUN_ID = UUID("66666666-6666-4666-8666-666666666666")
START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class SwitchableSynthesizer:
    metadata = FixtureSpeechSynthesizer.metadata.model_copy(
        update={"adapter_id": "tests.switchable_fixture"}
    )
    cache_identity: str = metadata.cache_identity
    sample_rate: int = 24_000

    def __init__(self, *, failing_segment: str, failures_remaining: int) -> None:
        self.failing_segment = failing_segment
        self.failures_remaining = failures_remaining
        self.calls: list[str] = []
        self.fixture = FixtureSpeechSynthesizer()

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        self.calls.append(segment.id)
        if segment.id == self.failing_segment and self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise TransientSynthesisError("temporary fixture outage")
        return self.fixture.synthesize(segment)


class SimulatedInterruption(BaseException):
    pass


class InterruptOnceSynthesizer(SwitchableSynthesizer):
    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        self.calls.append(segment.id)
        if segment.id == self.failing_segment and self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise SimulatedInterruption
        return self.fixture.synthesize(segment)


class FatalSynthesizer(SwitchableSynthesizer):
    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        self.calls.append(segment.id)
        if segment.id == self.failing_segment:
            raise FatalSynthesisError("unsupported fixture voice")
        return self.fixture.synthesize(segment)


def _worker(
    store: RunStore,
    cache: SegmentCache,
    synthesizer: SwitchableSynthesizer,
    *,
    max_attempts: int = 3,
    delays: list[float] | None = None,
) -> LocalWorker:
    return LocalWorker(
        store,
        cache=cache,
        synthesizer=synthesizer,
        retry_policy=RetryPolicy(
            max_attempts=max_attempts,
            base_delay_seconds=0.25,
            max_delay_seconds=2,
        ),
        sleeper=(delays.append if delays is not None else lambda _seconds: None),
        clock=lambda: START,
    )


def test_transient_segment_failure_retries_and_retains_failure_history(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = SwitchableSynthesizer(
        failing_segment="speech-0002",
        failures_remaining=1,
    )
    delays: list[float] = []
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)

    completed = _worker(store, cache, synthesizer, delays=delays).process(RUN_ID)

    checkpoint = SegmentCheckpointStore(store).load_optional(RUN_ID, "speech-0002")
    assert completed.status is RunStatus.COMPLETED
    assert synthesizer.calls == ["speech-0001", "speech-0002", "speech-0002"]
    assert delays == [0.25]
    assert checkpoint is not None
    assert checkpoint.status is CheckpointStatus.COMPLETED
    assert checkpoint.attempt_count == 2
    assert len(checkpoint.failures) == 1
    assert checkpoint.failures[0].kind == "transient"


def test_failed_segment_resumes_without_resynthesizing_completed_segment(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = SwitchableSynthesizer(
        failing_segment="speech-0002",
        failures_remaining=2,
    )
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    worker = _worker(store, cache, synthesizer, max_attempts=2)

    with pytest.raises(WorkerError, match="exhausted 2 transient attempts"):
        worker.process(RUN_ID)

    failed = store.load(RUN_ID)
    timeline_before_resume = store.timeline_path(RUN_ID).read_bytes()
    assert failed.status is RunStatus.FAILED
    assert failed.recovery is not None
    assert failed.recovery.speech_segments_completed == 1
    assert failed.recovery.failed_segment_id == "speech-0002"
    assert synthesizer.calls == ["speech-0001", "speech-0002", "speech-0002"]

    completed = worker.resume(RUN_ID)

    assert completed.status is RunStatus.COMPLETED
    assert completed.recovery is not None
    assert completed.recovery.process_attempts == 2
    assert completed.recovery.resume_count == 1
    assert completed.recovery.checkpoint_reuses == 1
    assert synthesizer.calls == [
        "speech-0001",
        "speech-0002",
        "speech-0002",
        "speech-0002",
    ]
    assert store.timeline_path(RUN_ID).read_bytes() == timeline_before_resume
    checkpoint = SegmentCheckpointStore(store).load_optional(RUN_ID, "speech-0002")
    assert checkpoint is not None
    assert checkpoint.attempt_count == 3
    assert len(checkpoint.failures) == 2


def test_interrupted_running_run_can_resume_from_completed_checkpoint(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = InterruptOnceSynthesizer(
        failing_segment="speech-0002",
        failures_remaining=1,
    )
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    worker = _worker(store, cache, synthesizer)

    with pytest.raises(SimulatedInterruption):
        worker.process(RUN_ID)

    interrupted = store.load(RUN_ID)
    assert interrupted.status is RunStatus.RUNNING
    assert synthesizer.calls == ["speech-0001", "speech-0002"]

    completed = worker.resume(RUN_ID)

    assert completed.status is RunStatus.COMPLETED
    assert completed.recovery is not None
    assert completed.recovery.checkpoint_reuses == 1
    assert synthesizer.calls == ["speech-0001", "speech-0002", "speech-0002"]


def test_fatal_segment_failure_is_not_retried(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = FatalSynthesizer(
        failing_segment="speech-0002",
        failures_remaining=0,
    )
    delays: list[float] = []
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)

    with pytest.raises(WorkerError, match="failed fatally without retry"):
        _worker(store, cache, synthesizer, delays=delays).process(RUN_ID)

    checkpoint = SegmentCheckpointStore(store).load_optional(RUN_ID, "speech-0002")
    assert synthesizer.calls == ["speech-0001", "speech-0002"]
    assert delays == []
    assert checkpoint is not None
    assert checkpoint.status is CheckpointStatus.FAILED
    assert checkpoint.attempt_count == 1
    assert checkpoint.failures[0].kind == "fatal"
