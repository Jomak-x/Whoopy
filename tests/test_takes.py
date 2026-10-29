from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest
from fastapi.testclient import TestClient

from whoopy.artifacts import TargetPlatform
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import ProcessedSpeechSynthesizer, SpeechProcessingSettings
from whoopy.audio.synthesis import cache_key_for
from whoopy.pipeline.checkpoints import SegmentCheckpointStore
from whoopy.pipeline.generation import RunModelMetadata, ScriptRunConfig, TTSRunSettings
from whoopy.pipeline.revisions import RevisionRequest, RevisionStore, TimelineEdit
from whoopy.pipeline.runs import (
    AUDIO_FILENAME,
    AUDIO_MANIFEST_FILENAME,
    QUALITY_FILENAME,
    TIMELINE_FILENAME,
    RunExecution,
    RunRecovery,
    RunStage,
    RunStatus,
    RunStore,
)
from whoopy.pipeline.takes import SegmentTakeError, SegmentTakeStore
from whoopy.timeline import build_script_timeline
from whoopy.webui.api import create_api
from whoopy.webui.server import LocalWebApplication

RUN_ID = UUID("40404040-4040-4040-8040-404040404040")
NOW = datetime(2026, 8, 5, tzinfo=UTC)
SCRIPT = "Welcome.\n\n[pause: 3s]\n\nNotice the support beneath you.\n"


def _completed_run(root: Path) -> tuple[RunStore, PcmAudio]:
    store = RunStore(root)
    config = ScriptRunConfig(
        profile="basic",
        target=TargetPlatform(operating_system="linux", architecture="x86_64"),
        tts=TTSRunSettings(
            voice_name="af_heart",
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
