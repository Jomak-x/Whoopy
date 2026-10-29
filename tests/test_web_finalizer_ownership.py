from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from tests.test_webui import _ReadyKokoroPacks
from whoopy.pipeline import RunStatus, RunStore
from whoopy.webui.server import GenerationTask, LocalWebApplication


def test_browser_resume_owns_admission_until_prior_attempts_are_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailedProcess:
        returncode = 1

        def __init__(self, _command: list[str], **_kwargs: Any) -> None:
            pass

        def communicate(self) -> tuple[str, str]:
            return "", "whoopy: error: model preflight failed"

        def poll(self) -> int:
            return self.returncode

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FailedProcess,
        model_pack_service_factory=lambda _registry, _models: _ReadyKokoroPacks(),
    )
    with monkeypatch.context() as setup:
        setup.setattr("whoopy.webui.server.threading.Thread.start", lambda _thread: None)
        created = application.start_generation({"mode": "prompt", "text": "Pause.", "minutes": 1})
    run_id = created["run_id"]
    for _ in range(3):
        application._persist_process_failure(run_id, "Previous launch failed.")
    application._tasks[run_id].launching = False
    store = RunStore(application.runs_directory)
    previous = store.load(run_id)
    assert previous.recovery is not None and previous.recovery.process_attempts == 3
    finalizing = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    merge_attempts = application._record_resumed_web_launch
    run_generation = application._run_generation

    def pause_finalizer(task: GenerationTask) -> None:
        finalizing.set()
        assert release.wait(5)
        merge_attempts(task)

    def supervise(*args: Any) -> None:
        try:
            run_generation(*args)
        finally:
            finished.set()

    monkeypatch.setattr(application, "_record_resumed_web_launch", pause_finalizer)
    monkeypatch.setattr(application, "_run_generation", supervise)
    assert application.resume_run(run_id) is not None
    try:
        assert finalizing.wait(5)
        incomplete = store.load(run_id)
        assert incomplete.status is RunStatus.FAILED
        assert incomplete.recovery is not None and incomplete.recovery.process_attempts == 1
        for summary in (
            application.run(run_id),
            application.task(run_id),
            *application.list_runs(),
        ):
            assert summary is not None
            assert summary["status"] == "running"
            assert summary["resumable"] is False
            assert summary["execution"]["stage"] == "finalizing"
        with pytest.raises(ValueError, match="already active"):
            application.resume_run(run_id)
        with pytest.raises(ValueError, match="active"):
            application.start_model_pack_task("smoke-test", "kokoro", {})
        with pytest.raises(ValueError, match="active"):
            application.delete_run(run_id)
        assert store.load(run_id) == incomplete
    finally:
        release.set()
        assert finished.wait(5)
    result = store.load(run_id)
    assert result.status is RunStatus.FAILED
    assert result.recovery is not None
    assert result.recovery.process_attempts == 4
    assert result.recovery.resume_count == 3
    merge_attempts(application._tasks[run_id])
    assert store.load(run_id) == result
    summary = application.run(run_id)
    assert summary is not None and summary["resumable"] is True
    assert summary["status"] == "failed"


@pytest.mark.parametrize("callback", ["failure", "cancellation", "retry_provenance"])
def test_stale_supervisor_cannot_mutate_a_new_operation(tmp_path: Path, callback: str) -> None:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    store = RunStore(application.runs_directory)
    run_id = str(store.create("A newer generation operation.").run_id)
    application._persist_process_failure(run_id, "New operation failure.")
    stale = GenerationTask(
        task_id=run_id,
        run_id=run_id,
        prior_process_attempts=3,
        prior_resume_count=2,
        resumed_web_launch=True,
    )
    current = GenerationTask(task_id=run_id, run_id=run_id)
    application._tasks[run_id] = current
    record = store.load(run_id)

    if callback == "failure":
        application._persist_process_failure(run_id, "Stale failure.", task=stale)
    elif callback == "cancellation":
        application._persist_cancel_intent(run_id, task=stale)
    else:
        application._record_resumed_web_launch(stale)

    assert store.load(run_id) == record
    assert application._tasks[run_id] is current
    assert current.active
