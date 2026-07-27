from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.artifacts import TargetPlatform
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.processing import SpeechProcessingSettings
from whoopy.control import LocalControlPlane
from whoopy.pipeline.generation import RunModelMetadata, ScriptRunConfig, TTSRunSettings
from whoopy.pipeline.runs import (
    InvalidRunIdError,
    RunRecord,
    RunStatus,
    RunStore,
    RunStoreError,
)

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
CREATED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_control_plane_saves_a_queued_run_without_processing_it(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    control = LocalControlPlane(store)

    record = control.submit_prompt("  A short grounding meditation.  ")

    assert record.status is RunStatus.QUEUED
    assert record.prompt == "A short grounding meditation."
    assert store.load(record.run_id) == record
    assert store.record_path(record.run_id).is_file()
    assert not store.timeline_path(record.run_id).exists()

    document = json.loads(store.record_path(record.run_id).read_text(encoding="utf-8"))
    assert document["schema_version"] == 3
    assert document["run_id"] == str(record.run_id)
    assert document["status"] == "queued"
    assert document["recovery"]["process_attempts"] == 0
    assert control.get_run(str(record.run_id)) == record


def test_phase_one_completed_record_remains_readable() -> None:
    record = RunRecord.model_validate(
        {
            "schema_version": 1,
            "run_id": RUN_ID,
            "status": "completed",
            "prompt": "An old run.",
            "created_at": CREATED_AT,
            "updated_at": CREATED_AT,
            "timeline_artifact": "timeline.json",
            "error": None,
        }
    )

    assert record.schema_version == 1
    assert record.audio_artifact is None
    assert record.audio_manifest_artifact is None
    assert record.quality_artifact is None


def test_phase_two_completed_record_remains_readable() -> None:
    record = RunRecord.model_validate(
        {
            "schema_version": 2,
            "run_id": RUN_ID,
            "status": "completed",
            "prompt": "A Phase 2 run.",
            "created_at": CREATED_AT,
            "updated_at": CREATED_AT,
            "timeline_artifact": "timeline.json",
            "audio_artifact": "narration.wav",
            "audio_manifest_artifact": "audio-manifest.json",
            "quality_artifact": "quality.json",
            "error": None,
        }
    )

    assert record.schema_version == 2
    assert record.recovery is None


def test_empty_prompt_is_rejected_before_a_directory_is_created(tmp_path: Path) -> None:
    store = RunStore(tmp_path)

    with pytest.raises(RunStoreError, match="Invalid run request"):
        store.create("   ", run_id=RUN_ID, created_at=CREATED_AT)

    assert not store.run_directory(RUN_ID).exists()


def test_run_id_must_be_a_uuid_before_it_becomes_a_path(tmp_path: Path) -> None:
    store = RunStore(tmp_path)

    with pytest.raises(InvalidRunIdError, match="Invalid run ID"):
        store.load("../../outside")


def test_script_run_atomically_saves_every_immutable_input(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    resolved = ScriptRunConfig(
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
    metadata = RunModelMetadata(tts=FixtureSpeechSynthesizer.metadata)

    record = store.create_script_run(
        script="Welcome.\n\n[pause: 2s]\n\nBreathe.",
        source_name="test.md",
        resolved_config=resolved,
        model_metadata=metadata,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert record.schema_version == 4
    assert record.source_kind == "script_file"
    assert store.load_script(RUN_ID).startswith("Welcome.")
    assert store.load_resolved_config(RUN_ID) == resolved
    assert store.load_model_metadata(RUN_ID) == metadata
    assert store.load(RUN_ID) == record
