from __future__ import annotations

import json
import threading
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID

import pytest

from whoopy.artifacts import TargetPlatform
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import SpeechProcessingSettings
from whoopy.cli import main
from whoopy.meditation import (
    DraftedSection,
    MeditationGenerationResult,
    MeditationPlan,
    PlannedSection,
    compile_breath_protocol,
)
from whoopy.meditation.models import (
    BreathMode,
    DeliveryIntent,
    MeditationTechnique,
    PracticeBeat,
    RawGenerationAttempt,
)
from whoopy.meditation.pacing import pace_section
from whoopy.pipeline.generation import (
    GenerationRunSettings,
    RunModelMetadata,
    ScriptRunConfig,
    TTSRunSettings,
)
from whoopy.pipeline.revisions import (
    BreathProtocolEdit,
    RevisionError,
    RevisionRequest,
    RevisionStore,
    TimelineEdit,
    validate_semantic_plan_edit,
)
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
from whoopy.pipeline.worker import LocalWorker
from whoopy.timeline import (
    BreathSegment,
    PronunciationOverride,
    SilenceSegment,
    SpeechSegment,
    Timeline,
    build_script_timeline,
)
from whoopy.webui.server import LocalWebApplication

RUN_ID = UUID("99999999-9999-4999-8999-999999999999")
NOW = datetime(2026, 8, 5, tzinfo=UTC)
SCRIPT = "Welcome to this practice.\n\n[pause: 4s]\n\nNotice the support beneath you.\n"


def _completed_script_run(root: Path) -> RunStore:
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
        source_name="revision-test.md",
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
            cache_misses=2,
            checkpoint_reuses=0,
            speech_segments_total=2,
            speech_segments_completed=2,
        ),
        execution=RunExecution(stage=RunStage.COMPLETED),
    )
    store.save(completed)
    return store


def test_silence_edit_creates_immutable_revision_and_requests_assembly_only(
    tmp_path: Path,
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    revisions = RevisionStore(store)

    edited = revisions.create(
        RUN_ID,
        RevisionRequest(
            timeline=TimelineEdit(silence_duration_ms={"silence-0001": 9_000}),
            note="Give the practice more room.",
        ),
    )

    assert edited.change_kind == "assembly_only"
    assert edited.affected_speech_segment_ids == ()
    assert len(revisions.list(RUN_ID)) == 2
    silence = [
        segment
        for segment in store.load_timeline(RUN_ID).segments
        if isinstance(segment, SilenceSegment)
    ]
    assert silence[0].duration_ms == 9_000
    record = store.load(RUN_ID)
    assert record.status is RunStatus.INTERRUPTED
    assert record.execution is not None and record.execution.stage is RunStage.ASSEMBLING


def test_pause_edit_updates_compiled_markers_without_changing_ignored_text(tmp_path: Path) -> None:
    script = (
        "---\n[pause: 90s]\n---\n# A practice\n\n"
        "Welcome to this practice.\n\n"
        "```text\n[pause: 80s]\n```\n\n"
        "  [PAUSE: 2s]  \n\n[pause: 2000ms]\n\n"
        "Notice the support beneath you.\n"
    )
    store = _completed_script_run(tmp_path / "runs")
    store._write_bytes(store.script_path(RUN_ID), script.encode())
    store.write_timeline(
        RUN_ID, build_script_timeline(run_id=RUN_ID, script=script, created_at=NOW)
    )
    RevisionStore(store).create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(silence_duration_ms={"silence-0001": 1_000})),
    )

    editable_script = store.load_script(RUN_ID)
    assert editable_script == script.replace("[PAUSE: 2s]", "[pause: 1000ms]").replace(
        "[pause: 2000ms]\n", ""
    )
    recompiled = build_script_timeline(run_id=RUN_ID, script=editable_script, created_at=NOW)
    assert recompiled.segments == store.load_timeline(RUN_ID).segments


def test_delivery_edit_invalidates_only_the_affected_speech_segment(tmp_path: Path) -> None:
    store = _completed_script_run(tmp_path / "runs")
    revisions = RevisionStore(store)

    edited = revisions.create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(delivery={"speech-0002": "compassionate"})),
    )

    assert edited.change_kind == "selective_speech"
    assert edited.affected_speech_segment_ids == ("speech-0002",)
    speech = [
        segment
        for segment in store.load_timeline(RUN_ID).segments
        if isinstance(segment, SpeechSegment)
    ]
    assert speech[1].delivery == "compassionate"
    record = store.load(RUN_ID)
    assert record.execution is not None and record.execution.stage is RunStage.SYNTHESIZING
    assert record.recovery is not None and record.recovery.speech_segments_completed == 1


def test_pronunciation_edit_is_immutable_typed_and_selectively_invalidated(
    tmp_path: Path,
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    original_text = next(
        segment.text
        for segment in store.load_timeline(RUN_ID).segments
        if isinstance(segment, SpeechSegment)
    )
    override = PronunciationOverride(source="Welcome", pronunciation="Well-come")

    edited = RevisionStore(store).create(
        RUN_ID,
        RevisionRequest(
            timeline=TimelineEdit(pronunciation_overrides={"speech-0001": (override,)})
        ),
    )

    speech = next(
        segment for segment in store.load_timeline(RUN_ID).segments if segment.id == "speech-0001"
    )
    assert isinstance(speech, SpeechSegment)
    assert speech.text == original_text
    assert speech.pronunciation_overrides == (override,)
    assert edited.change_kind == "selective_speech"
    assert edited.affected_speech_segment_ids == ("speech-0001",)
    revision_timeline = Timeline.model_validate_json(
        (
            tmp_path / "runs" / str(RUN_ID) / "revisions" / edited.revision_id / "timeline.json"
        ).read_text()
    )
    persisted = next(
        segment for segment in revision_timeline.segments if segment.id == "speech-0001"
    )
    assert isinstance(persisted, SpeechSegment)
    assert persisted.text == original_text
    assert persisted.pronunciation_overrides == (override,)
    record = store.load(RUN_ID)
    assert record.execution is not None and record.execution.stage is RunStage.SYNTHESIZING
    assert record.recovery is not None and record.recovery.speech_segments_completed == 1


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            (PronunciationOverride(source="Whoopy", pronunciation="Woo-pee"),),
            "does not occur in the clean spoken text",
        ),
        (
            (
                PronunciationOverride(source="Welcome", pronunciation="Well-come"),
                PronunciationOverride(source="Welcome", pronunciation="Wel-kuhm"),
            ),
            "duplicate pronunciation override sources",
        ),
    ],
)
def test_pronunciation_edit_rejects_missing_or_duplicate_sources_without_revision(
    tmp_path: Path,
    overrides: tuple[PronunciationOverride, ...],
    message: str,
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    revisions = RevisionStore(store)

    with pytest.raises(RevisionError, match=message):
        revisions.create(
            RUN_ID,
            RevisionRequest(
                timeline=TimelineEdit(pronunciation_overrides={"speech-0001": overrides})
            ),
        )

    assert revisions.list(RUN_ID) == []


def test_local_application_rejects_pronunciation_for_unsupported_adapter(
    tmp_path: Path,
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )

    with pytest.raises(ValueError, match="kokoro does not support typed pronunciation"):
        application.edit_run(
            str(RUN_ID),
            {
                "timeline": {
                    "pronunciation_overrides": {
                        "speech-0001": [{"source": "Welcome", "pronunciation": "Well-come"}]
                    }
                }
            },
        )

    assert RevisionStore(store).list(RUN_ID) == []


def _plan(*, breath_mode: BreathMode = "natural_awareness") -> MeditationPlan:
    sections = [
        PlannedSection(
            id=section_id,
            title=title,
            purpose=f"Guide the reviewed {title.casefold()} beat safely.",
            technique=cast(MeditationTechnique, technique),
            beat=cast(PracticeBeat, beat),
            delivery=cast(
                DeliveryIntent,
                {
                    "arrival": "warm",
                    "focused_attention": "calm",
                    "return": "encouraging",
                }[technique],
            ),
            target_speech_seconds=20,
            pause_after_ms=6_000,
            minimum_words=8,
            maximum_words=30,
        )
        for section_id, title, technique, beat in (
            ("arrival", "Arrival", "arrival", "arrival"),
            ("practice", "Practice", "focused_attention", "practice"),
            ("ending", "Ending", "return", "ending"),
        )
    ]
    return MeditationPlan(
        title="A Steady Moment",
        intention="Practice steady attention.",
        requested_duration_seconds=300,
        silence_budget_seconds=18,
        breath_mode=breath_mode,
        sections=sections,
    )


def test_semantic_plan_edit_is_rejected_without_regeneration_context(tmp_path: Path) -> None:
    store = _completed_script_run(tmp_path / "runs")
    original = _plan()
    store.plan_path(RUN_ID).write_text(original.model_dump_json(indent=2) + "\n")
    revisions = RevisionStore(store)

    with pytest.raises(RevisionError, match="production plan regenerator"):
        revisions.create(
            RUN_ID,
            RevisionRequest(
                plan=_plan(breath_mode="paced_no_hold"),
                note="Change the breathing method.",
            ),
        )

    assert revisions.list(RUN_ID) == []
    assert MeditationPlan.model_validate_json(store.plan_path(RUN_ID).read_text()) == original
    assert store.load(RUN_ID).status is RunStatus.COMPLETED


def test_noop_plan_and_timeline_edit_does_not_create_cosmetic_revision(
    tmp_path: Path,
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    plan = _plan()
    store.plan_path(RUN_ID).write_text(plan.model_dump_json(indent=2) + "\n")
    revisions = RevisionStore(store)

    with pytest.raises(RevisionError, match="does not change"):
        revisions.create(
            RUN_ID,
            RevisionRequest(
                plan=plan,
                timeline=TimelineEdit(delivery={"speech-0001": "neutral"}),
            ),
        )

    # Rejection happens before the original artifacts are snapshotted, so a
    # failed edit cannot look like an accepted immutable revision.
    assert revisions.list(RUN_ID) == []
    assert store.load(RUN_ID).status is RunStatus.COMPLETED


def test_reviewed_breath_protocol_edit_updates_typed_timing_and_only_its_instructions(
    tmp_path: Path,
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    script = "Welcome.\n\n" + compile_breath_protocol("gentle_even_4_4@1", cycles=3)
    timeline = build_script_timeline(
        run_id=RUN_ID,
        script=script,
        created_at=NOW,
        source="script_file",
    )
    store._write_bytes(store.script_path(RUN_ID), script.encode())
    store.write_timeline(RUN_ID, timeline)

    edited = RevisionStore(store).create(
        RUN_ID,
        RevisionRequest(
            breath_protocol=BreathProtocolEdit(
                protocol_id="gentle_long_exhale_3_4@1",
                cycles=3,
            ),
            note="Use the reviewed gentle longer-exhale option.",
        ),
    )

    breaths = [
        segment
        for segment in store.load_timeline(RUN_ID).segments
        if isinstance(segment, BreathSegment)
    ]
    assert {segment.protocol_id for segment in breaths} == {"gentle_long_exhale_3_4@1"}
    assert [segment.duration_ms for segment in breaths] == [3_000, 4_000] * 3
    assert edited.change_kind == "selective_speech"
    assert len(edited.affected_speech_segment_ids) == 3
    assert "Inhale gently for 3 seconds." in store.load_script(RUN_ID)
    assert "protocol=gentle_long_exhale_3_4@1" in store.load_script(RUN_ID)
    record = store.load(RUN_ID)
    assert record.execution is not None and record.execution.stage is RunStage.SYNTHESIZING


def test_reviewed_breath_edit_refuses_cycle_invention_without_semantic_regeneration(
    tmp_path: Path,
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    script = compile_breath_protocol("gentle_even_4_4@1", cycles=3)
    store._write_bytes(store.script_path(RUN_ID), script.encode())
    store.write_timeline(
        RUN_ID,
        build_script_timeline(
            run_id=RUN_ID,
            script=script,
            created_at=NOW,
            source="script_file",
        ),
    )

    with pytest.raises(RevisionError, match="semantic section regeneration"):
        RevisionStore(store).create(
            RUN_ID,
            RevisionRequest(
                breath_protocol=BreathProtocolEdit(
                    protocol_id="gentle_long_exhale_3_4@1",
                    cycles=4,
                )
            ),
        )


def test_semantic_plan_edit_accepts_reviewed_technique_and_normalizes_delivery() -> None:
    original = _plan()
    practice = original.sections[1].model_copy(
        update={
            "purpose": "Guide gentle noting during the reviewed practice beat.",
            "technique": "noting",
            "delivery": "encouraging",
        }
    )
    requested = original.model_copy(
        update={
            "requested_outcome": "Notice distraction and return without judgment.",
            "sections": [original.sections[0], practice, original.sections[2]],
        }
    )

    normalized = validate_semantic_plan_edit(original, requested)

    assert normalized.sections[1].technique == "noting"
    assert normalized.sections[1].delivery == "calm"
    assert normalized.requested_outcome.startswith("Notice distraction")


def test_semantic_plan_edit_rejects_unsupported_techniques_and_timing_changes() -> None:
    original = _plan()
    unsupported = original.sections[1].model_copy(update={"technique": "visualization"})
    with pytest.raises(RevisionError, match="not reviewed"):
        validate_semantic_plan_edit(
            original,
            original.model_copy(
                update={
                    "sections": [
                        original.sections[0],
                        unsupported,
                        original.sections[2],
                    ]
                }
            ),
        )

    changed_timing = original.sections[1].model_copy(update={"pause_after_ms": 20_000})
    with pytest.raises(RevisionError, match="deterministic field pause_after_ms"):
        validate_semantic_plan_edit(
            original,
            original.model_copy(
                update={
                    "sections": [
                        original.sections[0],
                        changed_timing,
                        original.sections[2],
                    ]
                }
            ),
        )


def _generated(plan: MeditationPlan, *, middle_text: str) -> MeditationGenerationResult:
    texts = [
        "Notice the steady support beneath you as this practice begins.",
        middle_text,
        "Let your attention widen and return gently to the room now.",
    ]
    drafted = [
        DraftedSection(
            section_id=section.id,
            text=text,
            word_count=len(text.removesuffix(".").split()),
        )
        for section, text in zip(plan.sections, texts, strict=True)
    ]
    script = "\n\n".join(
        pace_section(draft.text, section_pause_ms=section.pause_after_ms)
        for section, draft in zip(plan.sections, drafted, strict=True)
    )
    return MeditationGenerationResult(
        plan=plan,
        sections=drafted,
        script=script,
        timeline=build_script_timeline(
            run_id=RUN_ID,
            script=script,
            created_at=NOW,
            source="generated_prompt",
        ),
        estimated_duration_seconds=300,
        raw_attempts=[],
    )


def _completed_generated_run(root: Path, generated: MeditationGenerationResult) -> RunStore:
    store = RunStore(root)
    config = ScriptRunConfig(
        mode="generated_prompt",
        profile="standard",
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
        generation=GenerationRunSettings(
            duration_seconds=300,
            seed=42,
            plan_prompt_id="whoopy.plan",
            plan_prompt_version=1,
            section_prompt_id="whoopy.section",
            section_prompt_version=1,
            max_parallel_sections=1,
            estimated_duration_seconds=300,
        ),
    )
    queued = store.create_generated_run(
        prompt="Practice steady attention.",
        generated=generated,
        resolved_config=config,
        model_metadata=RunModelMetadata(
            tts=FixtureSpeechSynthesizer.metadata,
            llm=FixtureSpeechSynthesizer.metadata,
        ),
        run_id=RUN_ID,
        created_at=NOW,
    )
    store.write_timeline(RUN_ID, generated.timeline)
    store.audio_path(RUN_ID).write_bytes(b"wave")
    store.audio_manifest_path(RUN_ID).write_text("{}")
    store.quality_path(RUN_ID).write_text("{}")
    speech_total = sum(isinstance(item, SpeechSegment) for item in generated.timeline.segments)
    store.save(
        queued.transition(
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
                cache_misses=speech_total,
                checkpoint_reuses=0,
                speech_segments_total=speech_total,
                speech_segments_completed=speech_total,
            ),
            execution=RunExecution(stage=RunStage.COMPLETED),
        )
    )
    return store


@pytest.mark.parametrize(
    "prior_edit",
    [
        None,
        TimelineEdit(silence_duration_ms={"silence-0001": 9_000}),
        TimelineEdit(delivery={"speech-0002": "compassionate"}),
    ],
)
def test_semantic_plan_revision_commits_redraft_and_restores_section_artifacts(
    tmp_path: Path,
    prior_edit: TimelineEdit | None,
) -> None:
    base = _plan()
    original = _generated(
        base,
        middle_text="Keep returning to one simple anchor with patient steady attention.",
    )
    original = original.model_copy(
        update={
            "raw_attempts": [
                RawGenerationAttempt(
                    stage="section:practice", attempt=1, text="original draft", elapsed_seconds=1
                )
            ]
        }
    )
    store = _completed_generated_run(tmp_path / "runs", original)
    revisions = RevisionStore(store)
    prior_revision = (
        revisions.create(RUN_ID, RevisionRequest(timeline=prior_edit))
        if prior_edit is not None
        else None
    )
    prior_timeline = store.load_timeline(RUN_ID)
    prior_context = {
        path.relative_to(store.run_directory(RUN_ID)): path.read_bytes()
        for root in (store.draft_sections_path(RUN_ID), store.raw_model_output_path(RUN_ID))
        for path in root.rglob("*")
        if path.is_file()
    }
    changed_section = base.sections[1].model_copy(
        update={
            "technique": "noting",
            "purpose": "Name distraction softly, then return to the chosen anchor.",
            "delivery": "calm",
        }
    )
    requested = base.model_copy(
        update={"sections": [base.sections[0], changed_section, base.sections[2]]}
    )
    requested = validate_semantic_plan_edit(base, requested)
    redraft = _generated(
        requested,
        middle_text="Name each distraction softly and return without adding any judgment.",
    )

    revision = RevisionStore(store).create_semantic_plan_revision(
        RUN_ID,
        base_plan=base,
        base_revision_id=revisions.active_revision_id(RUN_ID),
        requested_plan=requested,
        generated=redraft,
        note="Use reviewed noting in the practice beat.",
    )

    assert revision.change_kind == "plan"
    assert revision.affected_speech_segment_ids
    assert store.load_script(RUN_ID) == redraft.script
    assert MeditationPlan.model_validate_json(store.plan_path(RUN_ID).read_text()) == requested
    saved_section = DraftedSection.model_validate_json(
        (store.draft_sections_path(RUN_ID) / "practice.json").read_text()
    )
    assert "distraction" in saved_section.text
    execution = store.load(RUN_ID).execution
    assert execution is not None
    assert execution.stage is RunStage.SYNTHESIZING

    target = prior_revision or revisions.list(RUN_ID)[0]
    revisions.restore(RUN_ID, target.revision_id)
    assert store.load_timeline(RUN_ID) == prior_timeline
    restored_context = {
        path.relative_to(store.run_directory(RUN_ID)): path.read_bytes()
        for root in (store.draft_sections_path(RUN_ID), store.raw_model_output_path(RUN_ID))
        for path in root.rglob("*")
        if path.is_file()
    }
    assert restored_context == prior_context
    restored_section = DraftedSection.model_validate_json(
        (store.draft_sections_path(RUN_ID) / "practice.json").read_text()
    )
    assert restored_section.text == original.sections[1].text


@pytest.mark.parametrize("has_prior_revision", [False, True])
@pytest.mark.parametrize(
    "concurrent_edit",
    [
        RevisionRequest(script="Notice the sounds around you and settle comfortably."),
        RevisionRequest(timeline=TimelineEdit(silence_duration_ms={"silence-0001": 9_000})),
    ],
)
def test_semantic_plan_revision_rejects_same_plan_edit_committed_during_drafting(
    tmp_path: Path,
    has_prior_revision: bool,
    concurrent_edit: RevisionRequest,
) -> None:
    base = _plan()
    store = _completed_generated_run(
        tmp_path / "runs",
        _generated(
            base,
            middle_text="Keep returning to one simple anchor with patient steady attention.",
        ),
    )
    revisions = RevisionStore(store)
    if has_prior_revision:
        revisions.create(
            RUN_ID,
            RevisionRequest(timeline=TimelineEdit(delivery={"speech-0002": "compassionate"})),
        )
    parent_revision_id = revisions.active_revision_id(RUN_ID)
    requested = base.model_copy(update={"requested_outcome": "Notice distraction gently."})
    redraft = _generated(
        requested,
        middle_text="Name each distraction softly and return without adding any judgment.",
    )

    committed = revisions.create(RUN_ID, concurrent_edit)
    committed_record = store.load(RUN_ID)
    committed_script = store.load_script(RUN_ID)
    committed_timeline = store.load_timeline(RUN_ID)
    committed_revisions = revisions.list(RUN_ID)
    assert MeditationPlan.model_validate_json(store.plan_path(RUN_ID).read_text()) == base

    with pytest.raises(RevisionError, match="active revision changed"):
        revisions.create_semantic_plan_revision(
            RUN_ID,
            base_plan=base,
            base_revision_id=parent_revision_id,
            requested_plan=requested,
            generated=redraft,
            note="Guide gentle noting.",
        )

    assert revisions.active_revision_id(RUN_ID) == committed.revision_id
    assert revisions.list(RUN_ID) == committed_revisions
    assert store.load(RUN_ID) == committed_record
    assert store.load_script(RUN_ID) == committed_script
    assert store.load_timeline(RUN_ID) == committed_timeline
    assert MeditationPlan.model_validate_json(store.plan_path(RUN_ID).read_text()) == base


def test_web_plan_edit_launches_production_regenerator_with_validated_plan(
    tmp_path: Path,
) -> None:
    base = _plan()
    store = _completed_generated_run(
        tmp_path / "runs",
        _generated(
            base,
            middle_text="Keep returning to one simple anchor with patient steady attention.",
        ),
    )
    changed = base.sections[1].model_copy(
        update={
            "technique": "noting",
            "purpose": "Name distraction softly, then return to the chosen anchor.",
        }
    )
    requested = base.model_copy(update={"sections": [base.sections[0], changed, base.sections[2]]})
    launched = threading.Event()
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured["command"] = command
            plan_path = Path(command[command.index("--plan-file") + 1])
            captured["plan"] = MeditationPlan.model_validate_json(plan_path.read_text())
            launched.set()

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
        command_launcher=FakeProcess,
    )

    response = application.edit_run(
        str(RUN_ID),
        {
            "plan": requested.model_dump(mode="json"),
            "note": "Use reviewed noting in the practice beat.",
        },
    )

    assert response is not None
    assert launched.wait(timeout=2)
    command = captured["command"]
    assert command[:5] == [command[0], "-m", "whoopy", "run", "regenerate-plan"]
    assert command[5] == str(RUN_ID)
    submitted = captured["plan"]
    assert isinstance(submitted, MeditationPlan)
    assert submitted.sections[1].technique == "noting"
    # The reviewed mapping overrides arbitrary delivery values before a model loads.
    assert submitted.sections[0].delivery == "warm"
    assert store.load(RUN_ID).status is RunStatus.COMPLETED


def test_plan_regeneration_cli_rejects_noop_before_loading_a_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plan = _plan()
    store = _completed_generated_run(
        tmp_path / "runs",
        _generated(
            plan,
            middle_text="Keep returning to one simple anchor with patient steady attention.",
        ),
    )
    plan_file = tmp_path / "same-plan.json"
    plan_file.write_text(plan.model_dump_json(indent=2) + "\n")

    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "run",
                "regenerate-plan",
                str(RUN_ID),
                "--plan-file",
                str(plan_file),
                "--config-dir",
                "config",
                "--runs-dir",
                str(store.root),
            ]
        )

    assert stopped.value.code == 2
    assert "does not change the active plan" in capsys.readouterr().err


def test_generated_pause_edit_renders_and_restores_exact_timing(tmp_path: Path) -> None:
    generated = _generated(
        _plan(), middle_text="Notice the support beneath you and let attention settle here."
    )
    store = _completed_generated_run(tmp_path / "runs", generated)
    revisions = RevisionStore(store)
    edited = revisions.create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(silence_duration_ms={"silence-0001": 1_000})),
    )
    expected = store.load_timeline(RUN_ID)
    completed = LocalWorker(store).resume(RUN_ID)
    assert completed.status is RunStatus.COMPLETED
    assert store.load_timeline(RUN_ID) == expected
    assert expected.preserve_timing is True
    manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    silence_span = next(s for s in manifest["segments"] if s["segment_id"] == "silence-0001")
    assert silence_span["requested_duration_ms"] == 1_000
    with wave.open(str(store.audio_path(RUN_ID)), "rb") as audio:
        assert manifest["duration_ms"] == pytest.approx(
            audio.getnframes() * 1_000 / audio.getframerate()
        )
    assert manifest["duration_ms"] < 300_000
    quality = json.loads(store.quality_path(RUN_ID).read_text())
    assert quality["passed"] is True
    assert all(check["name"] != "requested_duration" for check in quality["checks"])
    revisions.create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(silence_duration_ms={"silence-0001": 2_000})),
    )
    revisions.restore(RUN_ID, edited.revision_id)
    restored = LocalWorker(store).resume(RUN_ID)
    assert restored.status is RunStatus.COMPLETED
    assert store.load_timeline(RUN_ID) == expected
    restored_manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    assert restored_manifest["duration_ms"] == manifest["duration_ms"]
    assert restored_manifest["pcm_sha256"] == manifest["pcm_sha256"]


def test_wording_edit_preserves_accepted_generated_pause_timing(tmp_path: Path) -> None:
    generated = _generated(
        _plan(), middle_text="Notice the support beneath you and let attention settle here."
    )
    first_silence = next(
        segment for segment in generated.timeline.segments if isinstance(segment, SilenceSegment)
    )
    store = _completed_generated_run(tmp_path / "runs", generated)
    revisions = RevisionStore(store)
    pause_revision = revisions.create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(silence_duration_ms={first_silence.id: 1_000})),
    )
    worker = LocalWorker(store)
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    pause_manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    script = store.load_script(RUN_ID)
    edited = revisions.create(
        RUN_ID,
        RevisionRequest(script=script.replace("steady support", "comfortable steady support", 1)),
    )
    assert edited.change_kind == "script"
    assert edited.affected_speech_segment_ids == ("speech-0001",)
    expected = store.load_timeline(RUN_ID)
    assert expected.preserve_timing is True
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    assert store.load_timeline(RUN_ID) == expected
    manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    silence_span = next(s for s in manifest["segments"] if s["segment_id"] == first_silence.id)
    assert silence_span["requested_duration_ms"] == 1_000
    assert silence_span["actual_duration_ms"] == 1_000
    with wave.open(str(store.audio_path(RUN_ID)), "rb") as audio:
        assert manifest["duration_ms"] == pytest.approx(
            audio.getnframes() * 1_000 / audio.getframerate()
        )
    assert manifest["duration_ms"] < 300_000
    assert manifest["pcm_sha256"] != pause_manifest["pcm_sha256"]
    revisions.restore(RUN_ID, pause_revision.revision_id)
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    restored_manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    assert restored_manifest["duration_ms"] == pause_manifest["duration_ms"]
    assert restored_manifest["pcm_sha256"] == pause_manifest["pcm_sha256"]


@pytest.mark.parametrize("prior_pause_edit", [False, True])
def test_explicit_script_pause_change_overrides_generated_timing(
    tmp_path: Path, prior_pause_edit: bool
) -> None:
    generated = _generated(
        _plan(), middle_text="Notice the support beneath you and let attention settle here."
    )
    store = _completed_generated_run(tmp_path / "runs", generated)
    revisions = RevisionStore(store)
    if prior_pause_edit:
        revisions.create(
            RUN_ID,
            RevisionRequest(timeline=TimelineEdit(silence_duration_ms={"silence-0001": 1_000})),
        )
    script = store.load_script(RUN_ID)
    compiled = build_script_timeline(run_id=RUN_ID, script=script, created_at=NOW)
    first_pause = next(s for s in compiled.segments if isinstance(s, SilenceSegment))
    revisions.create(
        RUN_ID,
        RevisionRequest(
            script=script.replace(f"[pause: {first_pause.duration_ms}ms]", "[pause: 2000ms]", 1)
        ),
    )

    assert store.load_timeline(RUN_ID).preserve_timing is True
    assert LocalWorker(store).resume(RUN_ID).status is RunStatus.COMPLETED
    manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    span = next(s for s in manifest["segments"] if s["segment_id"] == "silence-0001")
    assert span["requested_duration_ms"] == 2_000
    assert span["actual_duration_ms"] == 2_000
    with wave.open(str(store.audio_path(RUN_ID)), "rb") as audio:
        assert manifest["duration_ms"] == pytest.approx(
            audio.getnframes() * 1_000 / audio.getframerate()
        )


def test_pace_edit_resynthesizes_only_changed_speech(tmp_path: Path) -> None:
    synthesized: list[str] = []

    class CountingSynthesizer(FixtureSpeechSynthesizer):
        def synthesize(self, segment: SpeechSegment) -> PcmAudio:
            synthesized.append(segment.id)
            return super().synthesize(segment)

    store = _completed_script_run(tmp_path / "runs")
    revisions = RevisionStore(store)
    revisions.create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(silence_duration_ms={"silence-0001": 2_000})),
    )
    worker = LocalWorker(store, synthesizer=CountingSynthesizer())
    worker.resume(RUN_ID)
    synthesized.clear()
    edited = revisions.create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(pace={"speech-0002": "very_slow"})),
    )
    completed = worker.resume(RUN_ID)
    assert edited.affected_speech_segment_ids == ("speech-0002",)
    assert synthesized == ["speech-0002"]
    assert completed.status is RunStatus.COMPLETED
    assert completed.recovery is not None
    assert completed.recovery.checkpoint_reuses == 1
    assert store.load_timeline(RUN_ID).preserve_timing is True
    manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    silence_span = next(s for s in manifest["segments"] if s["segment_id"] == "silence-0001")
    assert silence_span["actual_duration_ms"] == 2_000


@pytest.mark.parametrize("expand_paragraph", [False, True])
def test_wording_revision_preserves_rendered_controls_and_fitted_pauses(
    tmp_path: Path, expand_paragraph: bool
) -> None:
    generated = _generated(
        _plan(), middle_text="Notice the support beneath you and let attention settle here."
    )
    store = _completed_generated_run(tmp_path / "runs", generated)
    revisions = RevisionStore(store)
    worker = LocalWorker(store)
    revisions.create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(delivery={"speech-0001": "warm"})),
    )
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    fitted = [
        segment
        for segment in store.load_timeline(RUN_ID).segments
        if isinstance(segment, SilenceSegment)
    ]
    assert fitted != [
        segment for segment in generated.timeline.segments if isinstance(segment, SilenceSegment)
    ]
    controlled = revisions.create(
        RUN_ID,
        RevisionRequest(
            timeline=TimelineEdit(
                pace={"speech-0001": "slow", "speech-0003": "very_slow"},
                delivery={"speech-0001": "compassionate"},
                intensity={"speech-0001": "moderate"},
                pronunciation_overrides={
                    "speech-0001": (
                        PronunciationOverride(source="steady", pronunciation="sted-ee"),
                    ),
                },
            )
        ),
    )
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    accepted_timeline = store.load_timeline(RUN_ID)
    accepted_manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    editable_script = store.load_script(RUN_ID)
    replacement = "let your attention"
    if expand_paragraph:
        replacement = "Notice the support beneath your feet. " * 20 + replacement
    wording = revisions.create(
        RUN_ID,
        RevisionRequest(script=editable_script.replace("let attention", replacement, 1)),
    )
    if expand_paragraph:
        assert len(store.load_timeline(RUN_ID).segments) > len(accepted_timeline.segments)
        assert "speech-0001" not in wording.affected_speech_segment_ids
    else:
        assert wording.affected_speech_segment_ids == ("speech-0002",)
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    rendered = store.load_timeline(RUN_ID)
    assert rendered.segments[0] == accepted_timeline.segments[0]
    ending = next(
        segment for segment in reversed(rendered.segments) if isinstance(segment, SpeechSegment)
    )
    assert ending.pace == "very_slow"
    assert [s for s in rendered.segments if isinstance(s, SilenceSegment)] == fitted
    manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    for pause in fitted:
        span = next(s for s in manifest["segments"] if s["segment_id"] == pause.id)
        assert span["actual_duration_ms"] == pause.duration_ms
    with wave.open(str(store.audio_path(RUN_ID)), "rb") as audio:
        assert manifest["duration_ms"] == pytest.approx(
            audio.getnframes() * 1_000 / audio.getframerate()
        )
    revisions.restore(RUN_ID, controlled.revision_id)
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    assert store.load_timeline(RUN_ID) == accepted_timeline
    restored_manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    assert restored_manifest["pcm_sha256"] == accepted_manifest["pcm_sha256"]
    assert restored_manifest["duration_ms"] == accepted_manifest["duration_ms"]


@pytest.mark.parametrize("pace, delivery", [("very_slow", "warm"), ("natural", "neutral")])
@pytest.mark.parametrize("expand_paragraph", [False, True])
def test_new_script_controls_override_accepted_controls(
    tmp_path: Path, pace: str, delivery: str, expand_paragraph: bool
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    revisions = RevisionStore(store)
    revisions.create(
        RUN_ID,
        RevisionRequest(
            timeline=TimelineEdit(
                pace={"speech-0001": "slow"},
                delivery={"speech-0001": "compassionate"},
                pronunciation_overrides={
                    "speech-0001": (
                        PronunciationOverride(source="Welcome", pronunciation="Well-come"),
                    ),
                },
            )
        ),
    )
    assert LocalWorker(store).resume(RUN_ID).status is RunStatus.COMPLETED
    script = store.load_script(RUN_ID)
    if expand_paragraph:
        script = script.replace(
            "Notice the support beneath you.", "Notice the support beneath you. " * 24
        )
    revisions.create(
        RUN_ID,
        RevisionRequest(script=f"[pace: {pace}]\n[delivery: {delivery}]\n\n" + script),
    )
    if expand_paragraph:
        assert len(store.load_timeline(RUN_ID).segments) > 3
    first = store.load_timeline(RUN_ID).segments[0]
    assert isinstance(first, SpeechSegment)
    assert first.pace == pace
    assert first.delivery == delivery
    assert first.pronunciation_overrides == (
        PronunciationOverride(source="Welcome", pronunciation="Well-come"),
    )
    assert LocalWorker(store).resume(RUN_ID).status is RunStatus.COMPLETED


@pytest.mark.parametrize(
    "marker, extra_speech, expected_pace",
    [
        ("[pace: slow]", "", "very_slow"),
        ("[pace: natural]", "", "natural"),
        ("[pace: slow]\n[pace: slow]", "", "slow"),
        ("", "", "natural"),
        ("[pace: slow]", "Notice the sounds around you.\n\n", "slow"),
        ("[pace: slow]", "Notice the support beneath you.\n\n", "slow"),
    ],
    ids=[
        "unchanged",
        "replacement",
        "explicit-reset",
        "removed",
        "unmatched-boundary",
        "duplicate-boundary",
    ],
)
def test_shifted_control_marker_preserves_only_corresponding_accepted_controls(
    tmp_path: Path, marker: str, extra_speech: str, expected_pace: str
) -> None:
    store = _completed_script_run(tmp_path / "runs")
    revisions = RevisionStore(store)
    worker = LocalWorker(store)
    initial_script = SCRIPT.replace("Notice", "[pace: slow]\n\nNotice", 1)
    revisions.create(RUN_ID, RevisionRequest(script=initial_script))
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    controlled = revisions.create(
        RUN_ID,
        RevisionRequest(timeline=TimelineEdit(pace={"speech-0002": "very_slow"})),
    )
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    accepted = store.load_timeline(RUN_ID)
    accepted_manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    expanded_script = initial_script.replace(
        "Welcome to this practice.", "Welcome to this practice. " * 30, 1
    ).replace("[pace: slow]\n\n", f"{marker}\n\n{extra_speech}", 1)

    revisions.create(RUN_ID, RevisionRequest(script=expanded_script))
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    rendered = store.load_timeline(RUN_ID)
    speeches = [segment for segment in rendered.segments if isinstance(segment, SpeechSegment)]
    assert len(speeches) > 2
    assert speeches[-1].text == "Notice the support beneath you."
    assert all(
        segment.pace == expected_pace
        for segment in speeches
        if segment.text == "Notice the support beneath you."
    )

    revisions.restore(RUN_ID, controlled.revision_id)
    assert worker.resume(RUN_ID).status is RunStatus.COMPLETED
    assert store.load_timeline(RUN_ID) == accepted
    restored_manifest = json.loads(store.audio_manifest_path(RUN_ID).read_text())
    assert restored_manifest["pcm_sha256"] == accepted_manifest["pcm_sha256"]
