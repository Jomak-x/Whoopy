from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whoopy.artifacts import TargetPlatform
from whoopy.audio.exports import ExportArtifact, ExportManifest, ExportMetadata
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import AudioManifest, PcmAudio
from whoopy.audio.processing import ProcessedSpeechSynthesizer, SpeechProcessingSettings
from whoopy.meditation import MeditationPlan, PlannedSection
from whoopy.pipeline import RunExecution, RunStage, RunStatus, RunStore
from whoopy.pipeline.generation import RunModelMetadata, ScriptRunConfig, TTSRunSettings
from whoopy.pipeline.revisions import RevisionRequest, RevisionStore
from whoopy.pipeline.worker import LocalWorker, RetryPolicy, WorkerError
from whoopy.timeline import SpeechSegment, build_script_timeline
from whoopy.webui.api import create_api
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


def _rendered_script_run(application: LocalWebApplication) -> tuple[RunStore, str]:
    store = RunStore(application.runs_directory)
    script = "Notice the room around you.\n\n[pause: 1s]\n\nFeel the support beneath you."
    config = ScriptRunConfig(
        profile="basic",
        target=TargetPlatform(operating_system="linux", architecture="x86_64"),
        tts=TTSRunSettings(
            voice_name="af_heart",
            speaker_id=3,
            speed=0.9,
            num_threads=2,
            provider="cpu",
            language="en-us",
        ),
        processing=SpeechProcessingSettings(),
    )
    record = store.create_script_run(
        script=script,
        source_name="playback.md",
        resolved_config=config,
        model_metadata=RunModelMetadata(tts=FixtureSpeechSynthesizer.metadata),
    )
    store.write_timeline(
        record.run_id,
        build_script_timeline(
            run_id=record.run_id,
            script=script,
            created_at=record.created_at,
        ),
    )
    completed = LocalWorker(
        store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
    ).process(record.run_id)
    assert completed.status is RunStatus.COMPLETED
    return store, str(record.run_id)


def _save_export(store: RunStore, run_id: str) -> Path:
    audio = store.audio_path(run_id)
    payload = audio.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    duration_ms = AudioManifest.model_validate_json(
        store.audio_manifest_path(run_id).read_text()
    ).duration_ms
    export_directory = audio.parent / "exports"
    export_directory.mkdir()
    exported = export_directory / "finished.wav"
    exported.write_bytes(payload)
    manifest = ExportManifest(
        generated_at=datetime.now(UTC),
        source_filename="narration.wav",
        source_byte_count=len(payload),
        source_sha256=digest,
        source_duration_ms=duration_ms,
        metadata=ExportMetadata(
            title="A quiet moment.", model="fixture", created_at=datetime.now(UTC)
        ),
        ffmpeg_executable="fixture-ffmpeg",
        ffmpeg_version="fixture",
        artifacts=(
            ExportArtifact(
                kind="wav",
                filename=exported.name,
                codec="pcm_s16le",
                byte_count=len(payload),
                sha256=digest,
                duration_ms=duration_ms,
                sample_rate=24_000,
                channels=1,
                embedded_tags={},
            ),
        ),
    )
    (export_directory / "export-manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    return exported


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_export_listing_and_download_reject_stale_narration(tmp_path: Path, change: str) -> None:
    application = _application(tmp_path)
    store, run_id = _rendered_script_run(application)
    audio = store.audio_path(run_id)
    original = audio.read_bytes()
    exported = _save_export(store, run_id)
    available = application.exports(run_id)
    assert available is not None and available["available"] is True
    assert application.export_path(run_id, "wav") == exported

    if change == "replace":
        audio.write_bytes(b"narration revision B")
    else:
        audio.unlink()

    assert application.exports(run_id) == {"available": False, "artifacts": []}
    assert application.export_path(run_id, "wav") is None
    assert exported.read_bytes() == original


def test_failed_edited_render_hides_stale_playback_quality_and_exports(tmp_path: Path) -> None:
    application = _application(tmp_path)
    store, run_id = _rendered_script_run(application)
    exported = _save_export(store, run_id)
    preserved = {
        path: path.read_bytes()
        for path in (
            store.audio_path(run_id),
            store.audio_manifest_path(run_id),
            store.quality_path(run_id),
            exported,
        )
    }
    client = TestClient(create_api(application), base_url="http://127.0.0.1")
    prefix = f"/api/v1/runs/{run_id}"
    for suffix in ("/audio", "/artifact/quality", "/artifact/manifest", "/exports/wav"):
        assert client.get(prefix + suffix).status_code == 200
    assert client.get(prefix).json()["quality_passed"] is True
    assert client.get(prefix).json()["has_audio"] is True
    old_script = store.load_script(run_id)
    RevisionStore(store).create(
        run_id,
        RevisionRequest(script=old_script.replace("Notice the room", "Listen to distant sounds")),
    )

    class FailedSpeech(FixtureSpeechSynthesizer):
        def synthesize(self, segment: SpeechSegment) -> PcmAudio:
            raise RuntimeError("controlled revised speech failure")

    with pytest.raises(WorkerError, match="controlled revised speech failure"):
        LocalWorker(
            store,
            synthesizer=ProcessedSpeechSynthesizer(FailedSpeech()),
            retry_policy=RetryPolicy(max_attempts=1),
        ).resume(run_id)

    assert store.load(run_id).status is RunStatus.FAILED
    assert store.load_script(run_id) != old_script
    assert application.waveform(run_id, points=100) is None
    assert application.sections(run_id) is None
    assert application.segment_audition(run_id, "speech-0001") is None
    for suffix in ("/audio", "/artifact/quality", "/artifact/manifest", "/exports/wav"):
        assert client.get(prefix + suffix).status_code == 404
    summary = client.get(prefix).json()
    assert summary["has_audio"] is False
    assert summary["quality_passed"] is None
    assert client.get(prefix + "/exports").json() == {"available": False, "artifacts": []}
    assert {path: path.read_bytes() for path in preserved} == preserved

    assert (
        LocalWorker(store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer()))
        .resume(run_id)
        .status
        is RunStatus.COMPLETED
    )
    assert client.get(prefix + "/audio").status_code == 200
    assert client.get(prefix).json()["quality_passed"] is True
    assert client.get(prefix + "/exports").json()["available"] is False


@pytest.mark.parametrize(
    "change", ["pace", "pause", "processing", "speed", "voice", "reference", "quality"]
)
def test_current_playback_rejects_changed_canonical_identity(tmp_path: Path, change: str) -> None:
    application = _application(tmp_path)
    store, run_id = _rendered_script_run(application)
    original_audio = store.audio_path(run_id).read_bytes()
    assert application.audio_path(run_id) == store.audio_path(run_id)
    if change in {"pace", "pause"}:
        timeline = store.load_timeline(run_id)
        segments = list(timeline.segments)
        index = 0 if change == "pace" else 1
        update = {"pace": "slow"} if change == "pace" else {"duration_ms": 2_000}
        segments[index] = segments[index].model_copy(update=update)
        store.write_timeline(run_id, timeline.model_copy(update={"segments": segments}))
    elif change in {"processing", "speed", "voice", "reference"}:
        config = store.load_resolved_config(run_id)
        if change == "processing":
            config = config.model_copy(
                update={"processing": replace(config.processing, edge_fade_ms=25)}
            )
        else:
            update = (
                {"speed": 1.2}
                if change == "speed"
                else (
                    {"voice_name": "am_michael"}
                    if change == "voice"
                    else {"reference_audio_sha256": "0" * 64}
                )
            )
            config = config.model_copy(update={"tts": config.tts.model_copy(update=update)})
        store._write_json(store.resolved_config_path(run_id), config.model_dump_json())
    else:
        path = store.quality_path(run_id)
        quality = json.loads(path.read_text())
        quality["pcm_sha256"] = "0" * 64
        path.write_text(json.dumps(quality))
    assert application.audio_path(run_id) is None
    assert application.artifact_path(run_id, "quality") is None
    summary = application.run(run_id)
    assert summary is not None
    assert summary["quality_passed"] is None
    assert summary["has_audio"] is False
    assert store.audio_path(run_id).read_bytes() == original_audio
