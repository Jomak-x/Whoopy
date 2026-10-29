from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from whoopy.pipeline import RunRecord, RunStore
from whoopy.webui.server import LocalWebApplication


def _application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalWebApplication:
    def unavailable(_command: list[str], **_kwargs: Any) -> Any:
        raise OSError("offline runtime unavailable")

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=unavailable,
    )
    monkeypatch.setattr(application, "_require_speech_model_ready", lambda *_args, **_kwargs: None)
    return application


@pytest.mark.parametrize("mode", ["prompt", "script"])
def test_browser_retry_history_survives_restart_before_child_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    pending: list[threading.Thread] = []
    monkeypatch.setattr(threading.Thread, "start", lambda thread: pending.append(thread))
    application = _application(tmp_path, monkeypatch)
    launched = application.start_generation({"mode": mode, "text": "Notice the room."})
    run_id = str(launched["run_id"])
    store = RunStore(application.runs_directory)
    pending.pop(0).run()
    first_failure = store.load(run_id)
    assert first_failure.recovery is not None
    assert first_failure.recovery.process_attempts == 1
    assert first_failure.recovery.resume_count == 0

    application.resume_run(run_id)
    assert store.load(run_id).recovery == first_failure.recovery
    abandoned_thread = pending.pop(0)
    restarted = _application(tmp_path, monkeypatch)
    restarted.reconcile_runs()
    assert store.load(run_id).recovery == first_failure.recovery
    restarted.resume_run(run_id)
    assert store.load(run_id).recovery == first_failure.recovery
    pending.pop(0).run()
    second_failure = store.load(run_id)
    assert second_failure.recovery is not None
    assert second_failure.recovery.process_attempts == 2
    assert second_failure.recovery.resume_count == 1

    abandoned_thread.run()
    assert store.load(run_id) == second_failure
    restarted.resume_run(run_id)
    pending.pop(0).run()
    final = store.load(run_id)
    assert final.recovery is not None
    assert final.recovery.process_attempts == 3
    assert final.recovery.resume_count == 2
    snapshots = [
        RunRecord.model_validate_json(path.read_text(encoding="utf-8"))
        for path in (store.run_directory(run_id) / "history" / "web-launch-attempts").glob("*.json")
    ]
    assert first_failure in snapshots
    assert second_failure in snapshots
    assert all(record.run_id == final.run_id for record in snapshots)


def test_browser_supervisor_preserves_child_owned_failure_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending: list[threading.Thread] = []
    monkeypatch.setattr(threading.Thread, "start", lambda thread: pending.append(thread))
    application = _application(tmp_path, monkeypatch)
    launched = application.start_generation({"mode": "script", "text": "Notice the room."})
    run_id = str(launched["run_id"])
    store = RunStore(application.runs_directory)
    task = application._tasks[run_id]
    application._persist_process_failure(run_id, "child retained its failure")
    child_failure = store.load(run_id)

    application._persist_process_failure(run_id, "supervisor saw nonzero exit", task=task)
    pending.pop(0).run()

    assert store.load(run_id) == child_failure
    assert child_failure.recovery is not None
    assert child_failure.recovery.process_attempts == 1
    assert child_failure.error == "child retained its failure"
