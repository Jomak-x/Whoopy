from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.test_takes import RUN_ID, _alternative_take, _completed_run
from whoopy.pipeline.checkpoints import SegmentCheckpointStore
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.revisions import RevisionRequest, RevisionStore, TimelineEdit
from whoopy.pipeline.takes import SegmentTakeStore
from whoopy.webui.server import GenerationTask, LocalWebApplication


def _prepared_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[LocalWebApplication, str, str]:
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    store, _audio = _completed_run(application.runs_directory)
    take = SegmentTakeStore(store).capture_active(RUN_ID, "speech-0001", source="original")
    revisions = RevisionStore(store)
    revisions.create(
        RUN_ID, RevisionRequest(timeline=TimelineEdit(silence_duration_ms={"silence-0001": 2_000}))
    )
    initial_revision = revisions.list(RUN_ID)[0].revision_id
    monkeypatch.setattr(application, "_run_recovery_command", lambda *_args: None)
    return application, initial_revision, take.take_id


def _mutate(
    application: LocalWebApplication, operation: str, revision_id: str, take_id: str
) -> dict[str, Any] | None:
    if operation == "edit":
        return application.edit_run(
            str(RUN_ID), {"timeline": {"silence_duration_ms": {"silence-0001": 1_000}}}
        )
    if operation == "restore":
        return application.restore_revision(str(RUN_ID), revision_id)
    return application.choose_segment_take(str(RUN_ID), "speech-0001", take_id)


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("operation", ["edit", "restore", "take"])
@pytest.mark.parametrize("blocker", ["model", "generation"])
def test_busy_revision_request_does_not_mutate_and_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, blocker: str
) -> None:
    application, revision_id, take_id = _prepared_application(tmp_path, monkeypatch)
    if blocker == "model":
        monkeypatch.setattr(application, "_run_model_pack_task", lambda *_args: None)
        application.start_model_pack_task("smoke-test", "moss-local-5b", {})
    else:
        application._tasks["other"] = GenerationTask(task_id="other", run_id="other")
    run_directory = application.runs_directory / str(RUN_ID)
    before = _snapshot(run_directory)

    with pytest.raises(ValueError, match="active"):
        _mutate(application, operation, revision_id, take_id)

    assert _snapshot(run_directory) == before
    assert str(RUN_ID) not in application._tasks
    application._tasks.clear()
    application._model_pack_tasks.clear()
    result = _mutate(application, operation, revision_id, take_id)
    assert result is not None
    assert result["task"]["run_id"] == str(RUN_ID)
    assert application._tasks[str(RUN_ID)].active
    assert _snapshot(run_directory) != before


@pytest.mark.parametrize("operation", ["edit", "restore", "take"])
def test_revision_owns_admission_until_worker_registration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    application, revision_id, take_id = _prepared_application(tmp_path, monkeypatch)
    mutated = threading.Event()
    release = threading.Event()
    contender_started = threading.Event()
    contender_finished = threading.Event()
    original_resume = application._resume_run_admitted

    def resume(run_id: str) -> dict[str, Any] | None:
        mutated.set()
        assert release.wait(5)
        return original_resume(run_id)

    def contend() -> None:
        contender_started.set()
        try:
            with pytest.raises(ValueError, match="active"):
                application.start_model_pack_task("smoke-test", "moss-local-5b", {})
        finally:
            contender_finished.set()

    monkeypatch.setattr(application, "_resume_run_admitted", resume)
    with ThreadPoolExecutor(max_workers=2) as pool:
        mutation = pool.submit(_mutate, application, operation, revision_id, take_id)
        try:
            assert mutated.wait(5)
            contender = pool.submit(contend)
            assert contender_started.wait(5)
            assert not contender_finished.wait(0.05)
        finally:
            release.set()
        assert mutation.result(timeout=5) is not None
        contender.result(timeout=5)
    assert application._tasks[str(RUN_ID)].active
    assert application._model_pack_tasks == {}


@pytest.mark.parametrize("operation", ["generation", "model", "delete"])
def test_alternative_take_retains_admission_after_worker_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    store, original_audio = _completed_run(application.runs_directory)
    monkeypatch.setattr("whoopy.webui.server.threading.Thread.start", lambda _thread: None)
    result = application.generate_alternative_take(str(RUN_ID), "speech-0001")
    assert result is not None
    generation_task = application._tasks[str(RUN_ID)]
    _alternative_take(store)
    generation_task.launching = False
    before = _snapshot(store.run_directory(RUN_ID))

    with pytest.raises(ValueError, match="active"):
        if operation == "generation":
            application.regenerate_segment(str(RUN_ID), "speech-0001")
        elif operation == "model":
            application.start_model_pack_task("smoke-test", "moss-local-5b", {})
        else:
            application.delete_run(str(RUN_ID))

    assert _snapshot(store.run_directory(RUN_ID)) == before
    application._finalize_alternative_take(
        generation_task, "speech-0001", result["selected_take_id"]
    )
    assert application._tasks[str(RUN_ID)] is not generation_task
    assert application._tasks[str(RUN_ID)].active
    assert SegmentCheckpointStore(store).audio_path(RUN_ID, "speech-0001").read_bytes() == (
        original_audio.pcm_s16le
    )
    assert SegmentTakeStore(store).selected(RUN_ID, "speech-0001") == result["selected_take_id"]


@pytest.mark.parametrize("status", ["interrupted", "completed"])
def test_delete_rejects_registered_or_finalizing_worker(
    tmp_path: Path, status: str
) -> None:
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    store, _audio = _completed_run(application.runs_directory)
    if status == "interrupted":
        RevisionStore(store).create(
            RUN_ID,
            RevisionRequest(timeline=TimelineEdit(silence_duration_ms={"silence-0001": 2_000})),
        )
    task = GenerationTask(
        task_id=str(RUN_ID),
        run_id=str(RUN_ID),
        launching=status == "interrupted",
        process=SimpleNamespace(poll=lambda: None) if status == "completed" else None,
    )
    application._tasks[str(RUN_ID)] = task
    before = _snapshot(store.run_directory(RUN_ID))

    with pytest.raises(ValueError, match="active"):
        application.delete_run(str(RUN_ID))

    assert _snapshot(store.run_directory(RUN_ID)) == before
    task.launching = False
    task.process = None
    assert application.delete_run(str(RUN_ID)) is not None
    assert not store.run_directory(RUN_ID).exists()


def test_delete_rejects_durable_worker_finalization(tmp_path: Path) -> None:
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    store, _audio = _completed_run(application.runs_directory)
    with RunLock(store.run_directory(RUN_ID)):
        with pytest.raises(ValueError, match="active"):
            application.delete_run(str(RUN_ID))
        assert store.run_directory(RUN_ID).is_dir()
    assert application.delete_run(str(RUN_ID)) is not None


def test_delete_waits_for_worker_registration_then_rejects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, _revision, _take = _prepared_application(tmp_path, monkeypatch)
    registering = threading.Event()
    release = threading.Event()
    deletion_started = threading.Event()
    deletion_finished = threading.Event()
    launch = application._launch_recovery_command_admitted

    def delayed_launch(
        run_id: str, command_name: str, segment_id: str | None = None
    ) -> dict[str, Any]:
        registering.set()
        assert release.wait(5)
        return launch(run_id, command_name, segment_id)

    def delete() -> None:
        deletion_started.set()
        try:
            with pytest.raises(ValueError, match="active"):
                application.delete_run(str(RUN_ID))
        finally:
            deletion_finished.set()

    monkeypatch.setattr(application, "_launch_recovery_command_admitted", delayed_launch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        resume = pool.submit(application.resume_run, str(RUN_ID))
        try:
            assert registering.wait(5)
            deletion = pool.submit(delete)
            assert deletion_started.wait(5)
            assert not deletion_finished.wait(0.05)
        finally:
            release.set()
        assert resume.result(timeout=5) is not None
        deletion.result(timeout=5)
    assert application._tasks[str(RUN_ID)].active
    assert (application.runs_directory / str(RUN_ID)).is_dir()


def test_cancelled_alternative_take_releases_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    store, _audio = _completed_run(application.runs_directory)
    monkeypatch.setattr("whoopy.webui.server.threading.Thread.start", lambda _thread: None)
    result = application.generate_alternative_take(str(RUN_ID), "speech-0001")
    assert result is not None
    task = application._tasks[str(RUN_ID)]
    application.cancel_task(str(RUN_ID))
    task.launching = False
    before = _snapshot(store.run_directory(RUN_ID))

    application._finalize_alternative_take(task, "speech-0001", result["selected_take_id"])

    assert _snapshot(store.run_directory(RUN_ID)) == before
    assert application.start_model_pack_task("smoke-test", "moss-local-5b", {})["status"] == "queued"
