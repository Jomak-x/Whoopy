from __future__ import annotations

import multiprocessing
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.runs import RunRecord, RunStatus, RunStore, RunStoreError
from whoopy.pipeline.worker import LocalWorker, WorkerError
from whoopy.webui.server import LocalWebApplication

if sys.platform == "win32":
    from multiprocessing.connection import PipeConnection as Connection
else:
    from multiprocessing.connection import Connection


def _worker_commands(root: Path, run_id: str, connection: Connection) -> None:
    try:
        while True:
            command = connection.recv()
            if command == "stop":
                return
            if command == "hold":
                with RunLock(root / run_id):
                    connection.send("held")
                    assert connection.recv() == "release"
                connection.send("released")
            else:
                assert command == "resume"
                try:
                    result = LocalWorker(RunStore(root)).resume(run_id)
                except (WorkerError, RunStoreError):
                    connection.send("rejected")
                else:
                    connection.send(result.status.value)
    finally:
        connection.close()


@contextmanager
def _contender(root: Path, run_id: str) -> Iterator[Connection]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_worker_commands, args=(root, run_id, child))
    process.start()
    child.close()
    try:
        yield parent
    finally:
        parent.send("stop")
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
    assert process.exitcode == 0


def _command(connection: Connection, command: str) -> object:
    connection.send(command)
    assert connection.poll(15), f"Worker did not answer {command}"
    return connection.recv()


def _interrupted_application(tmp_path: Path) -> tuple[LocalWebApplication, RunRecord]:
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    store = RunStore(application.runs_directory)
    record = store.create("Notice the support beneath you.")
    assert record.recovery is not None
    assert record.execution is not None
    record = record.transition(
        RunStatus.INTERRUPTED,
        updated_at=datetime.now(UTC),
        recovery=record.recovery.model_copy(update={"process_attempts": 1}),
        execution=record.execution.finish(interruption_kind="cancelled"),
    )
    store.save(record)
    return application, record


def test_delete_owns_run_across_move_and_resume_cannot_resurrect_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, record = _interrupted_application(tmp_path)
    run_id = str(record.run_id)
    run_directory = application.runs_directory / run_id
    original_record = (run_directory / "run.json").read_bytes()
    move = application._trash_stopped_run

    with _contender(application.runs_directory, run_id) as contender:

        def move_while_contended(stopped: RunRecord) -> dict[str, Any]:
            assert _command(contender, "resume") == "rejected"
            result = move(stopped)
            assert not run_directory.exists()
            assert _command(contender, "resume") == "rejected"
            with pytest.raises(ValueError, match="active operation"):
                application.restore_deleted_run(
                    result["receipt_id"], confirmation=result["restore_confirmation"]
                )
            return result

        monkeypatch.setattr(application, "_trash_stopped_run", move_while_contended)
        deleted = application.delete_run(run_id)
        assert deleted is not None
        assert _command(contender, "resume") == "rejected"
        assert not run_directory.exists()
        entries = application.list_deleted_runs()
        assert len(entries) == 1
        assert (Path(entries[0]["path"]) / "run.json").read_bytes() == original_record

        restored = application.restore_deleted_run(
            deleted["receipt_id"], confirmation=deleted["restore_confirmation"]
        )
        assert restored is not None
        assert _command(contender, "resume") == "completed"
        assert RunStore(application.runs_directory).load(run_id).status is RunStatus.COMPLETED


def test_delete_rejects_another_process_owner_then_moves_after_release(tmp_path: Path) -> None:
    application, record = _interrupted_application(tmp_path)
    run_id = str(record.run_id)
    run_directory = application.runs_directory / run_id

    with _contender(application.runs_directory, run_id) as contender:
        assert _command(contender, "hold") == "held"
        try:
            with pytest.raises(ValueError, match="active generation"):
                application.delete_run(run_id)
            assert run_directory.is_dir()
        finally:
            assert _command(contender, "release") == "released"
        assert application.delete_run(run_id) is not None
        assert not run_directory.exists()
