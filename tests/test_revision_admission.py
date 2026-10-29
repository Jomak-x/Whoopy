from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from tests.test_takes import RUN_ID, _completed_run
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
