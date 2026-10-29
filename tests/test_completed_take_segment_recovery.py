from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.test_pending_take_revisions import _artifacts
from tests.test_takes import RUN_ID, _completed_run
from tests.test_task_finalization import _application
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import ProcessedSpeechSynthesizer
from whoopy.cli import main
from whoopy.pipeline.checkpoints import SegmentCheckpointStore
from whoopy.pipeline.revisions import RevisionStore
from whoopy.pipeline.runs import RunRecord, RunStatus, RunStore
from whoopy.pipeline.takes import PendingAlternativeTake, SegmentTakeStore
from whoopy.pipeline.worker import LocalWorker
from whoopy.timeline import SpeechSegment


def _pending_assembly(tmp_path: Path) -> tuple[RunStore, PendingAlternativeTake]:
    store, _audio = _completed_run(tmp_path / "runs")
    revisions = RevisionStore(store)
    revisions.create_assembly_revision(RUN_ID, note="Render initial selected audio")
    initial = LocalWorker(
        store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
    ).resume(RUN_ID)
    assert initial.status is RunStatus.COMPLETED
    assert initial.execution is not None
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
        assembly_prior_attempt_id=initial.execution.attempt_id,
    )
    takes.save_pending(pending)
    return store, pending


def _fixture_workers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    synthesized: list[str] = []

    class Synthesizer(FixtureSpeechSynthesizer):
        def synthesize(self, segment: SpeechSegment) -> PcmAudio:
            synthesized.append(segment.id)
            return super().synthesize(segment)

    def worker(
        store: RunStore,
        _record: RunRecord,
        *,
        bypass_cache_segment_ids: frozenset[str] = frozenset(),
        **_kwargs: object,
    ) -> LocalWorker:
        return LocalWorker(
            store,
            synthesizer=ProcessedSpeechSynthesizer(Synthesizer()),
            bypass_cache_segment_ids=bypass_cache_segment_ids,
        )

    monkeypatch.setattr("whoopy.cli._worker_for_record", worker)
    return synthesized


def test_cli_completed_take_resumes_then_regenerates_segment_without_web_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, pending = _pending_assembly(tmp_path)
    takes = SegmentTakeStore(store)
    synthesized = _fixture_workers(monkeypatch)
    location = ["--runs-dir", str(store.root), "--json"]

    assert main(["run", "resume", str(RUN_ID), *location]) == 0

    resumed = json.loads(capsys.readouterr().out)
    assert resumed["status"] == "completed"
    assert resumed["execution"]["attempt_id"] != str(pending.assembly_prior_attempt_id)
    assert takes.pending(RUN_ID) == pending
    assert synthesized == []
    assert takes.selected(RUN_ID, "speech-0001") == pending.selected_take_id
    retained = takes.audio_path(RUN_ID, "speech-0001", pending.selected_take_id).read_bytes()
    checkpoints = SegmentCheckpointStore(store)
    untouched = checkpoints.checkpoint_path(RUN_ID, "speech-0002").read_bytes()

    assert main(["run", "regenerate-segment", str(RUN_ID), "speech-0001", *location]) == 0

    regenerated = json.loads(capsys.readouterr().out)
    assert regenerated["status"] == "completed"
    assert regenerated["execution"]["attempt_id"] != resumed["execution"]["attempt_id"]
    assert synthesized == ["speech-0001"]
    assert takes.pending(RUN_ID) is None
    assert takes.selected(RUN_ID, "speech-0001") is None
    assert (
        takes.audio_path(RUN_ID, "speech-0001", pending.selected_take_id).read_bytes() == retained
    )
    assert checkpoints.checkpoint_path(RUN_ID, "speech-0002").read_bytes() == untouched
    receipt = (
        store.run_directory(RUN_ID)
        / "history"
        / "alternative-takes"
        / f"{pending.operation_id}.json"
    )
    assert PendingAlternativeTake.model_validate_json(receipt.read_text()) == pending
    assert store.audio_path(RUN_ID).is_file()


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
def test_cli_segment_regeneration_preserves_unresolved_take_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unresolved: str,
) -> None:
    store, pending = _pending_assembly(tmp_path)
    synthesized = _fixture_workers(monkeypatch)
    location = ["--runs-dir", str(store.root), "--json"]
    if unresolved != "incomplete":
        assert main(["run", "resume", str(RUN_ID), *location]) == 0
        capsys.readouterr()
    record = store.load(RUN_ID)
    assert record.execution is not None
    if unresolved in {"generating", "restoring", "failed"}:
        pending = pending.model_copy(update={"phase": unresolved})
    elif unresolved == "wrong_revision":
        pending = pending.model_copy(
            update={"assembly_revision_id": RevisionStore(store).list(RUN_ID)[0].revision_id}
        )
    elif unresolved == "same_attempt":
        pending = pending.model_copy(
            update={"assembly_prior_attempt_id": record.execution.attempt_id}
        )
    elif unresolved == "missing_revision":
        pending = pending.model_copy(update={"assembly_revision_id": None})
    elif unresolved == "missing_attempt":
        store.save(record.model_copy(update={"execution": None}))
    SegmentTakeStore(store).save_pending(pending)
    before = _artifacts(store.run_directory(RUN_ID))

    with pytest.raises(SystemExit) as stopped:
        main(["run", "regenerate-segment", str(RUN_ID), "speech-0001", *location])

    assert stopped.value.code == 2
    assert "pending alternative take" in capsys.readouterr().err
    assert synthesized == []
    assert _artifacts(store.run_directory(RUN_ID)) == before


def test_delete_reconciles_completed_take_before_archiving_without_web_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store, pending = _pending_assembly(tmp_path)
    _fixture_workers(monkeypatch)
    assert main(["run", "resume", str(RUN_ID), "--runs-dir", str(store.root), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert SegmentTakeStore(store).pending(RUN_ID) == pending

    result = _application(tmp_path).delete_run(str(RUN_ID))

    assert result is not None and result["recoverable"] is True
    assert not store.run_directory(RUN_ID).exists()
    destination = store.root / ".trash" / f"{result['receipt_id']}-{RUN_ID}"
    assert not (destination / "takes" / "pending.json").exists()
    receipt = destination / "history" / "alternative-takes" / f"{pending.operation_id}.json"
    assert PendingAlternativeTake.model_validate_json(receipt.read_text()) == pending
