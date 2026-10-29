from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

import pytest

from tests.test_revisions import RUN_ID as PLAN_RUN_ID
from tests.test_revisions import _completed_generated_run, _generated, _plan
from tests.test_take_restart_recovery import _assembly_launcher, _wait_status
from tests.test_takes import RUN_ID, _completed_run
from tests.test_task_finalization import _application
from whoopy.pipeline.checkpoints import SegmentCheckpointStore
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.revisions import RevisionError, RevisionRequest, RevisionStore, TimelineEdit
from whoopy.pipeline.runs import RunExecution, RunStage, RunStatus
from whoopy.pipeline.takes import PendingAlternativeTake, SegmentTakeStore


def _artifacts(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize("mutation", ["script", "controls", "restore"])
@pytest.mark.parametrize("phase", ["generating", "restoring", "assembling", "failed"])
def test_pending_alternative_rejects_revision_without_changing_artifacts(
    tmp_path: Path,
    mutation: str,
    phase: Literal["generating", "restoring", "assembling", "failed"],
) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    revisions = RevisionStore(store)
    revision = revisions.create_assembly_revision(RUN_ID, note="Original selected audio")
    takes = SegmentTakeStore(store)
    selected = takes.capture_active(RUN_ID, "speech-0001", source="original")
    takes.save_pending(
        PendingAlternativeTake(
            operation_id=uuid4(),
            run_id=RUN_ID,
            segment_id="speech-0001",
            selected_take_id=selected.take_id,
            phase=phase,
        )
    )
    directory = store.run_directory(RUN_ID)
    before = _artifacts(directory)

    with pytest.raises(RevisionError, match="pending alternative"):
        if mutation == "restore":
            revisions.restore(RUN_ID, revision.revision_id)
        elif mutation == "script":
            revisions.create(
                RUN_ID,
                RevisionRequest(script=store.load_script(RUN_ID).replace("Welcome", "Hello")),
            )
        else:
            revisions.create(
                RUN_ID, RevisionRequest(timeline=TimelineEdit(pace={"speech-0001": "slow"}))
            )

    assert _artifacts(directory) == before
    assert takes.selected(RUN_ID, "speech-0001") == selected.take_id


@pytest.mark.parametrize("caller_holds_lock", [False, True])
def test_pending_alternative_rejects_semantic_plan_commit_without_changes(
    tmp_path: Path, caller_holds_lock: bool
) -> None:
    base = _plan()
    original = _generated(base, middle_text="Notice the steady support beneath your body now.")
    store = _completed_generated_run(tmp_path / "runs", original)
    requested = base.model_copy(update={"requested_outcome": "Support calm and patient attention."})
    generated = _generated(
        requested, middle_text="Return gently to the support beneath your body now."
    )
    takes = SegmentTakeStore(store)
    takes.save_pending(
        PendingAlternativeTake(
            operation_id=uuid4(),
            run_id=PLAN_RUN_ID,
            segment_id="speech-0001",
            selected_take_id="t0001-0123456789",
            phase="failed",
            error="Finalization stopped.",
        )
    )
    revisions = RevisionStore(store)
    directory = store.run_directory(PLAN_RUN_ID)
    before = _artifacts(directory)

    with pytest.raises(RevisionError, match="pending alternative"):
        if caller_holds_lock:
            with RunLock(directory):
                revisions.create_semantic_plan_revision_locked(
                    PLAN_RUN_ID,
                    base_plan=base,
                    base_revision_id=None,
                    requested_plan=requested,
                    generated=generated,
                    note="Edited outcome",
                )
        else:
            revisions.create_semantic_plan_revision(
                PLAN_RUN_ID,
                base_plan=base,
                base_revision_id=None,
                requested_plan=requested,
                generated=generated,
                note="Edited outcome",
            )

    assert _artifacts(directory) == before


def test_failed_alternative_edit_rejection_preserves_retained_take_recovery(
    tmp_path: Path,
) -> None:
    store, original_audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    selected = takes.capture_active(RUN_ID, "speech-0001", source="original")
    pending = PendingAlternativeTake(
        operation_id=uuid4(),
        run_id=RUN_ID,
        segment_id="speech-0001",
        selected_take_id=selected.take_id,
        phase="failed",
        error="Finalization stopped.",
    )
    takes.save_pending(pending)
    store.save(
        store.load(RUN_ID).transition(
            RunStatus.FAILED,
            updated_at=datetime.now(UTC),
            execution=RunExecution(
                stage=RunStage.ASSEMBLING,
                interruption_kind="take_finalization_failed",
                message=pending.error,
            ),
            error=pending.error,
        )
    )
    directory = store.run_directory(RUN_ID)
    before = _artifacts(directory)
    with pytest.raises(RevisionError, match="pending alternative"):
        RevisionStore(store).create(
            RUN_ID, RevisionRequest(script=store.load_script(RUN_ID).replace("Welcome", "Hello"))
        )
    assert _artifacts(directory) == before

    application = _application(tmp_path, command_launcher=_assembly_launcher(store))
    result = application.choose_segment_take(str(RUN_ID), "speech-0001", selected.take_id)
    assert result is not None
    assert result["revision"]["change_kind"] == "assembly_only"
    _wait_status(application, "completed")

    assert takes.pending(RUN_ID) is None
    assert takes.selected(RUN_ID, "speech-0001") == selected.take_id
    assert SegmentCheckpointStore(store).audio_path(RUN_ID, "speech-0001").read_bytes() == (
        original_audio.pcm_s16le
    )
    assert application.audio_path(str(RUN_ID)) is not None


def test_unreadable_pending_alternative_rejects_revision_without_changes(tmp_path: Path) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    directory = store.run_directory(RUN_ID)
    pending = directory / "takes" / "pending.json"
    pending.parent.mkdir()
    pending.write_text("truncated pending state", encoding="utf-8")
    before = _artifacts(directory)

    with pytest.raises(RevisionError, match="pending alternative take is unreadable"):
        RevisionStore(store).create(
            RUN_ID, RevisionRequest(timeline=TimelineEdit(pace={"speech-0001": "slow"}))
        )

    assert _artifacts(directory) == before
