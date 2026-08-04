from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.audio.models import PcmAudio
from whoopy.pipeline.checkpoints import SegmentCheckpointStore
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.regeneration import (
    SegmentRegenerationError,
    prepare_segment_regeneration,
)
from whoopy.pipeline.runs import (
    AUDIO_FILENAME,
    AUDIO_MANIFEST_FILENAME,
    QUALITY_FILENAME,
    TIMELINE_FILENAME,
    InvalidRunIdError,
    RunExecution,
    RunRecovery,
    RunStage,
    RunStatus,
    RunStore,
    RunStoreError,
)
from whoopy.timeline import build_fixture_timeline

RUN_ID = UUID("77777777-7777-4777-8777-777777777777")
REGENERATION_ID = UUID("88888888-8888-4888-8888-888888888888")
START = datetime(2026, 8, 3, 12, 0, tzinfo=UTC)


def _completed_run(store: RunStore) -> None:
    queued = store.create("A test meditation.", run_id=RUN_ID, created_at=START)
    timeline = build_fixture_timeline(
        run_id=RUN_ID,
        prompt=queued.prompt,
        created_at=START,
    )
    store.write_timeline(RUN_ID, timeline)
    store.audio_path(RUN_ID).write_bytes(b"old-wave")
    store.audio_manifest_path(RUN_ID).write_bytes(b'{"old":"manifest"}')
    store.quality_path(RUN_ID).write_bytes(b'{"old":"quality"}')
    recovery = RunRecovery(
        process_attempts=1,
        resume_count=0,
        cache_hits=0,
        cache_misses=2,
        checkpoint_reuses=0,
        speech_segments_total=2,
        speech_segments_completed=2,
    )
    completed = queued.transition(
        RunStatus.COMPLETED,
        updated_at=START,
        timeline_artifact=TIMELINE_FILENAME,
        audio_artifact=AUDIO_FILENAME,
        audio_manifest_artifact=AUDIO_MANIFEST_FILENAME,
        quality_artifact=QUALITY_FILENAME,
        recovery=recovery,
        execution=RunExecution(stage=RunStage.COMPLETED),
    )
    store.save(completed)
    audio = PcmAudio(pcm_s16le=b"\x01\x00" * 240, sample_rate=24_000)
    checkpoint = SegmentCheckpointStore.completed_checkpoint(
        run_id=RUN_ID,
        segment_id="speech-0001",
        cache_key="a" * 64,
        audio=audio,
        attempt_count=1,
        cache_hit=False,
        started_at=START,
        completed_at=START,
    )
    SegmentCheckpointStore(store).save(checkpoint, audio=audio)


def test_completed_run_is_archived_and_prepared_for_cache_bypassing_resume(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    _completed_run(store)

    prepared = prepare_segment_regeneration(
        store,
        RUN_ID,
        "speech-0001",
        requested_at=START + timedelta(minutes=1),
        regeneration_attempt_id=REGENERATION_ID,
    )

    assert prepared.run_id == RUN_ID
    assert prepared.segment_id == "speech-0001"
    assert prepared.bypass_checkpoint is True
    assert prepared.bypass_shared_cache is True
    assert prepared.bypass_cache_segment_ids == frozenset(("speech-0001",))
    assert prepared.record.status is RunStatus.INTERRUPTED
    assert prepared.record.timeline_artifact is None
    assert prepared.record.audio_artifact is None
    assert prepared.record.audio_manifest_artifact is None
    assert prepared.record.quality_artifact is None
    assert prepared.record.error is None
    assert prepared.record.execution is not None
    assert prepared.record.execution.interruption_kind == "manual_regeneration"
    assert prepared.record.execution.current_segment_id == "speech-0001"
    assert prepared.record.recovery is not None
    assert prepared.record.recovery.speech_segments_completed == 1
    assert store.load(RUN_ID) == prepared.record

    # Preparation copies evidence rather than deleting it.  The resume path can
    # overwrite the live files only after this durable snapshot exists.
    assert store.audio_path(RUN_ID).read_bytes() == b"old-wave"
    assert SegmentCheckpointStore(store).checkpoint_path(RUN_ID, "speech-0001").is_file()
    assert (prepared.archive_directory / "segment/checkpoint.json").is_file()
    assert (prepared.archive_directory / "segment/audio.pcm").is_file()
    assert (prepared.archive_directory / "final/narration.wav").read_bytes() == b"old-wave"
    assert (prepared.archive_directory / "final/audio-manifest.json").is_file()
    assert (prepared.archive_directory / "final/quality.json").is_file()
    request = json.loads((prepared.archive_directory / "request.json").read_text(encoding="utf-8"))
    assert request["regeneration_attempt_id"] == str(REGENERATION_ID)
    assert request["original_status"] == "completed"
    assert request["checkpoint_status"] == "completed"
    assert request["bypass_checkpoint"] is True
    assert request["bypass_shared_cache"] is True
    assert {entry["source"] for entry in request["files"]} == {
        "segments/speech-0001/audio.pcm",
        "segments/speech-0001/checkpoint.json",
        "narration.wav",
        "audio-manifest.json",
        "quality.json",
    }


@pytest.mark.parametrize("status", [RunStatus.FAILED, RunStatus.INTERRUPTED])
def test_stopped_incomplete_run_can_prepare_regeneration(
    tmp_path: Path,
    status: RunStatus,
) -> None:
    store = RunStore(tmp_path / status.value)
    _completed_run(store)
    completed = store.load(RUN_ID)
    recovery = completed.recovery
    assert recovery is not None
    stopped_recovery = RunRecovery.model_validate(
        {
            **recovery.model_dump(),
            "speech_segments_completed": 1,
            "failed_segment_id": "speech-0002" if status is RunStatus.FAILED else None,
        }
    )
    execution = RunExecution(
        stage=RunStage.SYNTHESIZING,
        current_segment_id="speech-0002",
        interruption_kind=("test_interruption" if status is RunStatus.INTERRUPTED else None),
        message="Stopped for test.",
    )
    stopped = completed.transition(
        status,
        updated_at=START,
        recovery=stopped_recovery,
        execution=execution,
        error="test failure" if status is RunStatus.FAILED else None,
    )
    store.save(stopped)

    prepared = prepare_segment_regeneration(
        store,
        RUN_ID,
        "speech-0002",
        requested_at=START + timedelta(minutes=1),
        regeneration_attempt_id=REGENERATION_ID,
    )

    assert prepared.record.status is RunStatus.INTERRUPTED
    assert prepared.record.error is None
    assert prepared.record.recovery is not None
    assert prepared.record.recovery.failed_segment_id is None
    assert prepared.record.execution is not None
    assert prepared.record.execution.interruption_kind == "manual_regeneration"


@pytest.mark.parametrize("segment_id", ["../run.json", "speech/0001", "", ".hidden"])
def test_unsafe_segment_id_is_rejected_without_creating_history(
    tmp_path: Path,
    segment_id: str,
) -> None:
    store = RunStore(tmp_path / "runs")
    _completed_run(store)

    with pytest.raises(ValueError, match="Invalid segment ID"):
        prepare_segment_regeneration(store, RUN_ID, segment_id, requested_at=START)

    assert not (store.run_directory(RUN_ID) / "history").exists()


@pytest.mark.parametrize(
    ("segment_id", "message"),
    [
        ("silence-0001", "not a speech segment"),
        ("speech-9999", "not present"),
    ],
)
def test_unknown_or_non_speech_segment_is_rejected(
    tmp_path: Path,
    segment_id: str,
    message: str,
) -> None:
    store = RunStore(tmp_path / "runs")
    _completed_run(store)

    with pytest.raises(SegmentRegenerationError, match=message):
        prepare_segment_regeneration(store, RUN_ID, segment_id, requested_at=START)

    assert not (store.run_directory(RUN_ID) / "history").exists()
    assert store.load(RUN_ID).status is RunStatus.COMPLETED


def test_active_run_ownership_is_refused_without_archiving(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    _completed_run(store)
    completed = store.load(RUN_ID)
    recovery = completed.recovery
    assert recovery is not None
    active_execution = RunExecution(
        stage=RunStage.SYNTHESIZING,
        attempt_id=UUID("99999999-9999-4999-8999-999999999999"),
        owner_id="test-worker",
        pid=123,
        started_at=START,
        heartbeat_at=START,
        lease_expires_at=START + timedelta(seconds=15),
    )
    running = completed.transition(
        RunStatus.RUNNING,
        updated_at=START,
        recovery=recovery,
        execution=active_execution,
    )
    store.save(running)

    with pytest.raises(SegmentRegenerationError, match="active worker"):
        prepare_segment_regeneration(store, RUN_ID, "speech-0001", requested_at=START)

    assert not (store.run_directory(RUN_ID) / "history").exists()
    assert store.load(RUN_ID).status is RunStatus.RUNNING


def test_held_run_lock_is_refused_without_archiving(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    _completed_run(store)
    lock = RunLock(store.run_directory(RUN_ID))
    lock.acquire()
    try:
        with pytest.raises(SegmentRegenerationError, match="active worker"):
            prepare_segment_regeneration(store, RUN_ID, "speech-0001", requested_at=START)
    finally:
        lock.release()

    assert not (store.run_directory(RUN_ID) / "history").exists()
    assert store.load(RUN_ID).status is RunStatus.COMPLETED


def test_record_save_failure_keeps_original_authoritative_and_archive_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RunStore(tmp_path / "runs")
    _completed_run(store)
    original_record = store.load(RUN_ID)

    def fail_save(_record: object) -> None:
        raise RunStoreError("simulated commit failure")

    monkeypatch.setattr(store, "save", fail_save)
    with pytest.raises(RunStoreError, match="simulated commit failure"):
        prepare_segment_regeneration(
            store,
            RUN_ID,
            "speech-0001",
            requested_at=START,
            regeneration_attempt_id=REGENERATION_ID,
        )

    # Archive-first preparation cannot leave a record pointing at missing data.
    assert store.load(RUN_ID) == original_record
    assert store.audio_path(RUN_ID).read_bytes() == b"old-wave"
    archive = store.run_directory(RUN_ID) / "history" / "manual-regeneration" / str(REGENERATION_ID)
    assert (archive / "request.json").is_file()
    assert (archive / "segment/checkpoint.json").is_file()


def test_invalid_run_id_is_rejected_before_any_path_is_used(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")

    with pytest.raises(InvalidRunIdError, match="Invalid run ID"):
        prepare_segment_regeneration(store, "../../outside", "speech-0001")
