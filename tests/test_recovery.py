from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.synthesis import FatalSynthesisError, TransientSynthesisError
from whoopy.pipeline.cache import SegmentCache
from whoopy.pipeline.checkpoints import CheckpointStatus, SegmentCheckpointStore
from whoopy.pipeline.events import RunEventLog
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.logs import WorkerLog
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

    def close(self) -> None:
        """Match the production synthesizer lifecycle contract."""


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


class ClosableFatalSynthesizer(FatalSynthesizer):
    def __init__(self, *, failing_segment: str, failures_remaining: int) -> None:
        super().__init__(
            failing_segment=failing_segment,
            failures_remaining=failures_remaining,
        )
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


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
    assert interrupted.status is RunStatus.INTERRUPTED
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


def test_worker_closes_synthesizer_after_failure(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = ClosableFatalSynthesizer(
        failing_segment="speech-0002",
        failures_remaining=0,
    )
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)

    with pytest.raises(WorkerError):
        _worker(store, cache, synthesizer).process(RUN_ID)

    assert synthesizer.close_calls == 1


def test_event_log_failure_does_not_change_completion_or_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = ClosableFatalSynthesizer(
        failing_segment="never",
        failures_remaining=0,
    )
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    worker = _worker(store, cache, synthesizer)

    def fail_event(*_args: object, **_values: object) -> None:
        raise OSError("simulated diagnostic disk failure")

    monkeypatch.setattr(worker.events, "record", fail_event)

    completed = worker.process(RUN_ID)

    assert completed.status is RunStatus.COMPLETED
    assert store.load(RUN_ID).status is RunStatus.COMPLETED
    assert synthesizer.close_calls == 1


def test_run_lock_rejects_a_second_worker_in_the_same_process(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe.", run_id=RUN_ID, created_at=START)
    first = RunLock(store.run_directory(RUN_ID))
    second = RunLock(store.run_directory(RUN_ID))

    with first, pytest.raises(RunLockUnavailable, match="Another worker"):
        second.acquire()


def test_event_log_rotates_valid_json_lines_without_discarding_old_events(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe.", run_id=RUN_ID, created_at=START)
    events = RunEventLog(store, max_bytes=500)

    for number in range(5):
        events.record(
            RUN_ID,
            occurred_at=START,
            kind="test_event",
            message=f"event {number}: " + "calm " * 30,
        )

    active = store.events_path(RUN_ID)
    backup = active.with_name("events.jsonl.1")
    assert active.stat().st_size <= 500
    assert backup.stat().st_size <= 500
    documents: list[dict[str, str]] = []
    for path in (backup, active):
        documents.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    assert [document["message"].split(":")[0] for document in documents] == ["event 3", "event 4"]


def test_event_log_serializes_threaded_appends_without_losing_json_lines(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe.", run_id=RUN_ID, created_at=START)
    events = RunEventLog(store, max_bytes=100_000)

    def append(number: int) -> None:
        events.record(
            RUN_ID,
            occurred_at=START,
            kind="concurrent",
            message=f"event {number}",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(append, range(32)))

    documents = [
        json.loads(line)
        for line in store.events_path(RUN_ID).read_text(encoding="utf-8").splitlines()
    ]
    actual_messages = {document["message"] for document in documents}
    expected_messages = {f"event {number}" for number in range(32)}
    assert actual_messages == expected_messages


def test_worker_log_normalizes_lines_and_rotates_with_one_backup(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe.", run_id=RUN_ID, created_at=START)
    log = WorkerLog(store, max_bytes=150, maximum_line_characters=90)

    for number in range(4):
        log.record(
            RUN_ID,
            occurred_at=START,
            source=" model stderr ",
            level="error",
            message=f"line {number}\nwith extra spacing " + "x" * 100,
        )

    active = log.path(RUN_ID)
    backup = active.with_name("worker.log.1")
    assert active.stat().st_size <= 150
    assert backup.stat().st_size <= 150
    lines = backup.read_text(encoding="utf-8").splitlines()
    lines.extend(active.read_text(encoding="utf-8").splitlines())
    assert all("\n" not in line for line in lines)
    assert all("ERROR [model stderr]" in line for line in lines)
    assert any("line 3 with extra spacing" in line for line in lines)
