from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.pipeline.runs import RunStatus, RunStore
from whoopy.pipeline.worker import LocalWorker, WorkerError
from whoopy.timeline import Timeline

RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _clock(values: Iterator[datetime]) -> datetime:
    return next(values)


def test_worker_completes_run_and_writes_valid_timeline(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    worker = LocalWorker(store, clock=lambda: START + timedelta(seconds=1))

    completed = worker.process(RUN_ID)

    assert completed.status is RunStatus.COMPLETED
    assert completed.timeline_artifact == "timeline.json"
    assert completed.audio_artifact == "narration.wav"
    assert completed.audio_manifest_artifact == "audio-manifest.json"
    assert completed.quality_artifact == "quality.json"
    assert completed.recovery is not None
    assert completed.recovery.speech_segments_completed == 2
    assert completed.recovery.cache_misses == 2
    assert completed.error is None
    assert store.load(RUN_ID) == completed

    timeline = store.load_timeline(RUN_ID)
    assert timeline.run_id == RUN_ID
    assert timeline.source == "phase_2_fixture_meditation"
    assert timeline.segments[0].type == "SPEECH"
    assert timeline.segments[0].text == "Breathe slowly."
    assert timeline.segments[1].type == "SILENCE"

    assert store.audio_path(RUN_ID).is_file()
    assert store.audio_manifest_path(RUN_ID).is_file()
    assert store.quality_path(RUN_ID).is_file()
    assert store.audio_path(RUN_ID).read_bytes().startswith(b"RIFF")


def test_worker_refuses_to_process_a_completed_run_twice(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    worker = LocalWorker(store, clock=lambda: START + timedelta(seconds=1))
    worker.process(RUN_ID)

    with pytest.raises(WorkerError, match="only queued runs"):
        worker.process(RUN_ID)


def test_worker_saves_failed_state_when_timeline_building_fails(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    timestamps = iter(
        [
            START + timedelta(seconds=1),
            START + timedelta(seconds=2),
            START + timedelta(seconds=3),
        ]
    )

    def fail_to_build_timeline(_record: object, _created_at: datetime) -> Timeline:
        raise RuntimeError("fixture builder failed")

    worker = LocalWorker(
        store,
        clock=lambda: _clock(timestamps),
        timeline_builder=fail_to_build_timeline,
    )

    with pytest.raises(WorkerError, match="fixture builder failed"):
        worker.process(RUN_ID)

    failed = store.load(RUN_ID)
    assert failed.status is RunStatus.FAILED
    assert failed.error == "RuntimeError: fixture builder failed"
    assert failed.timeline_artifact is None
    assert not store.timeline_path(RUN_ID).exists()
