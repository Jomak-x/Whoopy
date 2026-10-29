from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from tests.test_takes import RUN_ID, _alternative_take, _completed_run, _pending_alternative
from tests.test_task_finalization import _application, _write_export
from tests.test_webui_review_boundaries import _rendered_script_run
from whoopy.audio import AudioManifest, AudioQualityReport
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.processing import ProcessedSpeechSynthesizer
from whoopy.pipeline.checkpoints import SegmentCheckpointStore
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.regeneration import SegmentRegenerationError, prepare_segment_regeneration
from whoopy.pipeline.revisions import RevisionStore
from whoopy.pipeline.runs import (
    RunExecution,
    RunRecord,
    RunStage,
    RunStatus,
    RunStore,
    RunStoreError,
)
from whoopy.pipeline.takes import PendingAlternativeTake, SegmentTakeStore
from whoopy.pipeline.worker import LocalWorker, WorkerError
from whoopy.webui.server import GenerationTask, LocalWebApplication


def test_run_summary_keeps_audio_and_status_on_the_same_task_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    _store, run_id = _rendered_script_run(application)
    task = GenerationTask(task_id=run_id, run_id=run_id)
    application._tasks[run_id] = task
    current_audio = application._current_audio

    def finish_after_audio_read(
        record: RunRecord, **kwargs: Any
    ) -> tuple[Path, AudioManifest, AudioQualityReport] | None:
        audio = current_audio(record, **kwargs)
        with application._task_lock:
            task.launching = False
        return audio

    monkeypatch.setattr(application, "_current_audio", finish_after_audio_read)
    finishing = application.run(run_id)
    assert finishing is not None
    assert finishing["status"] == "running"
    assert finishing["has_audio"] is False
    completed = application.run(run_id)
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["has_audio"] is True


def _assembly_launcher(store: RunStore) -> type[Any]:
    class Process:
        returncode: int | None = None

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            assert command[4] == "resume"

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self) -> tuple[str, str]:
            LocalWorker(
                store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
            ).resume(RUN_ID)
            self.returncode = 0
            return "{}", ""

    return Process


def _wait_status(application: LocalWebApplication, status: str) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = application.run(str(RUN_ID))
        assert result is not None
        if result["status"] == status:
            return result
        time.sleep(0.01)
    raise AssertionError(f"Run did not reach {status}: {result}")


@pytest.mark.parametrize(
    "crash_point", ["generating", "captured", "restoring", "restored", "assembling"]
)
def test_restart_reconciles_alternative_without_selecting_temporary_audio(
    tmp_path: Path, crash_point: str
) -> None:
    store, original_audio = _completed_run(tmp_path / "runs")
    selected, _existing, alternative_audio = _alternative_take(store)
    pending = _pending_alternative(store, selected)
    takes = SegmentTakeStore(store)
    store.audio_path(RUN_ID).write_bytes(b"temporary alternative master")
    _write_export(store, b"temporary alternative master")
    if crash_point != "generating":
        generated = takes.capture_active(
            RUN_ID,
            pending.segment_id,
            source="production_regeneration",
            regeneration_attempt_id=pending.operation_id,
            select_if_unset=False,
        )
        if crash_point != "captured":
            pending = pending.model_copy(
                update={"phase": "restoring", "generated_take_id": generated.take_id}
            )
            takes.save_pending(pending)
        if crash_point in {"restored", "assembling"}:
            takes.choose(RUN_ID, pending.segment_id, selected, action="restore")
        if crash_point == "assembling":
            prior = store.load(RUN_ID).execution
            assert prior is not None
            revision = RevisionStore(store).create_assembly_revision(RUN_ID, note="Selected take")
            pending = pending.model_copy(
                update={
                    "phase": "assembling",
                    "assembly_revision_id": revision.revision_id,
                    "assembly_prior_attempt_id": prior.attempt_id,
                }
            )
            takes.save_pending(pending)
    application = _application(tmp_path, command_launcher=_assembly_launcher(store))
    with RunLock(store.run_directory(RUN_ID)):
        completed = store.load(RUN_ID)
        if crash_point == "generating":
            assert completed.execution is not None
            now = datetime.now(UTC)
            store.save(
                completed.transition(
                    RunStatus.RUNNING,
                    updated_at=now,
                    execution=RunExecution(
                        stage=RunStage.SYNTHESIZING,
                        attempt_id=completed.execution.attempt_id,
                        owner_id="test-worker",
                        pid=1,
                        started_at=now,
                        heartbeat_at=now,
                        lease_expires_at=now + timedelta(seconds=15),
                    ),
                )
            )
        state = application.run(str(RUN_ID))
        assert state is not None and state["status"] == "running"
        assert state["has_audio"] is False
        assert application.audio_path(str(RUN_ID)) is None
        assert application.exports(str(RUN_ID)) == {"available": False, "artifacts": []}
        with pytest.raises(ValueError, match="active"):
            application.resume_run(str(RUN_ID))
        with pytest.raises(ValueError, match="active"):
            application.delete_run(str(RUN_ID))
        with (
            pytest.raises(ValueError, match="active meditation"),
            application._admit_model_operation(),
        ):
            raise AssertionError("Model operation was incorrectly admitted")
        if crash_point == "generating":
            store.save(completed)
    state = _wait_status(application, "completed")
    assert state["has_audio"] is True
    assert takes.pending(RUN_ID) is None
    assert takes.selected(RUN_ID, pending.segment_id) == selected
    assert SegmentCheckpointStore(store).audio_path(RUN_ID, pending.segment_id).read_bytes() == (
        original_audio.pcm_s16le
    )
    generated_takes = [
        value
        for value in takes.list(RUN_ID, pending.segment_id)
        if value.regeneration_attempt_id == pending.operation_id
    ]
    assert len(generated_takes) == 1
    assert generated_takes[0].pcm_sha256 == hashlib.sha256(alternative_audio.pcm_s16le).hexdigest()
    assert application.audio_path(str(RUN_ID)) is not None
    assert application.exports(str(RUN_ID)) == {"available": False, "artifacts": []}
    archived = (
        store.run_directory(RUN_ID)
        / "history"
        / "alternative-takes"
        / f"{pending.operation_id}.json"
    )
    receipt = PendingAlternativeTake.model_validate_json(archived.read_text())
    assert receipt.phase == "assembling"
    assert receipt.selected_take_id == selected


@pytest.mark.parametrize("archive_prepared", [False, True])
def test_restart_before_regeneration_commit_requires_explicit_retained_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive_prepared: bool
) -> None:
    store, _original_audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    selected = takes.capture_active(RUN_ID, "speech-0001", source="original")
    pending = PendingAlternativeTake(
        operation_id=uuid4(),
        run_id=RUN_ID,
        segment_id="speech-0001",
        selected_take_id=selected.take_id,
    )
    takes.save_pending(pending)
    if archive_prepared:

        def stop_before_commit(_record: object) -> None:
            raise RunStoreError("Stopped before regeneration commit")

        with monkeypatch.context() as patch:
            patch.setattr(store, "save", stop_before_commit)
            with pytest.raises(RunStoreError, match="before regeneration commit"):
                prepare_segment_regeneration(
                    store, RUN_ID, pending.segment_id, regeneration_attempt_id=pending.operation_id
                )
    application = _application(tmp_path, command_launcher=_assembly_launcher(store))
    state = _wait_status(application, "failed")
    assert not state["has_audio"] and not state["resumable"]
    assert application.audio_path(str(RUN_ID)) is None
    assert len(takes.list(RUN_ID, pending.segment_id)) == 1
    with pytest.raises(ValueError, match="Choose a retained take"):
        application.generate_alternative_take(str(RUN_ID), pending.segment_id)
    with pytest.raises(WorkerError, match="Choose a retained take"):
        LocalWorker(store).resume(RUN_ID)
    result = application.choose_segment_take(str(RUN_ID), pending.segment_id, selected.take_id)
    assert result is not None
    _wait_status(application, "completed")
    assert takes.selected(RUN_ID, pending.segment_id) == selected.take_id
    assert takes.pending(RUN_ID) is None


def test_unrelated_regeneration_cannot_replace_pending_alternative(tmp_path: Path) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    selected, _existing, _alternative = _alternative_take(store)
    pending = _pending_alternative(store, selected)
    before = store.load(RUN_ID)
    with pytest.raises(SegmentRegenerationError, match="pending alternative"):
        prepare_segment_regeneration(store, RUN_ID, pending.segment_id)
    assert store.load(RUN_ID) == before
    assert SegmentTakeStore(store).pending(RUN_ID) == pending


def test_successful_worker_recovery_clears_historical_web_task_error(tmp_path: Path) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    RevisionStore(store).create_assembly_revision(RUN_ID, note="Committed edited plan")
    record = store.load(RUN_ID)
    store.save(
        record.transition(
            RunStatus.FAILED,
            updated_at=datetime.now(UTC),
            execution=RunExecution(stage=RunStage.SYNTHESIZING),
            error="Speech renderer stopped",
        )
    )
    application = _application(tmp_path)
    task = GenerationTask(
        task_id=str(RUN_ID),
        run_id=str(RUN_ID),
        mode="plan_regeneration",
        launching=False,
        error="Speech renderer stopped",
    )
    application._tasks[str(RUN_ID)] = task
    failed_state = application.run(str(RUN_ID))
    assert failed_state is not None and failed_state["status"] == "failed"
    LocalWorker(store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())).resume(
        RUN_ID
    )
    state = application.run(str(RUN_ID))
    assert state is not None and state["status"] == "completed"
    assert state["error"] is None
    assert state["has_audio"] is True
    assert task.error == "Speech renderer stopped"
    assert application.audio_path(str(RUN_ID)) is not None


def test_restart_with_expired_generation_lease_keeps_selected_take_recoverable(
    tmp_path: Path,
) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    selected, _existing, _alternative = _alternative_take(store)
    pending = _pending_alternative(store, selected)
    expired = datetime.now(UTC) - timedelta(minutes=1)
    record = store.load(RUN_ID)
    store.save(
        record.transition(
            RunStatus.RUNNING,
            updated_at=expired,
            execution=RunExecution(
                stage=RunStage.SYNTHESIZING,
                attempt_id=uuid4(),
                owner_id="stopped-worker",
                pid=999999,
                started_at=expired,
                heartbeat_at=expired,
                lease_expires_at=expired + timedelta(seconds=15),
            ),
        )
    )
    application = _application(tmp_path)
    state = _wait_status(application, "failed")
    assert state["has_audio"] is False
    assert state["resumable"] is False
    retained = SegmentTakeStore(store).pending(RUN_ID)
    assert retained is not None and retained.phase == "failed"
    assert retained.selected_take_id == pending.selected_take_id
    assert application.audio_path(str(RUN_ID)) is None
