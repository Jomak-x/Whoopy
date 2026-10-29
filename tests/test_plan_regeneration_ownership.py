from __future__ import annotations

import json
import multiprocessing
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from multiprocessing.connection import Connection
from pathlib import Path
from uuid import UUID

import pytest

from tests.test_revisions import RUN_ID, _completed_generated_run, _generated, _plan
from whoopy.adapters.llm import LlamaCppScriptGenerator
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.cli import main
from whoopy.hardware import HardwareSnapshot
from whoopy.meditation import MeditationGenerationResult, MeditationPlan, load_prompt_bundle
from whoopy.meditation.models import RawGenerationAttempt
from whoopy.meditation.workspace import GenerationWorkspace
from whoopy.pipeline.generation import PendingGenerationConfig
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.revisions import RevisionManifest, RevisionStore
from whoopy.pipeline.runs import RunRecord, RunStatus, RunStore
from whoopy.pipeline.worker import LocalWorker
from whoopy.webui.server import LocalWebApplication


def _application(runs: Path) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=runs.parent / "models",
        runs_directory=runs,
    )


def _delete_commands(runs: Path, connection: Connection) -> None:
    try:
        application = _application(runs)
        while connection.recv() == "delete":
            try:
                deleted = application.delete_run(str(RUN_ID))
            except ValueError:
                connection.send("rejected")
            else:
                connection.send("deleted" if deleted is not None else "missing")
    finally:
        connection.close()


@contextmanager
def _deletion_contender(runs: Path) -> Iterator[Callable[[], None]]:
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=_delete_commands, args=(runs, child))
    process.start()
    child.close()

    def assert_rejected() -> None:
        parent.send("delete")
        assert parent.poll(15), "Deletion contender did not respond"
        assert parent.recv() == "rejected"

    try:
        yield assert_rejected
    finally:
        parent.send("stop")
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(10)
        parent.close()
    assert process.exitcode == 0


def _regeneration_inputs(tmp_path: Path) -> tuple[RunStore, MeditationGenerationResult, list[str]]:
    base = _plan()
    store = _completed_generated_run(
        tmp_path / "runs",
        _generated(
            base,
            middle_text="Keep returning to one simple anchor with patient steady attention.",
        ),
    )
    changed = base.sections[1].model_copy(
        update={
            "technique": "noting",
            "purpose": "Name distraction softly, then return to the chosen anchor.",
        }
    )
    requested = base.model_copy(update={"sections": [base.sections[0], changed, base.sections[2]]})
    redraft = _generated(
        requested,
        middle_text="Name each distraction softly and return without adding any judgment.",
    ).model_copy(
        update={
            "raw_attempts": [
                RawGenerationAttempt(
                    stage="section:practice", attempt=1, text="new draft", elapsed_seconds=1
                )
            ]
        }
    )
    config = store.load_resolved_config(RUN_ID)
    prompts = load_prompt_bundle(Path("config/prompts"))
    request = PendingGenerationConfig(
        profile="standard",
        target=config.target,
        tts=config.tts,
        processing=config.processing,
        duration_seconds=300,
        seed=42,
        plan_prompt_id=prompts.plan.prompt_id,
        plan_prompt_version=prompts.plan.version,
        section_prompt_id=prompts.section.prompt_id,
        section_prompt_version=prompts.section.version,
        max_parallel_sections=1,
        model_metadata=store.load_model_metadata(RUN_ID),
    )
    store.generation_request_path(RUN_ID).write_text(request.model_dump_json(), encoding="utf-8")
    plan_file = tmp_path / "edited-plan.json"
    plan_file.write_text(requested.model_dump_json(), encoding="utf-8")
    return (
        store,
        redraft,
        [
            "run",
            "regenerate-plan",
            str(RUN_ID),
            "--plan-file",
            str(plan_file),
            "--config-dir",
            "config",
            "--models-dir",
            str(tmp_path / "models"),
            "--runs-dir",
            str(store.root),
            "--json",
        ],
    )


def test_plan_regeneration_excludes_cross_process_deletion_until_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, redraft, arguments = _regeneration_inputs(tmp_path)
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
    monkeypatch.setattr(
        "whoopy.cli._worker_for_record",
        lambda run_store, _record, **_kwargs: LocalWorker(run_store),
    )
    stages: list[str] = []
    load = RunStore.load
    save_plan = GenerationWorkspace.save_plan
    mark_for_render = RevisionStore._mark_for_render

    with _deletion_contender(store.root) as assert_rejected:

        def load_under_ownership(run_store: RunStore, run_id: UUID | str) -> RunRecord:
            if not stages:
                assert_rejected()
                stages.append("read")
            return load(run_store, run_id)

        def save_under_ownership(workspace: GenerationWorkspace, plan: MeditationPlan) -> None:
            assert_rejected()
            save_plan(workspace, plan)
            stages.append("persist")

        class DraftGenerator:
            def __init__(
                self, *_args: object, workspace: GenerationWorkspace, **_kwargs: object
            ) -> None:
                self.workspace = workspace

            def generate(self, **_kwargs: object) -> MeditationGenerationResult:
                assert_rejected()
                stages.append("draft")
                for attempt in redraft.raw_attempts:
                    self.workspace.save_attempt(attempt)
                for section in redraft.sections:
                    self.workspace.save_section(section)
                self.workspace.save_outputs(redraft.script, redraft.timeline)
                assert_rejected()
                return redraft

        def activate_under_ownership(
            revisions: RevisionStore, record: object, manifest: RevisionManifest
        ) -> None:
            assert_rejected()
            mark_for_render(revisions, record, manifest)
            assert_rejected()
            stages.append("activate")

        monkeypatch.setattr(RunStore, "load", load_under_ownership)
        monkeypatch.setattr(GenerationWorkspace, "save_plan", save_under_ownership)
        monkeypatch.setattr("whoopy.cli.LocalMeditationGenerator", DraftGenerator)
        monkeypatch.setattr(RevisionStore, "_mark_for_render", activate_under_ownership)
        assert main(arguments) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["run"]["status"] == "completed"
    assert output["revision"]["change_kind"] == "plan"
    assert stages == ["read", "persist", "draft", "activate"]
    assert store.load(RUN_ID).status is RunStatus.COMPLETED
    assert store.load_script(RUN_ID) == redraft.script
    assert len(RevisionStore(store).list(RUN_ID)) == 2
    assert _application(store.root).list_deleted_runs() == []
    workspaces = list((store.run_directory(RUN_ID) / "history" / "plan-regeneration").iterdir())
    assert len(workspaces) == 1
    workspace = GenerationWorkspace(workspaces[0])
    assert workspace.load_plan() == redraft.plan
    assert (workspace.root / "script.md").read_text() == redraft.script
    attempt_path = next((workspace.root / "raw-model-output").iterdir())
    saved_attempt = RawGenerationAttempt.model_validate_json(attempt_path.read_text())
    assert saved_attempt == redraft.raw_attempts[0]


def test_plan_regeneration_refuses_existing_owner_before_reading_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, _redraft, arguments = _regeneration_inputs(tmp_path)
    record_before = store.record_path(RUN_ID).read_bytes()

    def unexpected_read(_store: RunStore, _run_id: UUID | str) -> RunRecord:
        pytest.fail("Regeneration read the run before obtaining ownership")

    monkeypatch.setattr(RunStore, "load", unexpected_read)
    with RunLock(store.run_directory(RUN_ID)), pytest.raises(SystemExit) as stopped:
        main(arguments)
    assert stopped.value.code == 2
    assert "Another worker" in capsys.readouterr().err
    assert store.record_path(RUN_ID).read_bytes() == record_before
    assert not (store.run_directory(RUN_ID) / "history").exists()


def test_plan_regeneration_does_not_recreate_a_deleted_run(tmp_path: Path) -> None:
    store, _redraft, arguments = _regeneration_inputs(tmp_path)
    application = _application(store.root)
    original = store.record_path(RUN_ID).read_bytes()
    deleted = application.delete_run(str(RUN_ID))
    assert deleted is not None

    with pytest.raises(SystemExit) as stopped:
        main(arguments)

    assert stopped.value.code == 2
    assert not store.run_directory(RUN_ID).exists()
    restored = application.restore_deleted_run(
        deleted["receipt_id"], confirmation=deleted["restore_confirmation"]
    )
    assert restored is not None
    assert store.record_path(RUN_ID).read_bytes() == original
    assert not (store.run_directory(RUN_ID) / "history").exists()
