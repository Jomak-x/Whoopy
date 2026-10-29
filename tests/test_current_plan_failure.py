from __future__ import annotations

import json
from pathlib import Path
from threading import Event
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.test_plan_regeneration_ownership import _regeneration_inputs
from tests.test_revisions import RUN_ID, _completed_generated_run, _generated, _plan
from whoopy.adapters.llm import LlamaCppScriptGenerator
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.cli import main
from whoopy.hardware import HardwareSnapshot
from whoopy.meditation import MeditationGenerationResult, MeditationPlan
from whoopy.meditation.workspace import GenerationWorkspace
from whoopy.pipeline.revisions import RevisionStore
from whoopy.pipeline.runs import RunRecord, RunStatus, RunStore
from whoopy.pipeline.worker import LocalWorker, WorkerError
from whoopy.webui.api import create_api
from whoopy.webui.server import GenerationTask, LocalWebApplication


def _application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, failure: str = "exit"
) -> tuple[LocalWebApplication, RunStore, MeditationPlan, Event]:
    base = _plan()
    store = _completed_generated_run(
        tmp_path / "runs",
        _generated(
            base,
            middle_text="Keep returning to one simple anchor with patient steady attention.",
        ),
    )
    requested = base.model_copy(
        update={
            "sections": [
                base.sections[0],
                base.sections[1].model_copy(
                    update={"purpose": "Return gently to your chosen anchor."}
                ),
                base.sections[2],
            ]
        }
    )

    class FailedProcess:
        returncode = 2

        def __init__(self, _command: list[str], **_kwargs: Any) -> None:
            if failure == "launch":
                raise OSError("controlled precommit launch failure")

        def poll(self) -> int:
            return self.returncode

        def communicate(self) -> tuple[str, str]:
            return "", "controlled precommit drafting failure"

    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=store.root,
        command_launcher=FailedProcess,
    )
    finished = Event()
    supervise = application._run_plan_regeneration

    def supervise_and_signal(task: GenerationTask, input_path: Path, note: str) -> None:
        try:
            supervise(task, input_path, note)
        finally:
            finished.set()

    monkeypatch.setattr(application, "_run_plan_regeneration", supervise_and_signal)
    return application, store, requested, finished


@pytest.mark.parametrize("failure", ["exit", "launch"])
def test_current_precommit_plan_failure_visible_until_cli_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    application, store, requested, finished = _application(tmp_path, monkeypatch, failure=failure)
    before = store.load(RUN_ID)
    original_script = store.load_script(RUN_ID)
    original_plan = store.plan_path(RUN_ID).read_bytes()
    response = application.edit_run(str(RUN_ID), {"plan": requested.model_dump(mode="json")})
    assert response is not None and response["semantic_regeneration"] is True
    assert finished.wait(5)
    failed_task = application._tasks[str(RUN_ID)]
    assert failed_task.error is not None and "precommit" in failed_task.error
    assert store.load(RUN_ID) == before
    assert store.load_script(RUN_ID) == original_script
    assert store.plan_path(RUN_ID).read_bytes() == original_plan
    assert RevisionStore(store).active_revision_id(RUN_ID) is None

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        for path in (f"/api/v1/runs/{RUN_ID}", f"/api/v1/tasks/{RUN_ID}"):
            state = client.get(path)
            assert state.status_code == 200
            assert state.json()["status"] == "failed"
            assert state.json()["error"] == failed_task.error
            assert state.json()["execution"]["stage"] == "drafting"
            assert state.json()["resumable"] is False
        assert application.list_runs()[0]["error"] == failed_task.error

        RevisionStore(store).create_assembly_revision(RUN_ID, note="Recover retained meditation")
        monkeypatch.setattr(
            "whoopy.cli._worker_for_record",
            lambda run_store, _record, **_kwargs: LocalWorker(run_store),
        )
        assert main(["run", "resume", str(RUN_ID), "--runs-dir", str(store.root), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"
        assert store.load(RUN_ID).status is RunStatus.COMPLETED
        assert store.load(RUN_ID).execution != before.execution
        for path in (f"/api/v1/runs/{RUN_ID}", f"/api/v1/tasks/{RUN_ID}"):
            state = client.get(path).json()
            assert state["status"] == "completed"
            assert state["error"] is None
            assert state["has_audio"] is True
        assert failed_task.error is not None


def test_new_plan_operation_supersedes_current_precommit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, _store, requested, finished = _application(tmp_path, monkeypatch)
    payload = {"plan": requested.model_dump(mode="json")}
    application.edit_run(str(RUN_ID), payload)
    assert finished.wait(5)
    first = application.task(str(RUN_ID))
    assert first is not None and first["status"] == "failed"
    old_task = application._tasks[str(RUN_ID)]
    entered = Event()
    release = Event()
    finished.clear()

    class NextProcess:
        returncode: int | None = None

        def __init__(self, _command: list[str], **_kwargs: Any) -> None:
            pass

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self) -> tuple[str, str]:
            entered.set()
            assert release.wait(5)
            self.returncode = 2
            return "", "controlled newer drafting failure"

    monkeypatch.setattr(application, "_command_launcher", NextProcess)
    try:
        application.edit_run(str(RUN_ID), payload)
        assert entered.wait(5)
        state = application.task(str(RUN_ID))
        assert state is not None and state["status"] == "running"
        assert state["error"] is None
        assert application._tasks[str(RUN_ID)] is not old_task
    finally:
        release.set()
        assert finished.wait(5)
    state = application.task(str(RUN_ID))
    assert state is not None and state["status"] == "failed"
    assert state["error"] == "controlled newer drafting failure"
    assert old_task.error == first["error"]


@pytest.mark.parametrize("concurrent_recovery", [False, True])
def test_committed_plan_worker_construction_failure_preserves_current_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    concurrent_recovery: bool,
) -> None:
    store, redraft, arguments = _regeneration_inputs(tmp_path)
    original = store.load(RUN_ID)
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=store.root,
    )
    task = GenerationTask(
        task_id=str(RUN_ID),
        run_id=str(RUN_ID),
        mode="plan_regeneration",
        launching=False,
        error="controlled speech adapter construction failure",
        plan_source_record=original,
    )
    application._tasks[str(RUN_ID)] = task
    snapshot = HardwareSnapshot(
        operating_system="linux",
        architecture="x86_64",
        cpu_count=8,
        total_ram_gb=16,
        available_ram_gb=9,
        free_disk_gb=20,
        accelerators=["cpu"],
    )
    monkeypatch.setattr("whoopy.cli.inspect_hardware", lambda _path: snapshot)
    monkeypatch.setattr(
        LlamaCppScriptGenerator,
        "from_artifact_store",
        classmethod(lambda _cls, **_kwargs: FixtureSpeechSynthesizer()),
    )

    class DraftGenerator:
        def __init__(
            self, *_args: object, workspace: GenerationWorkspace, **_kwargs: object
        ) -> None:
            self.workspace = workspace

        def generate(self, **_kwargs: object) -> MeditationGenerationResult:
            for attempt in redraft.raw_attempts:
                self.workspace.save_attempt(attempt)
            for section in redraft.sections:
                self.workspace.save_section(section)
            self.workspace.save_outputs(redraft.script, redraft.timeline)
            return redraft

    completed: list[RunRecord] = []

    def fail_construction(run_store: RunStore, record: RunRecord, **_kwargs: object) -> LocalWorker:
        assert record.status is RunStatus.INTERRUPTED
        assert run_store.load_script(RUN_ID) == redraft.script
        if concurrent_recovery:
            completed.append(LocalWorker(run_store).resume(RUN_ID))
        raise WorkerError("controlled speech adapter construction failure")

    monkeypatch.setattr("whoopy.cli.LocalMeditationGenerator", DraftGenerator)
    monkeypatch.setattr("whoopy.cli._worker_for_record", fail_construction)
    with pytest.raises(SystemExit) as stopped:
        main(arguments)
    assert stopped.value.code == 2
    assert "controlled speech adapter construction failure" in capsys.readouterr().err
    assert store.load_script(RUN_ID) == redraft.script
    revisions = RevisionStore(store).list(RUN_ID)
    assert len(revisions) == 2
    assert revisions[-1].change_kind == "plan"
    if concurrent_recovery:
        assert store.load(RUN_ID) == completed[0]
    else:
        failed = store.load(RUN_ID)
        assert failed.status is RunStatus.FAILED
        assert failed.error == "WorkerError: controlled speech adapter construction failure"
        state = application.task(str(RUN_ID))
        assert state is not None and state["status"] == "failed"
        assert state["error"] == failed.error
        assert state["resumable"] is True
        monkeypatch.setattr(
            "whoopy.cli._worker_for_record",
            lambda run_store, _record, **_kwargs: LocalWorker(run_store),
        )
        assert main(["run", "resume", str(RUN_ID), "--runs-dir", str(store.root), "--json"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "completed"
    state = application.task(str(RUN_ID))
    assert state is not None and state["status"] == "completed"
    assert state["error"] is None
    assert state["has_audio"] is True
