from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whoopy.audio.exports import ExportArtifact, ExportManifest, ExportMetadata
from whoopy.meditation import MeditationPlan, PlannedSection
from whoopy.pipeline import RunExecution, RunStage, RunStatus, RunStore
from whoopy.timeline import build_script_timeline
from whoopy.webui.server import GenerationTask, LocalWebApplication


def _application(tmp_path: Path) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )


def _plan() -> MeditationPlan:
    practice = PlannedSection(
        id="practice",
        title="Practice",
        purpose="Return to a comfortable anchor.",
        technique="focused_attention",
        beat="practice",
        delivery="calm",
        target_speech_seconds=20,
        pause_after_ms=6_000,
        minimum_words=8,
        maximum_words=30,
    )
    return MeditationPlan(
        title="A Steady Moment",
        intention="Practice steady attention.",
        requested_duration_seconds=300,
        silence_budget_seconds=18,
        breath_mode="natural_awareness",
        sections=[
            practice.model_copy(
                update={
                    "id": "arrival",
                    "beat": "arrival",
                    "technique": "arrival",
                    "delivery": "warm",
                }
            ),
            practice,
            practice.model_copy(
                update={
                    "id": "ending",
                    "beat": "ending",
                    "technique": "return",
                    "delivery": "encouraging",
                }
            ),
        ],
    )


@pytest.mark.parametrize("operation", ["plan", "resume", "regenerate-segment"])
@pytest.mark.parametrize("blocker", ["model", "generation"])
def test_generation_recovery_waits_for_active_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, blocker: str
) -> None:
    application = _application(tmp_path)
    store = RunStore(application.runs_directory)
    record = store.create("Practice steady attention.")
    run_id = str(record.run_id)
    assert record.recovery is not None
    store.save(
        record.transition(
            RunStatus.INTERRUPTED,
            updated_at=datetime.now(UTC),
            recovery=record.recovery.model_copy(update={"process_attempts": 1}),
            execution=RunExecution(stage=RunStage.COMPILING, interruption_kind="test_interruption"),
        )
    )
    timeline = build_script_timeline(
        run_id=record.run_id, script="Notice the room around you.", created_at=datetime.now(UTC)
    )
    store.write_timeline(record.run_id, timeline)
    base = _plan()
    store.plan_path(run_id).write_text(base.model_dump_json(), encoding="utf-8")
    requested = base.model_copy(
        update={
            "sections": [
                base.sections[0],
                base.sections[1].model_copy(
                    update={"purpose": "Name distraction softly and return to the chosen anchor."}
                ),
                base.sections[2],
            ]
        }
    )
    monkeypatch.setattr("whoopy.webui.server.threading.Thread.start", lambda _thread: None)
    if blocker == "model":
        smoke = application.start_model_pack_task("smoke-test", "moss-local-5b", {})
        assert smoke["status"] == "queued"
        expected = "active model or runtime operation"
    else:
        other = store.create("Another meditation is generating.")
        other_id = str(other.run_id)
        application._tasks[other_id] = GenerationTask(task_id=other_id, run_id=other_id)
        expected = "generation worker is already active"
    initial_tasks = dict(application._tasks)

    with pytest.raises(ValueError, match=expected):
        if operation == "plan":
            application.edit_run(run_id, {"plan": requested.model_dump(mode="json")})
        elif operation == "resume":
            application.resume_run(run_id)
        else:
            application.regenerate_segment(run_id, timeline.segments[0].id)

    assert application._tasks == initial_tasks
    assert not (application.runs_directory / run_id / "history" / "editor-inputs").exists()
    assert store.load(run_id).status is RunStatus.INTERRUPTED


@pytest.mark.parametrize("blocker", ["model", "generation"])
def test_cancel_recovery_remains_available_during_active_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    application = _application(tmp_path)
    record = RunStore(application.runs_directory).create("Cancel this meditation.")
    monkeypatch.setattr("whoopy.webui.server.threading.Thread.start", lambda _thread: None)
    if blocker == "model":
        application.start_model_pack_task("smoke-test", "moss-local-5b", {})
    else:
        other = RunStore(application.runs_directory).create("Another meditation is generating.")
        other_id = str(other.run_id)
        application._tasks[other_id] = GenerationTask(task_id=other_id, run_id=other_id)

    result = application._launch_recovery_command(str(record.run_id), "cancel", prevalidated=True)

    assert result is not None
    assert result["run_id"] == str(record.run_id)


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_export_listing_and_download_reject_stale_narration(tmp_path: Path, change: str) -> None:
    application = _application(tmp_path)
    store = RunStore(application.runs_directory)
    record = store.create("A quiet moment.")
    run_id = str(record.run_id)
    audio = store.audio_path(run_id)
    audio.write_bytes(b"narration revision A")
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    export_directory = audio.parent / "exports"
    export_directory.mkdir()
    exported = export_directory / "finished.wav"
    exported.write_bytes(b"narration revision A")
    manifest = ExportManifest(
        generated_at=datetime.now(UTC),
        source_filename="narration.wav",
        source_byte_count=audio.stat().st_size,
        source_sha256=digest,
        source_duration_ms=1_000,
        metadata=ExportMetadata(
            title="A quiet moment.", model="kokoro", created_at=datetime.now(UTC)
        ),
        ffmpeg_executable="fixture-ffmpeg",
        ffmpeg_version="fixture",
        artifacts=(
            ExportArtifact(
                kind="wav",
                filename=exported.name,
                codec="pcm_s16le",
                byte_count=exported.stat().st_size,
                sha256=digest,
                duration_ms=1_000,
                sample_rate=24_000,
                channels=1,
                embedded_tags={},
            ),
        ),
    )
    (export_directory / "export-manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    available = application.exports(run_id)
    assert available is not None and available["available"] is True
    assert application.export_path(run_id, "wav") == exported

    if change == "replace":
        audio.write_bytes(b"narration revision B")
    else:
        audio.unlink()

    assert application.exports(run_id) == {"available": False, "artifacts": []}
    assert application.export_path(run_id, "wav") is None
    assert exported.read_bytes() == b"narration revision A"
