from __future__ import annotations

import os
from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from whoopy.pipeline import RunExecution, RunRecord, RunStage, RunStatus, RunStore
from whoopy.pipeline.locks import RunLock
from whoopy.webui.server import LocalWebApplication


def _application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalWebApplication:
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    monkeypatch.setattr("whoopy.webui.server.threading.Thread.start", lambda _thread: None)
    monkeypatch.setattr(application, "_require_speech_model_ready", lambda *_args, **_kwargs: None)
    return application


def _running_record(store: RunStore, *, expired: bool) -> RunRecord:
    record = store.create("Surviving Lab trial.", audience="lab")
    assert record.recovery is not None
    now = datetime.now(UTC)
    started = now - timedelta(minutes=30)
    heartbeat = now - timedelta(minutes=1) if expired else now
    record = record.transition(
        RunStatus.RUNNING,
        updated_at=heartbeat,
        recovery=record.recovery.model_copy(update={"process_attempts": 1}),
        execution=RunExecution(
            stage=RunStage.SYNTHESIZING,
            attempt_id=uuid4(),
            owner_id="previous-web-process",
            pid=os.getpid(),
            started_at=started,
            heartbeat_at=heartbeat,
            lease_expires_at=heartbeat + timedelta(seconds=15),
        ),
    )
    store.save(record)
    return record


def _start_operation(application: LocalWebApplication, operation: str) -> dict[str, object]:
    if operation == "generation":
        return application.start_generation({"mode": "script", "text": "Notice the room."})
    if operation == "runtime":
        return application.start_runtime_pack_task(
            "install", "moss-runtime", {"allow_network": False}
        )
    return application.start_model_pack_task("smoke-test", "moss-local-5b", {})


@pytest.mark.parametrize("operation", ["generation", "runtime", "model"])
@pytest.mark.parametrize("ownership", ["lease", "lock", "finalizing"])
def test_restarted_app_excludes_surviving_durable_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, ownership: str
) -> None:
    application = _application(tmp_path, monkeypatch)
    store = RunStore(application.runs_directory)
    record = _running_record(store, expired=ownership != "lease")
    if ownership == "finalizing":
        assert record.execution is not None
        record = record.transition(
            RunStatus.INTERRUPTED,
            updated_at=datetime.now(UTC),
            execution=record.execution.finish(interruption_kind="cancelled"),
        )
        store.save(record)
    owner = RunLock(store.run_directory(record.run_id)) if ownership != "lease" else nullcontext()

    with owner, pytest.raises(ValueError, match="active"):
        _start_operation(application, operation)

    assert application._tasks == {}
    assert application._model_pack_tasks == {}
    assert store.load(record.run_id).status is record.status
    assert len(application.list_runs()) == 1


@pytest.mark.parametrize("operation", ["generation", "runtime", "model"])
def test_restarted_app_reconciles_abandoned_worker_before_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    application = _application(tmp_path, monkeypatch)
    store = RunStore(application.runs_directory)
    record = _running_record(store, expired=True)

    result = _start_operation(application, operation)

    assert result["status"] == "queued"
    recovered = store.load(record.run_id)
    assert recovered.status is RunStatus.INTERRUPTED
    assert recovered.execution is not None
    assert recovered.execution.interruption_kind == "lease_expired"


def test_restarted_app_can_cancel_surviving_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path, monkeypatch)
    store = RunStore(application.runs_directory)
    record = _running_record(store, expired=True)

    with RunLock(store.run_directory(record.run_id)):
        result = application.cancel_run(str(record.run_id))

    assert result is not None
    assert result["run_id"] == str(record.run_id)
    assert str(record.run_id) in application._tasks
