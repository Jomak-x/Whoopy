from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Literal
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient

from whoopy.artifacts import TargetPlatform
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import ProcessedSpeechSynthesizer, SpeechProcessingSettings
from whoopy.audio.synthesis import cache_key_for
from whoopy.pipeline.checkpoints import SegmentCheckpointStore
from whoopy.pipeline.generation import RunModelMetadata, ScriptRunConfig, TTSRunSettings
from whoopy.pipeline.regeneration import prepare_segment_regeneration
from whoopy.pipeline.revisions import RevisionRequest, RevisionStore, TimelineEdit
from whoopy.pipeline.runs import (
    AUDIO_FILENAME,
    AUDIO_MANIFEST_FILENAME,
    QUALITY_FILENAME,
    TIMELINE_FILENAME,
    RunExecution,
    RunRecord,
    RunRecovery,
    RunStage,
    RunStatus,
    RunStore,
)
from whoopy.pipeline.takes import (
    PendingAlternativeTake,
    SegmentTakeError,
    SegmentTakeManifest,
    SegmentTakeStore,
)
from whoopy.timeline import build_script_timeline
from whoopy.webui.api import create_api
from whoopy.webui.server import GenerationTask, LocalWebApplication

RUN_ID = UUID("40404040-4040-4040-8040-404040404040")
NOW = datetime(2026, 8, 5, tzinfo=UTC)
SCRIPT = "Welcome.\n\n[pause: 3s]\n\nNotice the support beneath you.\n"


def _completed_run(root: Path) -> tuple[RunStore, PcmAudio]:
    store = RunStore(root)
    config = ScriptRunConfig(
        profile="basic",
        target=TargetPlatform(operating_system="linux", architecture="x86_64"),
        tts=TTSRunSettings(
            backend="moss-local-v1.5",
            voice_name="reference",
            speaker_id=0,
            speed=0.8,
            num_threads=2,
            provider="cpu",
            language="English",
        ),
        processing=SpeechProcessingSettings(),
    )
    queued = store.create_script_run(
        script=SCRIPT,
        source_name="takes.md",
        resolved_config=config,
        model_metadata=RunModelMetadata(tts=FixtureSpeechSynthesizer.metadata),
        run_id=RUN_ID,
        created_at=NOW,
    )
    timeline = build_script_timeline(run_id=RUN_ID, script=SCRIPT, created_at=NOW)
    store.write_timeline(RUN_ID, timeline)
    store.audio_path(RUN_ID).write_bytes(b"wave")
    store.audio_manifest_path(RUN_ID).write_text("{}")
    store.quality_path(RUN_ID).write_text("{}")
    speech_segments = [segment for segment in timeline.segments if segment.type == "SPEECH"]
    original = PcmAudio(pcm_s16le=b"\x10\x00" * 480, sample_rate=24_000)
    checkpoints = SegmentCheckpointStore(store)
    for index, segment in enumerate(speech_segments):
        audio = original if index == 0 else PcmAudio(b"\x20\x00" * 480, 24_000)
        checkpoint = checkpoints.completed_checkpoint(
            run_id=RUN_ID,
            segment_id=segment.id,
            cache_key=cache_key_for(
                segment, ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
            ),
            audio=audio,
            attempt_count=1,
            cache_hit=False,
            started_at=NOW,
            completed_at=NOW,
        )
        checkpoints.save(checkpoint, audio=audio)
    completed = queued.transition(
        RunStatus.COMPLETED,
        updated_at=NOW,
        timeline_artifact=TIMELINE_FILENAME,
        audio_artifact=AUDIO_FILENAME,
        audio_manifest_artifact=AUDIO_MANIFEST_FILENAME,
        quality_artifact=QUALITY_FILENAME,
        recovery=RunRecovery(
            process_attempts=1,
            resume_count=0,
            cache_hits=0,
            cache_misses=len(speech_segments),
            checkpoint_reuses=0,
            speech_segments_total=len(speech_segments),
            speech_segments_completed=len(speech_segments),
        ),
        execution=RunExecution(stage=RunStage.COMPLETED),
    )
    store.save(completed)
    return store, original


def _pending_alternative(store: RunStore, selected: str) -> PendingAlternativeTake:
    pending = PendingAlternativeTake(
        operation_id=uuid4(),
        run_id=RUN_ID,
        segment_id="speech-0001",
        selected_take_id=selected,
    )
    SegmentTakeStore(store).save_pending(pending)
    record = store.load(RUN_ID)
    prepare_segment_regeneration(
        store, RUN_ID, "speech-0001", regeneration_attempt_id=pending.operation_id
    )
    store.save(
        record.model_copy(
            update={
                "execution": RunExecution(
                    stage=RunStage.COMPLETED,
                    attempt_id=uuid4(),
                    owner_id="test-worker",
                    pid=1,
                    started_at=NOW,
                )
            }
        )
    )
    return pending


def test_production_checkpoints_become_immutable_takes_and_choices_only_reassemble(
    tmp_path: Path,
) -> None:
    store, original_audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    checkpoints = SegmentCheckpointStore(store)
    original = takes.capture_active(RUN_ID, "speech-0001", source="original")
    alternative_audio = PcmAudio(b"\x30\x00" * 480, 24_000)
    alternative_checkpoint = checkpoints.completed_checkpoint(
        run_id=RUN_ID,
        segment_id="speech-0001",
        cache_key=original.cache_key,
        audio=alternative_audio,
        attempt_count=2,
        cache_hit=False,
        started_at=NOW,
        completed_at=NOW,
    )
    checkpoints.save(alternative_checkpoint, audio=alternative_audio)
    alternative = takes.capture_active(
        RUN_ID,
        "speech-0001",
        source="production_regeneration",
        select_if_unset=False,
    )

    takes.choose(RUN_ID, "speech-0001", original.take_id)
    selection = takes.choose(RUN_ID, "speech-0001", alternative.take_id)
    revision = RevisionStore(store).create_assembly_revision(
        RUN_ID,
        note=f"Selected take {alternative.take_id}.",
    )

    assert original.take_id != alternative.take_id
    assert original.pcm_sha256 != alternative.pcm_sha256
    assert selection.previous_take_id == original.take_id
    assert takes.selected(RUN_ID, "speech-0001") == alternative.take_id
    assert checkpoints.audio_path(RUN_ID, "speech-0001").read_bytes() == (
        alternative_audio.pcm_s16le
    )
    assert revision.change_kind == "assembly_only"
    assert revision.affected_speech_segment_ids == ()
    execution = store.load(RUN_ID).execution
    assert execution is not None
    assert execution.stage is RunStage.ASSEMBLING
    assert len(takes.list(RUN_ID, "speech-0001")) == 2
    assert (
        takes.audio_path(RUN_ID, "speech-0001", original.take_id).read_bytes().startswith(b"RIFF")
    )

    undone = takes.undo(RUN_ID, "speech-0001")
    RevisionStore(store).create_assembly_revision(RUN_ID, note="Undid take selection.")

    assert undone.action == "undo"
    assert undone.selected_take_id == original.take_id
    assert checkpoints.audio_path(RUN_ID, "speech-0001").read_bytes() == original_audio.pcm_s16le
    assert len(RevisionStore(store).list(RUN_ID)) == 3


def test_corrupt_take_audio_is_refused_without_changing_selection(tmp_path: Path) -> None:
    store, _original_audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    original = takes.capture_active(RUN_ID, "speech-0001", source="original")
    path = store.run_directory(RUN_ID) / "takes" / "speech-0001" / original.take_id / "audio.pcm"
    path.write_bytes(b"corrupt")

    with pytest.raises(SegmentTakeError, match="digest"):
        takes.choose(RUN_ID, "speech-0001", original.take_id)

    assert takes.selected(RUN_ID, "speech-0001") == original.take_id


def test_versioned_take_api_lists_previews_and_selects_without_resynthesis(
    tmp_path: Path,
) -> None:
    store, _original_audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    checkpoints = SegmentCheckpointStore(store)
    original = takes.capture_active(RUN_ID, "speech-0001", source="original")
    alternative_audio = PcmAudio(b"\x40\x00" * 480, 24_000)
    alternative_checkpoint = checkpoints.completed_checkpoint(
        run_id=RUN_ID,
        segment_id="speech-0001",
        cache_key=original.cache_key,
        audio=alternative_audio,
        attempt_count=2,
        cache_hit=False,
        started_at=NOW,
        completed_at=NOW,
    )
    checkpoints.save(alternative_checkpoint, audio=alternative_audio)
    alternative = takes.capture_active(
        RUN_ID,
        "speech-0001",
        source="production_regeneration",
        select_if_unset=False,
    )

    class FinishedProcess:
        returncode = 0

        def __init__(self, _command: list[str], **_kwargs: object) -> None:
            pass

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FinishedProcess,
    )
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        listed = client.get(f"/api/v1/runs/{RUN_ID}/segments/speech-0001/takes")
        preview = client.get(
            f"/api/v1/runs/{RUN_ID}/segments/speech-0001/takes/{original.take_id}/audio"
        )
        chosen = client.post(
            f"/api/v1/runs/{RUN_ID}/segments/speech-0001/takes/{alternative.take_id}/choose",
            json={},
        )

    assert listed.status_code == 200
    assert len(listed.json()["takes"]) == 2
    assert preview.status_code == 200
    assert preview.content.startswith(b"RIFF")
    assert chosen.status_code == 202
    assert chosen.json()["selection"]["selected_take_id"] == alternative.take_id
    assert chosen.json()["revision"]["change_kind"] == "assembly_only"


@pytest.mark.parametrize("action", ["choose", "undo", "restore"])
@pytest.mark.parametrize("change", ["text", "delivery"])
def test_incompatible_takes_do_not_replace_checkpoint_or_selection(
    tmp_path: Path,
    action: Literal["choose", "undo", "restore"],
    change: str,
) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    original = takes.capture_active(RUN_ID, "speech-0001", source="original")
    takes.choose(RUN_ID, "speech-0001", original.take_id)
    request = (
        RevisionRequest(script=SCRIPT.replace("Welcome.", "Settle into your chair."))
        if change == "text"
        else RevisionRequest(timeline=TimelineEdit(delivery={"speech-0001": "warm"}))
    )
    RevisionStore(store).create(RUN_ID, request)
    checkpoint_root = SegmentCheckpointStore(store).segment_directory(RUN_ID, "speech-0001")
    take_root = store.run_directory(RUN_ID) / "takes" / "speech-0001"
    before = {
        path: path.read_bytes()
        for root in (checkpoint_root, take_root)
        for path in root.rglob("*")
        if path.is_file()
    }

    with pytest.raises(SegmentTakeError, match="incompatible"):
        if action == "undo":
            takes.undo(RUN_ID, "speech-0001")
        else:
            takes.choose(RUN_ID, "speech-0001", original.take_id, action=action)

    after = {
        path: path.read_bytes()
        for root in (checkpoint_root, take_root)
        for path in root.rglob("*")
        if path.is_file()
    }
    assert after == before
    assert takes.selected(RUN_ID, "speech-0001") == original.take_id


@pytest.mark.parametrize("change", ["text", "delivery"])
def test_alternative_generation_retains_current_audio_after_an_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    stale = takes.capture_active(RUN_ID, "speech-0001", source="original")
    request = (
        RevisionRequest(script=SCRIPT.replace("Welcome.", "Settle into your chair."))
        if change == "text"
        else RevisionRequest(timeline=TimelineEdit(delivery={"speech-0001": "warm"}))
    )
    RevisionStore(store).create(RUN_ID, request)
    segment = next(
        segment
        for segment in store.load_timeline(RUN_ID).segments
        if segment.id == "speech-0001" and segment.type == "SPEECH"
    )
    checkpoints = SegmentCheckpointStore(store)
    current_audio = PcmAudio(b"\x40\x00" * 480, 24_000)
    current_checkpoint = checkpoints.completed_checkpoint(
        run_id=RUN_ID,
        segment_id=segment.id,
        cache_key=cache_key_for(segment, ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())),
        audio=current_audio,
        attempt_count=2,
        cache_hit=False,
        started_at=NOW,
        completed_at=NOW,
    )
    checkpoints.save(current_checkpoint, audio=current_audio)
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    alternative_audio = PcmAudio(b"\x50\x00" * 480, 24_000)

    def regenerate(
        run_id: str, segment_id: str, *, _finalizing_take: bool = False
    ) -> dict[str, str]:
        selected = takes.selected(run_id, segment_id)
        assert selected is not None and selected != stale.take_id
        selected_manifest = next(
            take for take in takes.list(run_id, segment_id) if take.take_id == selected
        )
        assert selected_manifest.cache_key == current_checkpoint.cache_key
        checkpoints.save(
            checkpoints.completed_checkpoint(
                run_id=RUN_ID,
                segment_id=segment.id,
                cache_key=current_checkpoint.cache_key,
                audio=alternative_audio,
                attempt_count=3,
                cache_hit=False,
                started_at=NOW,
                completed_at=NOW,
            ),
            audio=alternative_audio,
        )
        application._tasks[run_id] = GenerationTask(task_id=run_id, run_id=run_id)
        return {"run_id": run_id}

    monkeypatch.setattr(application, "regenerate_segment", regenerate)
    monkeypatch.setattr("whoopy.webui.server.threading.Thread.start", lambda _thread: None)
    result = application.generate_alternative_take(str(RUN_ID), segment.id)
    assert result is not None
    selected = result["selected_take_id"]
    takes.capture_active(
        RUN_ID, segment.id, source="production_regeneration", select_if_unset=False
    )
    takes.choose(RUN_ID, segment.id, selected, action="restore")
    assert checkpoints.audio_path(RUN_ID, segment.id).read_bytes() == current_audio.pcm_s16le
    assert takes.selected(RUN_ID, segment.id) == selected
    assert len(takes.list(RUN_ID, segment.id)) == 3


@pytest.mark.parametrize("replacement", ["cache_key", "pcm", "corrupt_pcm"])
def test_selected_take_tracks_verified_active_checkpoint(tmp_path: Path, replacement: str) -> None:
    store, original_audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    original = takes.capture_active(RUN_ID, "speech-0001", source="original")
    assert takes.selected(RUN_ID, "speech-0001") == original.take_id
    checkpoints = SegmentCheckpointStore(store)
    replacement_audio = (
        PcmAudio(b"\x30\x00" * 480, 24_000) if replacement == "pcm" else original_audio
    )
    checkpoint = checkpoints.completed_checkpoint(
        run_id=RUN_ID,
        segment_id="speech-0001",
        cache_key="0" * 64 if replacement == "cache_key" else original.cache_key,
        audio=replacement_audio,
        attempt_count=2,
        cache_hit=False,
        started_at=NOW,
        completed_at=NOW,
    )
    checkpoints.save(checkpoint, audio=replacement_audio)
    if replacement == "corrupt_pcm":
        checkpoints.audio_path(RUN_ID, "speech-0001").write_bytes(b"corrupt")

    assert takes.selected(RUN_ID, "speech-0001") is None
    assert takes.list(RUN_ID, "speech-0001") == [original]


def _alternative_take(store: RunStore) -> tuple[str, str, PcmAudio]:
    takes = SegmentTakeStore(store)
    original = takes.capture_active(RUN_ID, "speech-0001", source="original")
    checkpoints = SegmentCheckpointStore(store)
    audio = PcmAudio(b"\x60\x00" * 480, 24_000)
    checkpoint = checkpoints.completed_checkpoint(
        run_id=RUN_ID,
        segment_id="speech-0001",
        cache_key=original.cache_key,
        audio=audio,
        attempt_count=2,
        cache_hit=False,
        started_at=NOW,
        completed_at=NOW,
    )
    checkpoints.save(checkpoint, audio=audio)
    alternative = takes.capture_active(
        RUN_ID, "speech-0001", source="production_regeneration", select_if_unset=False
    )
    return original.take_id, alternative.take_id, audio


@pytest.mark.parametrize("abandoned", ["cancelled", "superseded"])
def test_abandoned_alternative_finalizer_preserves_later_selection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, abandoned: str
) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    original, alternative, alternative_audio = _alternative_take(store)
    takes = SegmentTakeStore(store)
    takes.choose(RUN_ID, "speech-0001", alternative)
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    run_id = str(RUN_ID)
    generation_task = GenerationTask(
        task_id=run_id,
        run_id=run_id,
        launching=False,
        cancel_requested=abandoned == "cancelled",
    )
    application._tasks[run_id] = (
        generation_task
        if abandoned == "cancelled"
        else GenerationTask(task_id=run_id, run_id=run_id, launching=False)
    )
    resumed: list[str] = []
    monkeypatch.setattr(application, "resume_run", lambda value: resumed.append(value))

    application._finalize_alternative_take(generation_task, "speech-0001", original)

    assert takes.selected(RUN_ID, "speech-0001") == alternative
    assert len(takes.list(RUN_ID, "speech-0001")) == 2
    assert SegmentCheckpointStore(store).audio_path(RUN_ID, "speech-0001").read_bytes() == (
        alternative_audio.pcm_s16le
    )
    assert resumed == []


def test_alternative_finalizer_rechecks_ownership_before_restoring_take(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    original, alternative, alternative_audio = _alternative_take(store)
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    run_id = str(RUN_ID)
    generation_task = GenerationTask(task_id=run_id, run_id=run_id, launching=False)
    application._tasks[run_id] = generation_task
    completion_observed = Event()
    selection_finished = Event()
    read_record = application._record
    resumed: list[str] = []

    def observed_completion(value: str) -> RunRecord | None:
        record = read_record(value)
        if not completion_observed.is_set():
            completion_observed.set()
            assert selection_finished.wait(5)
        return record

    def resume(value: str) -> dict[str, str]:
        resumed.append(value)
        application._tasks[value] = GenerationTask(task_id=value, run_id=value)
        return {"run_id": value}

    monkeypatch.setattr(application, "_record", observed_completion)
    monkeypatch.setattr(application, "resume_run", resume)
    monkeypatch.setattr(application, "_resume_run_admitted", resume)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            application._finalize_alternative_take, generation_task, "speech-0001", original
        )
        try:
            assert completion_observed.wait(5)
            chosen = application.choose_segment_take(run_id, "speech-0001", alternative)
            assert chosen is not None
        finally:
            selection_finished.set()
        future.result(timeout=5)

    assert SegmentTakeStore(store).selected(RUN_ID, "speech-0001") == alternative
    assert SegmentCheckpointStore(store).audio_path(RUN_ID, "speech-0001").read_bytes() == (
        alternative_audio.pcm_s16le
    )
    assert resumed == [run_id]


def test_successful_alternative_finalizer_retains_original_and_captures_generated_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, original_audio = _completed_run(tmp_path / "runs")
    original, _alternative, alternative_audio = _alternative_take(store)
    _pending_alternative(store, original)
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    run_id = str(RUN_ID)
    generation_task = GenerationTask(task_id=run_id, run_id=run_id, launching=False)
    application._tasks[run_id] = generation_task
    resumed: list[str] = []
    monkeypatch.setattr(application, "_resume_run_admitted", lambda value: resumed.append(value))

    application._finalize_alternative_take(generation_task, "speech-0001", original)

    takes = SegmentTakeStore(store)
    assert takes.selected(RUN_ID, "speech-0001") == original
    generated = takes.list(RUN_ID, "speech-0001")[-1]
    assert generated.source == "production_regeneration"
    assert generated.byte_count == len(alternative_audio.pcm_s16le)
    assert SegmentCheckpointStore(store).audio_path(RUN_ID, "speech-0001").read_bytes() == (
        original_audio.pcm_s16le
    )
    assert resumed == [run_id]


def test_later_selection_waits_for_alternative_finalizer_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, _audio = _completed_run(tmp_path / "runs")
    original, alternative, _alternative_audio = _alternative_take(store)
    _pending_alternative(store, original)
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )
    run_id = str(RUN_ID)
    generation_task = GenerationTask(task_id=run_id, run_id=run_id, launching=False)
    application._tasks[run_id] = generation_task
    commit_entered = Event()
    finish_commit = Event()
    selection_started = Event()
    selection_finished = Event()
    capture_active = SegmentTakeStore.capture_active

    def capture(
        takes: SegmentTakeStore,
        run_id: UUID | str,
        segment_id: str,
        *,
        source: Literal["original", "production_regeneration"],
        regeneration_attempt_id: UUID | None = None,
        select_if_unset: bool = True,
    ) -> SegmentTakeManifest:
        commit_entered.set()
        assert finish_commit.wait(5)
        return capture_active(
            takes,
            run_id,
            segment_id,
            source=source,
            regeneration_attempt_id=regeneration_attempt_id,
            select_if_unset=select_if_unset,
        )

    def choose() -> None:
        selection_started.set()
        application.choose_segment_take(run_id, "speech-0001", alternative)
        selection_finished.set()

    monkeypatch.setattr(SegmentTakeStore, "capture_active", capture)
    monkeypatch.setattr(application, "resume_run", lambda _run_id: None)

    def assemble(run_id: str) -> None:
        from whoopy.pipeline.worker import LocalWorker

        LocalWorker(
            store, synthesizer=ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
        ).resume(run_id)

    monkeypatch.setattr(application, "_resume_run_admitted", assemble)
    with ThreadPoolExecutor(max_workers=2) as executor:
        finalizer = executor.submit(
            application._finalize_alternative_take, generation_task, "speech-0001", original
        )
        try:
            assert commit_entered.wait(5)
            selection = executor.submit(choose)
            assert selection_started.wait(5)
            assert not selection_finished.wait(0.05)
        finally:
            finish_commit.set()
        finalizer.result(timeout=5)
        selection.result(timeout=5)

    assert SegmentTakeStore(store).selected(RUN_ID, "speech-0001") == alternative
