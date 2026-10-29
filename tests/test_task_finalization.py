from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from queue import Queue
from threading import Event
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from tests.test_revisions import RUN_ID as PLAN_RUN_ID
from tests.test_revisions import _completed_generated_run, _generated, _plan
from tests.test_takes import (
    NOW,
    RUN_ID,
    _alternative_take,
    _completed_run,
    _pending_alternative,
)
from whoopy.audio import TimelineWaveRenderer
from whoopy.audio.exports import ExportArtifact, ExportManifest, ExportMetadata
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import ProcessedSpeechSynthesizer
from whoopy.pipeline import RunExecution, RunStage, RunStatus, RunStore
from whoopy.pipeline.checkpoints import SegmentCheckpointStore
from whoopy.pipeline.regeneration import prepare_segment_regeneration
from whoopy.pipeline.revisions import RevisionStore
from whoopy.pipeline.runs import RunStoreError
from whoopy.pipeline.takes import SegmentTakeStore
from whoopy.pipeline.worker import LocalWorker, WorkerError
from whoopy.webui.api import create_api
from whoopy.webui.server import GenerationTask, LocalWebApplication


def _application(tmp_path: Path, **kwargs: Any) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        **kwargs,
    )


@pytest.mark.parametrize("failure", ["mkdir", "write"])
def test_plan_input_failure_keeps_revision_and_releases_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    application = _application(tmp_path)
    base = _plan()
    store = _completed_generated_run(
        application.runs_directory,
        _generated(base, middle_text="Return to this comfortable anchor with gentle attention."),
    )
    run_id = str(PLAN_RUN_ID)
    previous_task = GenerationTask(task_id=run_id, run_id=run_id, launching=False)
    application._tasks[run_id] = previous_task
    before = {
        path.relative_to(store.run_directory(run_id)): path.read_bytes()
        for path in store.run_directory(run_id).rglob("*")
        if path.is_file()
    }
    input_root = store.run_directory(run_id) / "history" / "editor-inputs"
    mkdir = Path.mkdir
    write_bytes = RunStore._write_bytes

    def fail_mkdir(path: Path, *args: Any, **kwargs: Any) -> None:
        if path == input_root:
            raise OSError("controlled editor-input mkdir failure")
        mkdir(path, *args, **kwargs)

    def fail_write(path: Path, payload: bytes) -> None:
        if path.parent == input_root:
            raise RunStoreError("controlled editor-input write failure")
        write_bytes(path, payload)

    requested = base.model_copy(
        update={
            "sections": [
                base.sections[0],
                base.sections[1].model_copy(update={"purpose": "Return gently to your anchor."}),
                base.sections[2],
            ]
        }
    )
    with monkeypatch.context() as patch:
        if failure == "mkdir":
            patch.setattr(Path, "mkdir", fail_mkdir)
        else:
            patch.setattr(RunStore, "_write_bytes", staticmethod(fail_write))
        with pytest.raises((OSError, RunStoreError), match="controlled editor-input"):
            application.edit_run(run_id, {"plan": requested.model_dump(mode="json")})

    assert application._tasks == {run_id: previous_task}
    assert not previous_task.active
    assert RevisionStore(store).active_revision_id(run_id) is None
    assert {
        path.relative_to(store.run_directory(run_id)): path.read_bytes()
        for path in store.run_directory(run_id).rglob("*")
        if path.is_file()
    } == before
    monkeypatch.setattr(application, "_run_model_pack_task", lambda _task: None)
    assert application.start_model_pack_task("smoke-test", "moss-local-5b", {})["status"] == (
        "queued"
    )


def _write_export(store: RunStore, payload: bytes) -> None:
    directory = store.run_directory(RUN_ID) / "exports"
    directory.mkdir(exist_ok=True)
    (directory / "finished.wav").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = ExportManifest(
        generated_at=datetime.now(UTC),
        source_filename="narration.wav",
        source_byte_count=len(payload),
        source_sha256=digest,
        source_duration_ms=1_000,
        metadata=ExportMetadata(title="Selected take", model="fixture", created_at=NOW),
        ffmpeg_executable="fixture-ffmpeg",
        ffmpeg_version="fixture",
        artifacts=(
            ExportArtifact(
                kind="wav",
                filename="finished.wav",
                codec="pcm_s16le",
                byte_count=len(payload),
                sha256=digest,
                duration_ms=1_000,
                sample_rate=24_000,
                channels=1,
                embedded_tags={},
            ),
        ),
    )
    (directory / "export-manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")


def test_alternative_take_routes_wait_for_finalization_and_selected_reassembly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, original_audio = _completed_run(tmp_path / "runs")
    alternative_audio = PcmAudio(b"\x60\x00" * 480, 24_000)
    checkpoints = SegmentCheckpointStore(store)

    def assemble_checkpoints() -> bytes:
        timeline = store.load_timeline(RUN_ID)
        speech_audio: dict[str, PcmAudio] = {}
        cache_keys: dict[str, str] = {}
        for segment in timeline.segments:
            if segment.type != "SPEECH":
                continue
            checkpoint = checkpoints.load_optional(RUN_ID, segment.id)
            assert checkpoint is not None
            completed = checkpoints.load_completed(
                RUN_ID, segment.id, cache_key=checkpoint.cache_key
            )
            assert completed is not None
            speech_audio[segment.id] = completed.audio
            cache_keys[segment.id] = checkpoint.cache_key
        rendered = TimelineWaveRenderer().render(
            timeline, speech_audio=speech_audio, speech_cache_keys=cache_keys
        )
        store.write_audio(RUN_ID, rendered.wave_bytes)
        store.write_audio_manifest(RUN_ID, rendered.manifest)
        store.write_quality(RUN_ID, rendered.quality)
        return rendered.wave_bytes

    original_master = assemble_checkpoints()
    child_release = Event()
    assembly_release = Event()
    assembly_finished = Event()
    finalizer_finished = Event()
    boundaries = ("capture", "restore", "revision", "resume", "registered")
    arrived: Queue[str] = Queue()
    releases = {name: Event() for name in boundaries}
    failures: Queue[BaseException] = Queue()

    def boundary(name: str) -> None:
        arrived.put(name)
        assert releases[name].wait(10), name

    class Process:
        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            self.command = command[4]
            self.returncode: int | None = None

        def poll(self) -> int | None:
            return self.returncode

        def communicate(self) -> tuple[str, str]:
            if self.command == "regenerate-segment":
                assert child_release.wait(10)
                pending = SegmentTakeStore(store).pending(RUN_ID)
                assert pending is not None
                prepare_segment_regeneration(
                    store, RUN_ID, "speech-0001", regeneration_attempt_id=pending.operation_id
                )
                previous = checkpoints.load_optional(RUN_ID, "speech-0001")
                assert previous is not None
                checkpoint = checkpoints.completed_checkpoint(
                    run_id=RUN_ID,
                    segment_id="speech-0001",
                    cache_key=previous.cache_key,
                    audio=alternative_audio,
                    attempt_count=2,
                    cache_hit=False,
                    started_at=NOW,
                    completed_at=NOW,
                )
                checkpoints.save(checkpoint, audio=alternative_audio)
                alternative_master = assemble_checkpoints()
                assert alternative_master != original_master
                _write_export(store, alternative_master)
                record = store.load(RUN_ID)
                assert record.recovery is not None
                store.save(
                    record.transition(
                        RunStatus.COMPLETED,
                        updated_at=datetime.now(UTC),
                        timeline_artifact="timeline.json",
                        audio_artifact="narration.wav",
                        audio_manifest_artifact="audio-manifest.json",
                        quality_artifact="quality.json",
                        recovery=record.recovery.model_copy(
                            update={
                                "speech_segments_completed": record.recovery.speech_segments_total
                            }
                        ),
                        execution=RunExecution(
                            stage=RunStage.COMPLETED,
                            attempt_id=uuid4(),
                            owner_id="test-worker",
                            pid=1,
                            started_at=NOW,
                        ),
                    )
                )
            else:
                assert self.command == "resume"
                assert assembly_release.wait(10)
                assembled = LocalWorker(
                    store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
                ).resume(RUN_ID)
                assert assembled.status is RunStatus.COMPLETED
                assert store.audio_path(RUN_ID).read_bytes() == original_master
            self.returncode = 0
            return "{}", ""

    application = _application(tmp_path, command_launcher=Process)
    capture = SegmentTakeStore.capture_active
    choose = SegmentTakeStore.choose
    revise = RevisionStore.create_assembly_revision
    resume = application._resume_run_admitted
    finalize = application._finalize_alternative_take
    recover = application._run_recovery_command

    def capture_at_boundary(takes: SegmentTakeStore, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("source") == "production_regeneration":
            boundary("capture")
        return capture(takes, *args, **kwargs)

    def restore_at_boundary(takes: SegmentTakeStore, *args: Any, **kwargs: Any) -> Any:
        if kwargs.get("action") == "restore":
            boundary("restore")
        return choose(takes, *args, **kwargs)

    def revise_at_boundary(revisions: RevisionStore, *args: Any, **kwargs: Any) -> Any:
        boundary("revision")
        return revise(revisions, *args, **kwargs)

    def resume_at_boundary(run_id: str) -> dict[str, Any] | None:
        boundary("resume")
        result = resume(run_id)
        boundary("registered")
        return result

    def finish_finalizer(*args: Any) -> None:
        try:
            finalize(*args)
        except BaseException as error:
            failures.put(error)
        finally:
            finalizer_finished.set()

    def finish_worker(task: GenerationTask, command: str, segment_id: str | None) -> None:
        try:
            recover(task, command, segment_id)
        except BaseException as error:
            failures.put(error)
        finally:
            if command == "resume":
                assembly_finished.set()

    monkeypatch.setattr(SegmentTakeStore, "capture_active", capture_at_boundary)
    monkeypatch.setattr(SegmentTakeStore, "choose", restore_at_boundary)
    monkeypatch.setattr(RevisionStore, "create_assembly_revision", revise_at_boundary)
    monkeypatch.setattr(application, "_resume_run_admitted", resume_at_boundary)
    monkeypatch.setattr(application, "_finalize_alternative_take", finish_finalizer)
    monkeypatch.setattr(application, "_run_recovery_command", finish_worker)

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        prefix = f"/api/v1/runs/{RUN_ID}"
        assert client.get(f"{prefix}/audio").content == original_master
        response = client.post(f"{prefix}/segments/speech-0001/takes", json={})
        assert response.status_code == 202
        selected_take = response.json()["selected_take_id"]
        assert response.json()["task"]["status"] == "running"
        assert response.json()["task"]["has_audio"] is False
        child_release.set()
        try:
            for name in boundaries:
                assert arrived.get(timeout=5) == name
                for path in (f"/api/v1/tasks/{RUN_ID}", prefix):
                    state = client.get(path)
                    assert state.status_code == 200
                    assert state.json()["status"] == "running"
                    assert state.json()["has_audio"] is False
                    assert state.json()["resumable"] is False
                assert client.get(f"{prefix}/audio").status_code == 404
                assert client.get(f"{prefix}/waveform").status_code == 404
                assert client.get(f"{prefix}/exports").json() == {
                    "available": False,
                    "artifacts": [],
                }
                assert client.get(f"{prefix}/exports/wav").status_code == 404
                assert client.post(f"{prefix}/exports", json={}).status_code == 400
                releases[name].set()
            assert finalizer_finished.wait(5)
            assert client.get(f"/api/v1/tasks/{RUN_ID}").json()["status"] == "running"
            assert client.get(f"{prefix}/audio").status_code == 404
            assembly_release.set()
            assert assembly_finished.wait(5)
            assert failures.empty()
            assert client.get(f"/api/v1/tasks/{RUN_ID}").json()["status"] == "completed"
            assert client.get(f"{prefix}/audio").content == original_master
            assert client.get(f"{prefix}/exports").json()["available"] is False
        finally:
            child_release.set()
            assembly_release.set()
            for release in releases.values():
                release.set()
            finalizer_finished.wait(5)
            assembly_finished.wait(5)

    takes = SegmentTakeStore(store)
    assert takes.selected(RUN_ID, "speech-0001") == selected_take
    generated = [
        take
        for take in takes.list(RUN_ID, "speech-0001")
        if take.source == "production_regeneration"
    ]
    assert len(generated) == 1
    assert generated[0].pcm_sha256 == hashlib.sha256(alternative_audio.pcm_s16le).hexdigest()
    assert checkpoints.audio_path(RUN_ID, "speech-0001").read_bytes() == original_audio.pcm_s16le


@pytest.mark.parametrize("failure", ["capture", "restore", "revision", "resume"])
def test_failed_take_finalization_hides_temporary_audio_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    application = _application(tmp_path)
    store, _audio = _completed_run(application.runs_directory)
    selected, _alternative, _alternative_audio = _alternative_take(store)
    _pending_alternative(store, selected)
    temporary_master = b"temporary alternative narration"
    store.audio_path(RUN_ID).write_bytes(temporary_master)
    _write_export(store, temporary_master)
    run_id = str(RUN_ID)
    task = GenerationTask(
        task_id=run_id,
        run_id=run_id,
        mode="alternative_take",
        launching=False,
        finalizing_take=True,
    )
    application._tasks[run_id] = task

    def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RunStoreError(f"controlled {failure} failure: input_value=private-take-text")

    target, method = {
        "capture": (SegmentTakeStore, "capture_active"),
        "restore": (SegmentTakeStore, "choose"),
        "revision": (RevisionStore, "create_assembly_revision"),
        "resume": (application, "_resume_run_admitted"),
    }[failure]
    with monkeypatch.context() as patch:
        patch.setattr(target, method, fail)
        application._finalize_alternative_take(task, "speech-0001", selected)

    assert not task.finalizing_take
    assert task.error is not None and "RunStoreError" in task.error
    assert store.load(RUN_ID).status is RunStatus.FAILED
    assert "private-take-text" not in store.load(RUN_ID).model_dump_json()
    for current in (application, _application(tmp_path)):
        with TestClient(create_api(current), base_url="http://127.0.0.1") as client:
            prefix = f"/api/v1/runs/{RUN_ID}"
            state = client.get(f"/api/v1/tasks/{RUN_ID}")
            assert state.json()["status"] == "failed"
            assert state.json()["has_audio"] is False
            assert state.json()["resumable"] is False
            assert "private-take-text" not in state.text
            assert client.get(f"{prefix}/audio").status_code == 404
            assert client.get(f"{prefix}/exports").json()["available"] is False
            assert client.get(f"{prefix}/exports/wav").status_code == 404
            assert client.post(f"{prefix}/exports", json={}).status_code == 400
        with pytest.raises(ValueError, match="Choose a retained take"):
            current.resume_run(run_id)
    failed_record = store.load(RUN_ID)
    checkpoint = SegmentCheckpointStore(store).audio_path(RUN_ID, "speech-0001").read_bytes()
    with pytest.raises(WorkerError, match="Choose a retained take"):
        LocalWorker(store).resume(RUN_ID)
    assert store.load(RUN_ID) == failed_record
    assert (
        SegmentCheckpointStore(store).audio_path(RUN_ID, "speech-0001").read_bytes() == checkpoint
    )
