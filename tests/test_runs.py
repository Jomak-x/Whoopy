from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.artifacts import TargetPlatform
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.processing import SpeechProcessingSettings
from whoopy.control import LocalControlPlane
from whoopy.meditation import DraftedSection, MeditationPlan, PlannedSection
from whoopy.meditation.generator import MeditationGenerationResult
from whoopy.pipeline.generation import (
    GenerationRunSettings,
    PendingGenerationConfig,
    RunModelMetadata,
    ScriptRunConfig,
    TTSRunSettings,
)
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.runs import (
    InvalidRunIdError,
    RunExecution,
    RunRecord,
    RunStage,
    RunStatus,
    RunStore,
    RunStoreError,
)
from whoopy.ports import AdapterMetadata
from whoopy.timeline import SpeechSegment, Timeline

RUN_ID = UUID("11111111-1111-4111-8111-111111111111")
CREATED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _metadata() -> RunModelMetadata:
    return RunModelMetadata(
        tts=FixtureSpeechSynthesizer.metadata,
        llm=AdapterMetadata(
            adapter_id="test.llm",
            versioned_model_id="test-plan@1",
            runtime_id="fixture",
            runtime_version="1",
            license_id="CC0-1.0",
            device="cpu",
        ),
    )


def _pending_generation(**changes: object) -> PendingGenerationConfig:
    values: dict[str, object] = {
        "profile": "standard",
        "target": TargetPlatform(operating_system="linux", architecture="x86_64"),
        "tts": TTSRunSettings(
            voice_name="af_heart",
            speaker_id=3,
            speed=0.9,
            num_threads=2,
            provider="cpu",
            language="en-us",
        ),
        "processing": SpeechProcessingSettings(),
        "duration_seconds": 300,
        "seed": 42,
        "plan_prompt_id": "whoopy.plan",
        "plan_prompt_version": 4,
        "section_prompt_id": "whoopy.section",
        "section_prompt_version": 3,
        "max_parallel_sections": 2,
        "model_metadata": _metadata(),
    }
    values.update(changes)
    return PendingGenerationConfig.model_validate(values)


def _resolved_generation(
    request: PendingGenerationConfig,
    *,
    seed: int | None = None,
) -> ScriptRunConfig:
    return ScriptRunConfig(
        mode="generated_prompt",
        profile=request.profile,
        target=request.target,
        tts=request.tts,
        processing=request.processing,
        generation=GenerationRunSettings(
            duration_seconds=request.duration_seconds,
            seed=request.seed if seed is None else seed,
            plan_prompt_id=request.plan_prompt_id,
            plan_prompt_version=request.plan_prompt_version,
            section_prompt_id=request.section_prompt_id,
            section_prompt_version=request.section_prompt_version,
            max_parallel_sections=request.max_parallel_sections,
            estimated_duration_seconds=298.0,
        ),
    )


def _generated_result() -> MeditationGenerationResult:
    sections = [
        PlannedSection(
            id="arrive",
            title="Arrive",
            purpose="Guide the arrival step.",
            technique="arrival",
            target_speech_seconds=20,
            pause_after_ms=6_000,
            minimum_words=8,
            maximum_words=30,
        ),
        PlannedSection(
            id="notice",
            title="Notice",
            purpose="Guide the noticing step.",
            technique="focused_attention",
            target_speech_seconds=20,
            pause_after_ms=6_000,
            minimum_words=8,
            maximum_words=30,
        ),
        PlannedSection(
            id="return",
            title="Return",
            purpose="Guide the return step.",
            technique="return",
            target_speech_seconds=20,
            pause_after_ms=6_000,
            minimum_words=8,
            maximum_words=30,
        ),
    ]
    drafted = [
        DraftedSection(
            section_id=section.id,
            text="Notice the steady support beneath you for this moment.",
            word_count=9,
        )
        for section in sections
    ]
    return MeditationGenerationResult(
        plan=MeditationPlan(
            title="A Steady Moment",
            intention="Practice steady attention.",
            requested_duration_seconds=300,
            sections=sections,
        ),
        sections=drafted,
        script="# A Steady Moment\n\nNotice the steady support beneath you.",
        timeline=Timeline(
            schema_version=4,
            run_id=RUN_ID,
            created_at=CREATED_AT,
            source="generated_prompt",
            segments=[SpeechSegment(id="speech-0001", text="Notice the support.")],
        ),
        estimated_duration_seconds=298.0,
        raw_attempts=[],
    )


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
    assert document["execution"]["stage"] == "queued"
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


def test_expired_execution_reconciles_to_resumable_interrupted_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    queued = store.create("Breathe.", run_id=RUN_ID, created_at=CREATED_AT)
    assert queued.recovery is not None
    running = queued.transition(
        RunStatus.RUNNING,
        updated_at=CREATED_AT,
        recovery=queued.recovery.model_copy(update={"process_attempts": 1}),
        execution=RunExecution(
            stage=RunStage.SYNTHESIZING,
            attempt_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            owner_id="test-worker",
            pid=123,
            started_at=CREATED_AT,
            heartbeat_at=CREATED_AT,
            lease_expires_at=CREATED_AT + timedelta(seconds=15),
            current_segment_id="speech-0002",
        ),
    )
    store.save(running)

    assert (
        store.reconcile_stale_run(RUN_ID, now=CREATED_AT + timedelta(seconds=14)).status
        is RunStatus.RUNNING
    )
    interrupted = store.reconcile_stale_run(
        RUN_ID,
        now=CREATED_AT + timedelta(seconds=15),
    )

    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.execution is not None
    assert interrupted.execution.interruption_kind == "lease_expired"
    assert interrupted.execution.current_segment_id == "speech-0002"
    assert not interrupted.execution.is_active


def test_reconciliation_does_not_steal_an_expired_but_os_locked_run(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    queued = store.create("Breathe.", run_id=RUN_ID, created_at=CREATED_AT)
    assert queued.recovery is not None
    running = queued.transition(
        RunStatus.RUNNING,
        updated_at=CREATED_AT,
        recovery=queued.recovery.model_copy(update={"process_attempts": 1}),
        execution=RunExecution(
            stage=RunStage.SYNTHESIZING,
            attempt_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            owner_id="live-worker",
            pid=123,
            started_at=CREATED_AT,
            heartbeat_at=CREATED_AT,
            lease_expires_at=CREATED_AT + timedelta(seconds=15),
        ),
    )
    store.save(running)

    with RunLock(store.run_directory(RUN_ID)):
        unchanged = store.reconcile_stale_run(
            RUN_ID,
            now=CREATED_AT + timedelta(minutes=1),
        )

    assert unchanged.status is RunStatus.RUNNING
    assert store.load(RUN_ID).status is RunStatus.RUNNING


def test_heartbeat_refuses_to_overwrite_a_different_attempt(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    claimed = store.start_generation(
        store.create_pending_generation_run(
            prompt="A calm practice.",
            generation_request=_pending_generation(),
            run_id=RUN_ID,
            created_at=CREATED_AT,
        ).run_id,
        owner_id="generation-worker",
        pid=123,
        started_at=CREATED_AT,
    )
    assert claimed.execution is not None

    with pytest.raises(RunStoreError, match="different processing attempt"):
        store.heartbeat(
            RUN_ID,
            attempt_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            now=CREATED_AT + timedelta(seconds=2),
        )


def test_stale_execution_update_cannot_restore_an_old_attempt(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    first = store.start_generation(
        store.create_pending_generation_run(
            prompt="A calm practice.",
            generation_request=_pending_generation(),
            run_id=RUN_ID,
            created_at=CREATED_AT,
        ).run_id,
        owner_id="first-worker",
        pid=123,
        started_at=CREATED_AT,
    )
    assert first.execution is not None
    replacement = first.execution.model_copy(
        update={
            "attempt_id": UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc"),
            "owner_id": "replacement-worker",
        }
    )
    store.save(
        first.transition(
            RunStatus.RUNNING,
            updated_at=CREATED_AT + timedelta(seconds=1),
            execution=replacement,
        )
    )

    with pytest.raises(RunStoreError, match="different processing attempt"):
        store.update_execution(
            RUN_ID,
            first.execution.heartbeat(
                now=CREATED_AT + timedelta(seconds=2),
                lease_seconds=15,
            ),
            updated_at=CREATED_AT + timedelta(seconds=2),
        )

    assert store.load(RUN_ID).execution == replacement


def test_queued_script_placeholder_promotes_without_changing_its_uuid_or_birth_time(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path)
    placeholder = store.create("temporary web request", run_id=RUN_ID, created_at=CREATED_AT)
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

    promoted = store.promote_queued_script_run(
        RUN_ID,
        script="Welcome.\n\n[pause: 2s]",
        source_name="web.md",
        resolved_config=resolved,
        model_metadata=metadata,
        promoted_at=CREATED_AT + timedelta(seconds=5),
    )

    assert promoted.run_id == placeholder.run_id
    assert promoted.created_at == placeholder.created_at
    assert promoted.schema_version == 6
    assert store.load_script(RUN_ID).startswith("Welcome")
    with pytest.raises(RunStoreError, match="already contains immutable inputs"):
        store.promote_queued_script_run(
            RUN_ID,
            script="replacement",
            source_name="replacement.md",
            resolved_config=resolved,
            model_metadata=metadata,
        )


def test_script_promotion_retries_an_identical_partial_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RunStore(tmp_path)
    store.create("temporary web request", run_id=RUN_ID, created_at=CREATED_AT)
    resolved = ScriptRunConfig(
        profile="basic",
        target=TargetPlatform(operating_system="linux", architecture="x86_64"),
        tts=_pending_generation().tts,
        processing=SpeechProcessingSettings(),
    )
    metadata = RunModelMetadata(tts=FixtureSpeechSynthesizer.metadata)
    original_write = RunStore._write_bytes
    calls = 0

    def fail_second_write(path: Path, payload: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RunStoreError("simulated crash during promotion")
        original_write(path, payload)

    monkeypatch.setattr(RunStore, "_write_bytes", staticmethod(fail_second_write))
    with pytest.raises(RunStoreError, match="simulated crash"):
        store.promote_queued_script_run(
            RUN_ID,
            script="Welcome.\n\n[pause: 2s]",
            source_name="web.md",
            resolved_config=resolved,
            model_metadata=metadata,
        )
    monkeypatch.setattr(RunStore, "_write_bytes", staticmethod(original_write))

    promoted = store.promote_queued_script_run(
        RUN_ID,
        script="Welcome.\n\n[pause: 2s]",
        source_name="web.md",
        resolved_config=resolved,
        model_metadata=metadata,
    )

    assert promoted.schema_version == 6
    assert store.load_script(RUN_ID).startswith("Welcome")


def test_script_promotion_preserves_conflicting_partial_data(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("temporary web request", run_id=RUN_ID, created_at=CREATED_AT)
    store.script_path(RUN_ID).write_text("user-owned different script", encoding="utf-8")
    resolved = ScriptRunConfig(
        profile="basic",
        target=TargetPlatform(operating_system="linux", architecture="x86_64"),
        tts=_pending_generation().tts,
        processing=SpeechProcessingSettings(),
    )

    with pytest.raises(RunStoreError, match="Refusing to replace different"):
        store.promote_queued_script_run(
            RUN_ID,
            script="new script",
            source_name="web.md",
            resolved_config=resolved,
            model_metadata=RunModelMetadata(tts=FixtureSpeechSynthesizer.metadata),
        )

    assert store.load_script(RUN_ID) == "user-owned different script"


def test_schema_v5_completed_run_requires_every_audio_artifact() -> None:
    with pytest.raises(ValueError, match="every audio artifact"):
        RunRecord.model_validate(
            {
                "schema_version": 5,
                "run_id": RUN_ID,
                "status": "completed",
                "prompt": "Legacy generated run.",
                "source_kind": "generated_prompt",
                "created_at": CREATED_AT,
                "updated_at": CREATED_AT,
                "script_artifact": "script.md",
                "resolved_config_artifact": "resolved-config.json",
                "model_metadata_artifact": "model-metadata.json",
                "plan_artifact": "plan.json",
                "raw_model_output_artifact": "raw-model-output",
                "draft_sections_artifact": "draft-sections",
                "timeline_artifact": "timeline.json",
                "recovery": {
                    "process_attempts": 1,
                    "resume_count": 0,
                    "cache_hits": 0,
                    "cache_misses": 0,
                    "checkpoint_reuses": 0,
                    "speech_segments_total": 1,
                    "speech_segments_completed": 1,
                },
            }
        )


def test_atomic_write_flushes_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flushes: list[int] = []
    monkeypatch.setattr("whoopy.pipeline.runs.os.fsync", flushes.append)

    destination = tmp_path / "durable.json"
    RunStore._write_bytes(destination, b"{}\n")

    assert destination.read_bytes() == b"{}\n"
    expected_flushes = 1 if os.name == "nt" else 2
    assert len(flushes) == expected_flushes


def test_run_load_retries_a_transient_permission_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = RunStore(tmp_path)
    record = store.create("A durable read.", run_id=RUN_ID)
    original_read_text = Path.read_text
    attempts = 0

    def transient_read(
        path: Path,
        encoding: str | None = None,
        errors: str | None = None,
    ) -> str:
        nonlocal attempts
        if path == store.record_path(RUN_ID) and attempts == 0:
            attempts += 1
            raise PermissionError("temporary Windows replace boundary")
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", transient_read)

    assert store.load(RUN_ID) == record
    assert attempts == 1


def test_pending_generation_run_atomically_saves_every_resume_input(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    request = _pending_generation()

    record = store.create_pending_generation_run(
        prompt="  A five-minute grounding practice.  ",
        generation_request=request,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert record.schema_version == 6
    assert record.source_kind == "generated_prompt"
    assert record.prompt == "A five-minute grounding practice."
    assert record.generation_request_artifact == "generation-request.json"
    assert record.script_artifact is None
    assert record.execution == RunExecution(stage=RunStage.QUEUED)
    assert store.load_generation_request(RUN_ID) == request
    assert store.load(RUN_ID) == record


def test_generation_request_upgrades_a_legacy_placeholder_idempotently(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    request = _pending_generation()
    legacy = store.create("A calm practice.", run_id=RUN_ID, created_at=CREATED_AT)

    first_path = store.write_generation_request(
        RUN_ID,
        request,
        written_at=CREATED_AT + timedelta(seconds=1),
    )
    second_path = store.write_generation_request(RUN_ID, request)

    upgraded = store.load(RUN_ID)
    assert first_path == second_path
    assert upgraded.schema_version == 6
    assert upgraded.created_at == legacy.created_at
    assert upgraded.generation_request_artifact == "generation-request.json"


def test_generation_request_cannot_be_overwritten_with_different_values(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path)
    original = _pending_generation()
    store.create_pending_generation_run(
        prompt="A calm practice.",
        generation_request=original,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    with pytest.raises(RunStoreError, match="different generation request"):
        store.write_generation_request(
            RUN_ID,
            _pending_generation(seed=99),
        )

    assert store.load_generation_request(RUN_ID) == original


def test_schema_v6_rejects_missing_or_partial_generation_provenance() -> None:
    base = {
        "schema_version": 6,
        "run_id": RUN_ID,
        "status": "queued",
        "prompt": "A calm practice.",
        "source_kind": "generated_prompt",
        "created_at": CREATED_AT,
        "updated_at": CREATED_AT,
        "recovery": {
            "process_attempts": 0,
            "resume_count": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "checkpoint_reuses": 0,
            "speech_segments_total": 0,
            "speech_segments_completed": 0,
        },
        "execution": {"stage": "queued"},
    }

    with pytest.raises(ValueError, match="requires its generation request"):
        RunRecord.model_validate(base)
    with pytest.raises(ValueError, match="requires every generated input artifact"):
        RunRecord.model_validate(
            {
                **base,
                "generation_request_artifact": "generation-request.json",
                "script_artifact": "script.md",
            }
        )


def test_schema_v1_through_v5_documents_still_validate_without_v6_field() -> None:
    legacy_documents = [
        {
            "schema_version": 1,
            "source_kind": "fixture_prompt",
            "status": "completed",
            "timeline_artifact": "timeline.json",
        },
        {
            "schema_version": 2,
            "source_kind": "fixture_prompt",
            "status": "completed",
            "timeline_artifact": "timeline.json",
            "audio_artifact": "narration.wav",
            "audio_manifest_artifact": "audio-manifest.json",
            "quality_artifact": "quality.json",
        },
        {
            "schema_version": 3,
            "source_kind": "fixture_prompt",
            "status": "queued",
            "recovery": {
                "process_attempts": 0,
                "resume_count": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "checkpoint_reuses": 0,
                "speech_segments_total": 0,
                "speech_segments_completed": 0,
            },
        },
        {
            "schema_version": 4,
            "source_kind": "script_file",
            "status": "queued",
            "script_artifact": "script.md",
            "resolved_config_artifact": "resolved-config.json",
            "model_metadata_artifact": "model-metadata.json",
            "recovery": {
                "process_attempts": 0,
                "resume_count": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "checkpoint_reuses": 0,
                "speech_segments_total": 0,
                "speech_segments_completed": 0,
            },
        },
        {
            "schema_version": 5,
            "source_kind": "generated_prompt",
            "status": "queued",
            "script_artifact": "script.md",
            "resolved_config_artifact": "resolved-config.json",
            "model_metadata_artifact": "model-metadata.json",
            "plan_artifact": "plan.json",
            "raw_model_output_artifact": "raw-model-output",
            "draft_sections_artifact": "draft-sections",
            "recovery": {
                "process_attempts": 0,
                "resume_count": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "checkpoint_reuses": 0,
                "speech_segments_total": 0,
                "speech_segments_completed": 0,
            },
        },
    ]

    for document in legacy_documents:
        record = RunRecord.model_validate(
            {
                "run_id": RUN_ID,
                "prompt": "Legacy run.",
                "created_at": CREATED_AT,
                "updated_at": CREATED_AT,
                **document,
            }
        )
        assert record.generation_request_artifact is None


def test_generated_promotion_keeps_pending_request_as_provenance(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    request = _pending_generation()
    store.create_pending_generation_run(
        prompt="A five-minute grounding practice.",
        generation_request=request,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    promoted = store.promote_queued_generated_run(
        RUN_ID,
        generated=_generated_result(),
        resolved_config=_resolved_generation(request),
        model_metadata=request.model_metadata,
        promoted_at=CREATED_AT + timedelta(seconds=10),
    )

    assert promoted.schema_version == 6
    assert promoted.generation_request_artifact == "generation-request.json"
    assert promoted.script_artifact == "script.md"
    assert store.load_generation_request(RUN_ID) == request


def test_generated_promotion_rejects_settings_changed_after_planning(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    request = _pending_generation()
    store.create_pending_generation_run(
        prompt="A five-minute grounding practice.",
        generation_request=request,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    with pytest.raises(RunStoreError, match="differ from the immutable"):
        store.promote_queued_generated_run(
            RUN_ID,
            generated=_generated_result(),
            resolved_config=_resolved_generation(request, seed=99),
            model_metadata=request.model_metadata,
        )

    assert store.load(RUN_ID).script_artifact is None
    assert store.load_generation_request(RUN_ID) == request


def test_active_generated_promotion_keeps_request_and_generation_lease(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    request = _pending_generation()
    store.create_pending_generation_run(
        prompt="A five-minute grounding practice.",
        generation_request=request,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )
    active = store.start_generation(
        RUN_ID,
        owner_id="generation-worker",
        pid=123,
        started_at=CREATED_AT + timedelta(seconds=1),
    )
    assert active.execution is not None
    assert active.execution.attempt_id is not None

    promoted = store.promote_active_generated_run(
        RUN_ID,
        attempt_id=active.execution.attempt_id,
        generated=_generated_result(),
        resolved_config=_resolved_generation(request),
        model_metadata=request.model_metadata,
        promoted_at=CREATED_AT + timedelta(seconds=2),
    )

    assert promoted.schema_version == 6
    assert promoted.status is RunStatus.RUNNING
    assert promoted.execution is not None
    assert promoted.execution.stage is RunStage.COMPILING
    assert promoted.execution.is_active
    assert store.load_generation_request(RUN_ID) == request


def test_interrupted_pending_generation_restarts_from_immutable_request(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    request = _pending_generation()
    store.create_pending_generation_run(
        prompt="A five-minute grounding practice.",
        generation_request=request,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )
    active = store.start_generation(
        RUN_ID,
        owner_id="generation-worker",
        pid=123,
        started_at=CREATED_AT + timedelta(seconds=1),
    )
    assert active.execution is not None
    assert active.execution.attempt_id is not None
    store.interrupt_active_run(
        RUN_ID,
        attempt_id=active.execution.attempt_id,
        kind="signal",
        message="Stopped safely.",
        interrupted_at=CREATED_AT + timedelta(seconds=2),
    )

    restarted = store.restart_generation(
        RUN_ID,
        owner_id="replacement-worker",
        pid=456,
        started_at=CREATED_AT + timedelta(seconds=3),
    )

    assert restarted.status is RunStatus.RUNNING
    assert restarted.recovery is not None
    assert restarted.recovery.resume_count == 1
    assert restarted.execution is not None
    assert restarted.execution.stage is RunStage.PLANNING
    assert store.load_generation_request(RUN_ID) == request


def test_active_failure_keeps_long_diagnostic_without_breaking_execution_summary(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path)
    request = _pending_generation()
    store.create_pending_generation_run(
        prompt="A five-minute grounding practice.",
        generation_request=request,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )
    active = store.start_generation(
        RUN_ID,
        owner_id="generation-worker",
        pid=123,
        started_at=CREATED_AT + timedelta(seconds=1),
    )
    assert active.execution is not None
    assert active.execution.attempt_id is not None
    diagnostic = "large model traceback " * 300

    failed = store.fail_active_run(
        RUN_ID,
        attempt_id=active.execution.attempt_id,
        error=diagnostic,
        failed_at=CREATED_AT + timedelta(seconds=2),
    )

    assert failed.status is RunStatus.FAILED
    assert failed.error == diagnostic
    assert failed.execution is not None
    assert failed.execution.message == diagnostic[:2_000]
