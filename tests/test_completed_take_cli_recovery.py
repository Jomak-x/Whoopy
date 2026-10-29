from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from tests.test_pending_take_revisions import _artifacts
from tests.test_plan_regeneration_ownership import _regeneration_inputs
from tests.test_revisions import NOW, RUN_ID
from whoopy.adapters.llm import LlamaCppScriptGenerator
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.processing import ProcessedSpeechSynthesizer
from whoopy.cli import main
from whoopy.hardware import HardwareSnapshot
from whoopy.meditation import MeditationGenerationResult
from whoopy.pipeline.revisions import RevisionError, RevisionRequest, RevisionStore, TimelineEdit
from whoopy.pipeline.runs import RunExecution, RunStage, RunStatus
from whoopy.pipeline.takes import PendingAlternativeTake, SegmentTakeStore
from whoopy.pipeline.worker import LocalWorker


def test_cli_resume_completed_take_allows_plan_regeneration_without_web_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, redraft, arguments = _regeneration_inputs(tmp_path)
    revisions = RevisionStore(store)
    revisions.create_assembly_revision(RUN_ID, note="Render initial selected audio")
    first = LocalWorker(
        store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
    ).resume(RUN_ID)
    assert first.status is RunStatus.COMPLETED
    assert first.execution is not None
    takes = SegmentTakeStore(store)
    selected = takes.capture_active(RUN_ID, "speech-0001", source="original")
    takes.choose(RUN_ID, "speech-0001", selected.take_id, action="restore")
    assembly = revisions.create_assembly_revision(RUN_ID, note="Restore selected take")
    pending = PendingAlternativeTake(
        operation_id=uuid4(),
        run_id=RUN_ID,
        segment_id="speech-0001",
        selected_take_id=selected.take_id,
        phase="assembling",
        assembly_revision_id=assembly.revision_id,
        assembly_prior_attempt_id=first.execution.attempt_id,
    )
    takes.save_pending(pending)
    monkeypatch.setattr(
        "whoopy.cli._worker_for_record",
        lambda run_store, _record, **_kwargs: LocalWorker(
            run_store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
        ),
    )

    assert main(["run", "resume", str(RUN_ID), "--runs-dir", str(store.root), "--json"]) == 0

    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "completed"
    assert resumed["execution"]["attempt_id"] != str(pending.assembly_prior_attempt_id)
    assert takes.pending(RUN_ID) == pending
    assert takes.selected(RUN_ID, "speech-0001") == selected.take_id
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
    draft_calls: list[str] = []

    class DraftGenerator:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            assert takes.pending(RUN_ID) is None

        def generate(self, **_kwargs: object) -> MeditationGenerationResult:
            draft_calls.append("draft")
            return redraft

    monkeypatch.setattr("whoopy.cli.LocalMeditationGenerator", DraftGenerator)

    assert main(arguments) == 0

    regenerated = json.loads(capsys.readouterr().out)
    assert regenerated["run"]["status"] == "completed"
    assert regenerated["revision"]["change_kind"] == "plan"
    assert regenerated["revision"]["parent_revision_id"] == assembly.revision_id
    assert store.load_script(RUN_ID) == redraft.script
    assert draft_calls == ["draft"]
    receipt = (
        store.run_directory(RUN_ID)
        / "history"
        / "alternative-takes"
        / f"{pending.operation_id}.json"
    )
    assert PendingAlternativeTake.model_validate_json(receipt.read_text()) == pending
    assert takes.pending(RUN_ID) is None


@pytest.mark.parametrize("entrypoint", ["cli", "revision"])
@pytest.mark.parametrize(
    "unresolved",
    [
        "generating",
        "restoring",
        "failed",
        "incomplete",
        "wrong_revision",
        "same_attempt",
        "missing_attempt",
        "missing_revision",
    ],
)
def test_unresolved_take_blocks_edit_before_drafting_without_artifact_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entrypoint: str,
    unresolved: str,
) -> None:
    store, _redraft, arguments = _regeneration_inputs(tmp_path)
    original = store.load(RUN_ID)
    revisions = RevisionStore(store)
    assembly = revisions.create_assembly_revision(RUN_ID, note="Selected take assembly")
    attempt_id = uuid4()
    if unresolved != "incomplete":
        store.save(
            original.model_copy(
                update={
                    "execution": RunExecution(
                        stage=RunStage.COMPLETED,
                        attempt_id=None if unresolved == "missing_attempt" else attempt_id,
                        owner_id=None if unresolved == "missing_attempt" else "test-worker",
                        pid=None if unresolved == "missing_attempt" else 1,
                        started_at=None if unresolved == "missing_attempt" else NOW,
                    )
                }
            )
        )
    phase: Literal["generating", "restoring", "assembling", "failed"] = "assembling"
    if unresolved == "generating":
        phase = "generating"
    elif unresolved == "restoring":
        phase = "restoring"
    elif unresolved == "failed":
        phase = "failed"
    revision_id = assembly.revision_id
    if unresolved == "wrong_revision":
        revision_id = revisions.list(RUN_ID)[0].revision_id
    takes = SegmentTakeStore(store)
    takes.save_pending(
        PendingAlternativeTake(
            operation_id=uuid4(),
            run_id=RUN_ID,
            segment_id="speech-0001",
            selected_take_id="t0001-0123456789",
            phase=phase,
            assembly_revision_id=None if unresolved == "missing_revision" else revision_id,
            assembly_prior_attempt_id=attempt_id if unresolved == "same_attempt" else uuid4(),
        )
    )
    before = _artifacts(store.run_directory(RUN_ID))

    def unexpected_model_plan(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Unresolved take reached model planning")

    monkeypatch.setattr("whoopy.cli._model_plan", unexpected_model_plan)
    if entrypoint == "cli":
        with pytest.raises(SystemExit) as stopped:
            main(arguments)
        assert stopped.value.code == 2
        assert "pending alternative take" in capsys.readouterr().err
    else:
        with pytest.raises(RevisionError, match="pending alternative take"):
            revisions.create(
                RUN_ID, RevisionRequest(timeline=TimelineEdit(pace={"speech-0001": "slow"}))
            )

    assert _artifacts(store.run_directory(RUN_ID)) == before
