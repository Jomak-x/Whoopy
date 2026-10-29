from __future__ import annotations

import json
import re
import shutil
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from whoopy.meditation import (
    DraftedSection,
    GenerationError,
    GenerationWorkspace,
    LocalMeditationGenerator,
    compile_breath_protocol,
    load_blueprint_library,
    load_prompt_bundle,
    review_script,
)
from whoopy.meditation.editorial import (
    apply_reviewed_style_contract,
    apply_reviewed_technique_contract,
)
from whoopy.meditation.generator import (
    _allocate_plan,
    _compact_plan_for_duration,
    _fit_complete_sentences,
    _fit_script_to_requested_duration,
    _repair_overlong_sentence_boundaries,
    _validate_plan_for_request,
)
from whoopy.meditation.models import MeditationKind, ProposedPlan
from whoopy.meditation.pacing import sentence_word_count, split_sentences
from whoopy.meditation.prompts import PromptLoadError
from whoopy.meditation.safety import ContentSafetyError, validate_meditation_text
from whoopy.ports import (
    AdapterMetadata,
    ScriptGenerationRequest,
    ScriptGenerationResult,
)
from whoopy.timeline import SilenceSegment, build_script_timeline

RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
CREATED_AT = datetime(2026, 7, 26, tzinfo=UTC)


class SequenceGenerator:
    """Small adapter double that still obeys the production port."""

    metadata = AdapterMetadata(
        adapter_id="test.sequence",
        versioned_model_id="fixture@1",
        runtime_id="python",
        runtime_version="test",
        license_id="CC0-1.0",
        device="fixture",
    )

    def __init__(self, outputs: list[str]) -> None:
        self.outputs = deque(outputs)
        self.requests: list[ScriptGenerationRequest] = []

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        self.requests.append(request)
        return ScriptGenerationResult(
            text=self.outputs.popleft(),
            metadata=self.metadata,
            elapsed_seconds=0.01,
        )


def _plan() -> str:
    return json.dumps(
        {
            "title": "A Steady Moment",
            "intention": "Offer a gentle grounding pause.",
            "sections": [
                {
                    "id": "arrive",
                    "title": "Arrive",
                    "purpose": "Invite awareness of support.",
                    "technique": "arrival",
                    "weight": 1,
                    "pause_seconds": 6,
                },
                {
                    "id": "notice",
                    "title": "Notice",
                    "purpose": "Notice simple body sensations.",
                    "technique": "body_scan",
                    "weight": 1,
                    "pause_seconds": 6,
                },
                {
                    "id": "return",
                    "title": "Return",
                    "purpose": "Return attention to the room.",
                    "technique": "return",
                    "weight": 1,
                    "pause_seconds": 6,
                },
            ],
        }
    )


def _section(section_id: str, word: str) -> str:
    text = (
        f"{word.title()} gently into this quiet moment. "
        f"Notice {word} steady support beneath you. "
        f"Let {word} simple experience be enough for now."
    )
    return json.dumps({"section_id": section_id, "text": text})


def test_prompt_bundle_loads_reviewable_versions() -> None:
    prompts = load_prompt_bundle(Path("config/prompts"))

    assert prompts.plan.prompt_id == "whoopy.plan"
    assert prompts.plan.version == 5
    assert prompts.section.version == 4


def test_plan_validation_repairs_only_internal_section_id_formatting() -> None:
    value = json.loads(_plan())
    value["sections"][1]["id"] = "Focused_Attention"

    plan = _validate_plan_for_request(value, prompt="A grounding practice")

    assert plan.sections[1].id == "focused-attention"
    assert plan.sections[1].technique == "body_scan"


def test_plan_id_repair_still_rejects_collisions() -> None:
    value = json.loads(_plan())
    value["sections"][0]["id"] = "same_id"
    value["sections"][1]["id"] = "same-id"

    with pytest.raises(ValueError, match="section IDs must be unique"):
        _validate_plan_for_request(value, prompt="A grounding practice")


def test_blueprint_library_contains_every_reviewed_v2_technique() -> None:
    library = load_blueprint_library()

    assert len(library.blueprints) == 11
    assert library.for_kind("sleep").versioned_id == "sleep@1"
    assert library.for_kind("sleep").ending_behavior == "sleep_taper"
    assert library.for_kind("grounding").ending_behavior == "daytime_reorientation"
    assert library.for_kind("loving_kindness").primary_technique == "loving_kindness"
    assert library.for_kind("open_awareness").primary_technique == "resting_awareness"


def test_reviewed_paced_breathing_has_opt_out_guidance_and_no_holds() -> None:
    script = compile_breath_protocol("gentle_even_4_4@1", cycles=3)

    assert "natural rhythm" in script
    assert "dizzy" in script
    assert "hold" not in script.casefold()
    assert script.count("[breath: inhale") == 3
    assert script.count("[breath: exhale") == 3


def test_whole_script_editorial_review_names_only_failed_sections() -> None:
    plan = _allocate_plan(ProposedPlan.model_validate_json(_plan()), 60, prompt="grounding")
    sections = [
        DraftedSection(
            section_id="arrive", text="Notice steady support beneath you.", word_count=5
        ),
        DraftedSection(
            section_id="notice",
            text="Find your peace while noticing the shoulders and hands.",
            word_count=9,
        ),
        DraftedSection(
            section_id="return",
            text="Notice sounds in the room and make one small movement.",
            word_count=10,
        ),
    ]

    report = review_script(plan=plan, sections=sections)

    assert not report.passed
    assert "notice" in report.failed_section_ids
    assert any(issue.code == "vague_reassurance" for issue in report.issues)


def test_body_scan_rejects_forced_tension_removal() -> None:
    plan = _allocate_plan(ProposedPlan.model_validate_json(_plan()), 60, prompt="grounding")
    sections = [
        DraftedSection(section_id="arrive", text="Feel steady support beneath you.", word_count=5),
        DraftedSection(
            section_id="notice",
            text="Scan the shoulders and release any tension now.",
            word_count=8,
        ),
        DraftedSection(
            section_id="return",
            text="Hear the room and invite a small movement.",
            word_count=8,
        ),
    ]

    report = review_script(plan=plan, sections=sections)

    assert any(issue.code == "technique_mismatch" for issue in report.issues)


def test_reviewed_style_contract_removes_loops_and_varies_mechanical_openings() -> None:
    sections = [
        DraftedSection(
            section_id="arrive",
            text=(
                "You might notice the floor. You might notice a nearby sound. "
                "You might notice the chair. If it feels comfortable, stay. "
                "If it feels right, rest. If it feels safe, listen."
            ),
            word_count=31,
        ),
        DraftedSection(
            section_id="notice",
            text="You might notice the floor. Feel the hands resting quietly.",
            word_count=10,
        ),
    ]

    polished = apply_reviewed_style_contract(sections)
    combined = " ".join(section.text for section in polished)

    assert combined.count("You might notice") == 2
    assert "Notice the chair." in combined
    assert combined.count("If it feels") == 2
    assert "Listen." in combined
    assert combined.count("You might notice the floor.") == 1
    assert all(
        section.word_count == len(re.findall(r"\b[\w'-]+\b", section.text)) for section in polished
    )


def test_reviewed_style_contract_reorders_repeated_imperative_qualifiers() -> None:
    sections = [
        DraftedSection(
            section_id="practice",
            text=(
                "Let the breath flow naturally, without forcing it. "
                "Let the breath be, if it feels good. "
                "Let the breath be, if it feels heavy. "
                "Let the breath be, if it feels light."
            ),
            word_count=32,
        )
    ]

    polished = apply_reviewed_style_contract(sections)

    assert "If it feels heavy, let the breath be." in polished[0].text
    assert "If it feels light, let the breath be." in polished[0].text
    assert (
        sum(
            sentence.casefold().startswith("let the breath")
            for sentence in split_sentences(polished[0].text)
        )
        == 2
    )
    assert polished[0].word_count == sections[0].word_count


def test_editorial_review_detects_exact_loop_inside_one_section() -> None:
    plan = _allocate_plan(ProposedPlan.model_validate_json(_plan()), 60, prompt="grounding")
    repeated = "Notice the steady support beneath you."
    sections = [
        DraftedSection(
            section_id="arrive",
            text=f"{repeated} {repeated}",
            word_count=12,
        ),
        DraftedSection(
            section_id="notice",
            text="Feel the hands resting on the lap.",
            word_count=7,
        ),
        DraftedSection(
            section_id="return",
            text="Hear the room and invite a small movement.",
            word_count=8,
        ),
    ]

    report = review_script(plan=plan, sections=sections)

    assert any(issue.code == "repeated_idea" for issue in report.issues)


def test_sentence_fitting_preserves_valid_prose_when_one_sentence_is_overlong() -> None:
    text = (
        "This deliberately overlong sentence contains far too many unnecessary words and "
        "should never force otherwise useful meditation guidance to be regenerated from "
        "scratch today. "
        "Notice both feet resting on the floor. "
        "Feel the steady support beneath them."
    )

    fitted = _fit_complete_sentences(
        text,
        purpose="notice steady support beneath both feet",
        minimum_words=12,
        maximum_words=16,
        maximum_sentence_words=22,
    )

    assert fitted == (
        "Notice both feet resting on the floor. Feel the steady support beneath them."
    )


def test_sentence_fitting_splits_after_closing_quotation_marks() -> None:
    text = (
        "Offer a well-wish: 'May this moment be met with kindness.' "
        "Add another: 'May I respond with patience.'"
    )

    fitted = _fit_complete_sentences(
        text,
        purpose="offer kindness and patience",
        minimum_words=15,
        maximum_words=22,
        maximum_sentence_words=22,
    )

    assert fitted == text


def test_reviewed_body_scan_contract_replaces_forced_relaxation() -> None:
    repaired = apply_reviewed_technique_contract(
        technique="body_scan",
        text="Feel the shoulders and release any tension before moving to the hands.",
    )

    assert repaired == ("Feel the shoulders and notice any tension before moving to the hands.")


def test_sentence_fitting_reports_word_and_sentence_constraints_together() -> None:
    text = (
        "This sentence contains many extra words that make it intentionally much too long "
        "for the reviewed meditation narration sentence contract right now during local "
        "generation. "
        "Notice both feet."
    )

    with pytest.raises(ValueError) as error:
        _fit_complete_sentences(
            text,
            purpose="notice both feet",
            minimum_words=10,
            maximum_words=20,
            maximum_sentence_words=22,
        )

    detail = str(error.value)
    assert "provide 3 usable words; expected 10-20" in detail
    assert "excluded 1 overlong sentence" in detail
    assert "original response contained" in detail


@pytest.mark.parametrize(
    ("text", "expected"),
    (
        (
            "Notice the soft touch of the floor beneath your feet, feel the steady "
            "rhythm of your breath, and let your body settle into the present moment.",
            "Notice the soft touch of the floor beneath your feet. Feel the steady "
            "rhythm of your breath. Let your body settle into the present moment.",
        ),
        (
            "Notice the sensation of your breath moving in and out, let it be as it is, "
            "feel the air touch your skin, if it feels comfortable, stay with it.",
            "Notice the sensation of your breath moving in and out. Let it be as it is. "
            "Feel the air touch your skin. If it feels comfortable, stay with it.",
        ),
    ),
)
def test_overlong_clause_list_repair_only_changes_sentence_boundaries(
    text: str,
    expected: str,
) -> None:
    repaired = _repair_overlong_sentence_boundaries(text)

    assert repaired == expected
    assert all(sentence_word_count(sentence) <= 22 for sentence in split_sentences(repaired))


def test_arrival_uses_validated_boundary_repair_after_model_retries() -> None:
    overlong_arrival = json.dumps(
        {
            "section_id": "arrive",
            "text": (
                "Notice the soft touch of the floor beneath your feet, feel the steady "
                "rhythm of your breath, and let your body settle into the present moment."
            ),
        }
    )
    adapter = SequenceGenerator(
        [
            _plan(),
            overlong_arrival,
            overlong_arrival,
            overlong_arrival,
            _section("notice", "notice"),
            _section("return", "return"),
        ]
    )

    result = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
    ).generate(
        prompt="Grounding.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert len(adapter.requests) == 6
    assert all(attempt.validation_error is not None for attempt in result.raw_attempts[1:4])
    assert result.sections[0].text == (
        "Notice the soft touch of the floor beneath your feet. "
        "Feel the steady rhythm of your breath. "
        "Let your body settle into the present moment."
    )


def test_daytime_ending_accepts_a_concrete_step_into_the_environment() -> None:
    plan = _allocate_plan(ProposedPlan.model_validate_json(_plan()), 60, prompt="grounding")
    sections = [
        DraftedSection(section_id="arrive", text="Notice support beneath you.", word_count=4),
        DraftedSection(
            section_id="notice",
            text="Feel the shoulders and hands without needing to change them.",
            word_count=10,
        ),
        DraftedSection(
            section_id="return",
            text="Take one small step and let the environment anchor you.",
            word_count=10,
        ),
    ]

    report = review_script(plan=plan, sections=sections)

    assert not any(issue.code == "incorrect_daytime_ending" for issue in report.issues)


def test_generation_applies_reviewed_daytime_fallback_after_failed_llm_repair(
    tmp_path: Path,
) -> None:
    original_ending = json.dumps(
        {
            "section_id": "return",
            "text": (
                "Pause gently at the close of this practice. Allow this quiet moment to "
                "settle naturally. Let the final breath remain unforced and easy. Notice "
                "the simple warmth of being here. Give yourself permission to finish "
                "without needing anything different."
            ),
        }
    )
    repair_ending = json.dumps(
        {
            "section_id": "return",
            "text": (
                "Pause at the close without changing your posture. Let this practice settle "
                "at its own pace. Stay with one natural breath. Finish quietly whenever the "
                "moment feels complete. Nothing needs to be added here."
            ),
        }
    )
    workspace = GenerationWorkspace(tmp_path / "ending-fallback")
    adapter = SequenceGenerator(
        [
            _plan(),
            _section("arrive", "settle"),
            _section("notice", "notice"),
            original_ending,
            repair_ending,
        ]
    )

    result = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
        workspace=workspace,
    ).generate(
        prompt="A daytime grounding meditation.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert "hear the room" in result.sections[-1].text
    assert result.sections[0].text == json.loads(_section("arrive", "settle"))["text"]
    assert result.sections[1].text == json.loads(_section("notice", "notice"))["text"]
    assert all(
        planned.minimum_words <= drafted.word_count <= planned.maximum_words
        for planned, drafted in zip(result.plan.sections, result.sections, strict=True)
    )
    assert result.editorial is not None
    assert not any(issue.code == "incorrect_daytime_ending" for issue in result.editorial.issues)
    attempts = workspace.root / "raw-model-output"
    assert (attempts / "section-return-01.json").is_file()
    assert (attempts / "section-return-editorial-repair-01.json").is_file()
    original_attempt, repair_attempt = result.raw_attempts[-2:]
    assert original_attempt.stage == "section:return"
    assert repair_attempt.stage == "section:return:editorial-repair"
    assert original_attempt.text != repair_attempt.text


def test_generation_sleep_fallback_removes_waking_language_and_preserves_other_sections() -> None:
    arrival = json.dumps(
        {
            "section_id": "arrival",
            "text": (
                "Feel the bed supporting your shoulders and legs as nearby sounds soften at "
                "their own pace."
            ),
        }
    )
    practice = json.dumps(
        {
            "section_id": "deeper-practice",
            "text": (
                "Move attention through the forehead, jaw, shoulders, hands, hips, and feet. "
                "Notice pressure, warmth, tension, or ease without forcing any change. Rest "
                "with the natural breath as sensations continue in their own rhythm."
            ),
        }
    )
    waking_ending = json.dumps(
        {
            "section_id": "ending",
            "text": ("Open your eyes and return to the room. Become alert and prepare to sit up."),
        }
    )
    waking_repair = json.dumps(
        {
            "section_id": "ending",
            "text": "Wake up now, stand up, and rise from the bed when you are ready.",
        }
    )
    result = LocalMeditationGenerator(
        SequenceGenerator([arrival, practice, waking_ending, waking_repair]),
        load_prompt_bundle(Path("config/prompts")),
    ).generate(
        prompt="A gentle meditation for sleep.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
        meditation_kind="sleep",
        writing_method="blueprint_first_sparse",
    )

    assert result.sections[0].text == json.loads(arrival)["text"]
    assert result.sections[1].text == json.loads(practice)["text"]
    assert "remain resting" in result.sections[-1].text
    assert not re.search(
        r"open your eyes|return to the room|become alert|wake up|sit up|stand up|rise from",
        result.sections[-1].text,
        re.I,
    )
    assert result.editorial is not None and result.editorial.passed
    assert result.plan.sections[-1].pause_intent == "extended"
    assert all(
        planned.minimum_words <= drafted.word_count <= planned.maximum_words
        for planned, drafted in zip(result.plan.sections, result.sections, strict=True)
    )


def test_unsafe_editorial_repair_cannot_enter_reviewed_ending_fallback() -> None:
    unsafe_repair = json.dumps(
        {
            "section_id": "return",
            "text": "This practice is guaranteed to cure anxiety. " + "Rest quietly. " * 8,
        }
    )
    adapter = SequenceGenerator(
        [
            _plan(),
            _section("arrive", "settle"),
            _section("notice", "notice"),
            json.dumps(
                {
                    "section_id": "return",
                    "text": (
                        "Pause here without moving and let the quiet moment end gently. "
                        "Allow one natural breath."
                    ),
                }
            ),
            unsafe_repair,
            unsafe_repair,
            unsafe_repair,
        ]
    )

    with pytest.raises(GenerationError, match="section:return:editorial-repair remained invalid"):
        LocalMeditationGenerator(
            adapter,
            load_prompt_bundle(Path("config/prompts")),
        ).generate(
            prompt="A daytime grounding meditation.",
            duration_seconds=60,
            run_id=RUN_ID,
            created_at=CREATED_AT,
        )


def test_prompt_without_front_matter_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "plan_system.md").write_text("missing metadata", encoding="utf-8")

    with pytest.raises(PromptLoadError, match="front matter"):
        load_prompt_bundle(tmp_path)


def test_plan_first_generation_produces_valid_script_and_timeline(tmp_path: Path) -> None:
    adapter = SequenceGenerator(
        [
            _plan(),
            _section("arrive", "settle"),
            _section("notice", "notice"),
            _section("return", "return"),
        ]
    )
    workspace = GenerationWorkspace(tmp_path / "draft")
    generator = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
        workspace=workspace,
    )

    result = generator.generate(
        prompt="A gentle grounding meditation.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert [request.seed for request in adapter.requests] == [42, 142, 152, 162]
    assert [section.section_id for section in result.sections] == [
        "arrive",
        "notice",
        "return",
    ]
    assert result.script.startswith("# A Steady Moment")
    assert len(result.timeline.segments) == 16
    silence_ms = sum(
        segment.duration_ms
        for segment in result.timeline.segments
        if isinstance(segment, SilenceSegment)
    )
    assert silence_ms / 1_000 == pytest.approx(result.plan.silence_budget_seconds, abs=1)
    assert result.estimated_duration_seconds == pytest.approx(60, abs=0.01)

    saved_script = (workspace.root / "script.md").read_text(encoding="utf-8")
    recompiled = build_script_timeline(
        run_id=RUN_ID,
        script=saved_script,
        created_at=CREATED_AT,
        source="generated_prompt",
    )
    assert recompiled == result.timeline
    assert workspace.load_plan() == result.plan
    for planned, section_script in zip(
        result.plan.sections, saved_script.split("[delivery:")[1:], strict=True
    ):
        section_timeline = build_script_timeline(
            run_id=RUN_ID,
            script="[delivery:" + section_script,
            created_at=CREATED_AT,
            source="generated_prompt",
        )
        ending = section_timeline.segments[-1]
        assert isinstance(ending, SilenceSegment)
        assert ending.duration_ms == planned.pause_after_ms


def test_blueprint_first_generation_skips_model_planning_and_records_method() -> None:
    adapter = SequenceGenerator(
        [
            json.dumps(
                {
                    "section_id": "arrival",
                    "text": (
                        "Feel the firm support beneath your feet and seat. Let nearby sounds "
                        "arrive without needing to identify them. Allow your posture to be "
                        "comfortable and steady."
                    ),
                }
            ),
            json.dumps(
                {
                    "section_id": "deeper-practice",
                    "text": (
                        "Move attention slowly through the shoulders, hands, and face. Notice "
                        "pressure, warmth, tension, or ease exactly as it appears. Nothing needs "
                        "to change right now."
                    ),
                }
            ),
            json.dumps(
                {
                    "section_id": "ending",
                    "text": (
                        "Hear the room around you again and feel contact with the floor. Choose "
                        "one small movement in the hands or feet. Take that grounded step into "
                        "your environment."
                    ),
                }
            ),
        ]
    )
    generator = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
    )

    result = generator.generate(
        prompt="A gentle grounding meditation.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
        meditation_kind="grounding",
        writing_method="blueprint_first_sparse",
    )

    assert [request.seed for request in adapter.requests] == [142, 152, 162]
    assert [section.section_id for section in result.sections] == [
        "arrival",
        "deeper-practice",
        "ending",
    ]
    assert result.plan.blueprint_id == "grounding@1"
    assert result.writing_method == "blueprint_first_sparse"
    assert result.plan.sections[1].pause_intent == "extended"
    assert result.plan.sections[1].pause_after_ms > result.plan.sections[-1].pause_after_ms
    assert result.plan.sections[-1].pause_after_ms < 10_000


def test_three_candidate_method_generates_and_structurally_selects_three_plans() -> None:
    adapter = SequenceGenerator(
        [
            _plan(),
            _plan(),
            _plan(),
            _section("arrive", "settle"),
            _section("notice", "notice"),
            _section("return", "return"),
        ]
    )
    generator = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
    )

    result = generator.generate(
        prompt="A gentle grounding meditation.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
        writing_method="three_candidate_structural_selection",
    )

    assert [request.seed for request in adapter.requests[:3]] == [42, 1042, 2042]
    assert [attempt.stage for attempt in result.raw_attempts[:3]] == [
        "plan-candidate-1",
        "plan-candidate-2",
        "plan-candidate-3",
    ]
    assert [attempt.request_seed for attempt in result.raw_attempts[:3]] == [42, 1042, 2042]
    assert all(attempt.max_output_tokens == 1_200 for attempt in result.raw_attempts)
    assert all(attempt.telemetry.load_elapsed_unavailable_reason for attempt in result.raw_attempts)
    assert result.writing_method == "three_candidate_structural_selection"


def test_generation_reports_planning_then_drafting_progress() -> None:
    adapter = SequenceGenerator(
        [
            _plan(),
            _section("arrive", "settle"),
            _section("notice", "notice"),
            _section("return", "return"),
        ]
    )
    stages: list[str] = []
    generator = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
        progress_callback=stages.append,
    )

    generator.generate(
        prompt="A short grounding meditation.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert stages == ["planning", "drafting"]


def test_invalid_json_is_repaired_with_a_bounded_retry() -> None:
    adapter = SequenceGenerator(
        [
            "not json",
            _plan(),
            _section("arrive", "settle"),
            _section("notice", "notice"),
            _section("return", "return"),
        ]
    )
    result = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
    ).generate(
        prompt="Grounding.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert result.raw_attempts[0].validation_error is not None
    assert result.raw_attempts[1].validation_error is None
    assert "previous response was invalid" in adapter.requests[1].prompt


def test_sleep_request_repairs_a_plan_that_would_wake_the_listener() -> None:
    corrected = json.loads(_plan())
    corrected["sections"][-1]["purpose"] = "Let guidance taper into quiet rest."
    corrected["sections"][-1]["technique"] = "sleep_transition"
    adapter = SequenceGenerator(
        [
            _plan(),
            json.dumps(corrected),
            _section("arrive", "settle"),
            _section("notice", "notice"),
            _section("return", "rest"),
        ]
    )

    result = LocalMeditationGenerator(
        adapter,
        load_prompt_bundle(Path("config/prompts")),
    ).generate(
        prompt="Generate me a good night meditation.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert result.plan.sections[-1].technique == "sleep_transition"
    assert result.raw_attempts[0].validation_error is not None
    assert "sleep_transition" in result.raw_attempts[0].validation_error


def test_unsafe_section_cannot_reach_the_timeline() -> None:
    unsafe = json.dumps(
        {
            "section_id": "arrive",
            "text": "Hold your breath. " + " ".join(["settle"] * 43),
        }
    )
    adapter = SequenceGenerator([_plan(), unsafe, unsafe, unsafe])

    with pytest.raises(GenerationError, match="section:arrive remained invalid"):
        LocalMeditationGenerator(
            adapter,
            load_prompt_bundle(Path("config/prompts")),
        ).generate(
            prompt="Grounding.",
            duration_seconds=60,
            run_id=RUN_ID,
            created_at=CREATED_AT,
        )


@pytest.mark.parametrize(
    "text",
    [
        "Breathe in deeply and hold it for a moment.",
        "Take a deep breath and fill your lungs.",
        "Exhale slowly for four counts.",
    ],
)
def test_prescribed_breath_control_is_rejected(text: str) -> None:
    with pytest.raises(ContentSafetyError):
        validate_meditation_text(text)


def test_natural_breath_observation_is_not_mistaken_for_breath_control() -> None:
    validate_meditation_text("Let each inhale feel natural, and let each exhale remain gentle.")


@pytest.mark.parametrize(
    "text",
    (
        "Let the shoulders soften. [emotional] Continue gently.",
        'Rest here. <break time="4s"/> Then notice the breath.',
        "<speak>Let the next moment arrive.</speak>",
    ),
)
def test_generated_prose_with_model_native_cues_is_rejected_for_section_repair(
    text: str,
) -> None:
    with pytest.raises(ContentSafetyError, match="forbidden markup"):
        validate_meditation_text(text)


def test_section_outside_word_budget_is_rejected() -> None:
    too_short = json.dumps({"section_id": "arrive", "text": "Just breathe."})
    adapter = SequenceGenerator([_plan(), too_short, too_short])

    with pytest.raises(GenerationError):
        LocalMeditationGenerator(
            adapter,
            load_prompt_bundle(Path("config/prompts")),
            max_validation_attempts=2,
        ).generate(
            prompt="Grounding.",
            duration_seconds=60,
            run_id=RUN_ID,
            created_at=CREATED_AT,
        )


def test_workspace_resume_reuses_validated_plan_and_sections(tmp_path: Path) -> None:
    workspace = GenerationWorkspace(tmp_path / "draft")
    first_adapter = SequenceGenerator(
        [
            _plan(),
            _section("arrive", "settle"),
            _section("notice", "notice"),
            _section("return", "return"),
        ]
    )
    first = LocalMeditationGenerator(
        first_adapter,
        load_prompt_bundle(Path("config/prompts")),
        workspace=workspace,
    ).generate(
        prompt="Grounding.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    second_adapter = SequenceGenerator([])
    second = LocalMeditationGenerator(
        second_adapter,
        load_prompt_bundle(Path("config/prompts")),
        workspace=workspace,
    ).generate(
        prompt="Grounding.",
        duration_seconds=60,
        run_id=RUN_ID,
        created_at=CREATED_AT,
    )

    assert second_adapter.requests == []
    assert second.script == first.script
    assert (tmp_path / "draft" / "plan.json").is_file()
    assert len(list((tmp_path / "draft" / "sections").glob("*.json"))) == 3
    assert (tmp_path / "draft" / "timeline.json").is_file()


def test_six_section_short_plan_never_rounds_below_minimum() -> None:
    proposed = ProposedPlan.model_validate(
        {
            "title": "Short Practice",
            "intention": "Fit a valid short practice.",
            "sections": [
                {
                    "id": f"part-{index}",
                    "title": f"Part {index}",
                    "purpose": "Guide one brief step.",
                    "technique": "focused_attention",
                    "weight": 5 if index == 1 else 1,
                    "pause_seconds": 6,
                }
                for index in range(1, 7)
            ],
        }
    )

    plan = _allocate_plan(proposed, 60)

    assert min(section.target_speech_seconds for section in plan.sections) >= 8
    allocated_seconds = sum(section.target_speech_seconds for section in plan.sections) + (
        sum(section.pause_after_ms for section in plan.sections) // 1_000
    )
    assert allocated_seconds == pytest.approx(60, abs=1)


def test_twenty_minute_plan_redistributes_oversized_quiet_blocks() -> None:
    proposed = ProposedPlan.model_validate(
        {
            "title": "Long Practice",
            "intention": "Sustain a spacious reviewed practice.",
            "sections": [
                {
                    "id": f"part-{index}",
                    "title": f"Part {index}",
                    "purpose": "Guide one stage of the practice.",
                    "technique": "focused_attention",
                    "weight": 1,
                    "pause_intent": "extended" if index == 3 else "embodied",
                }
                for index in range(1, 7)
            ],
        }
    )

    plan = _allocate_plan(proposed, 20 * 60)

    pauses = [section.pause_after_ms for section in plan.sections]
    assert max(pauses) <= 120_000
    assert sum(pauses) == pytest.approx(round(20 * 60 * 0.32) * 1_000, abs=5)


def test_short_plan_keeps_requested_middle_section_between_arrival_and_return() -> None:
    proposed = ProposedPlan.model_validate(
        {
            "title": "Evening",
            "intention": "Reflect before sleep.",
            "sections": [
                {
                    "id": section_id,
                    "title": title,
                    "purpose": purpose,
                    "technique": (
                        "arrival"
                        if section_id == "arrive"
                        else "sleep_transition"
                        if section_id == "return"
                        else "reflection"
                        if section_id == "day"
                        else "body_scan"
                        if section_id == "body"
                        else "focused_attention"
                    ),
                    "weight": weight,
                    "pause_seconds": 10,
                }
                for section_id, title, purpose, weight in (
                    ("arrive", "Arrive", "Settle into a comfortable position.", 2),
                    ("breath", "Breath", "Notice natural breathing.", 5),
                    ("body", "Body", "Relax the body.", 4),
                    ("day", "Day reflection", "Reflect on memories from the day.", 2),
                    ("return", "Return", "Return attention to the room.", 1),
                )
            ],
        }
    )

    focused = _compact_plan_for_duration(
        proposed,
        prompt="A two minute meditation to reflect on my day before sleep.",
        duration_seconds=120,
    )

    assert [section.id for section in focused.sections] == ["arrive", "day", "return"]


def test_v2_plan_derives_kind_ending_delivery_and_deterministic_silence() -> None:
    proposed = ProposedPlan.model_validate(
        {
            "title": "Night Practice",
            "intention": "Settle toward sleep.",
            "sections": [
                {
                    "id": "arrive",
                    "title": "Arrive",
                    "purpose": "Feel the bed supporting the body.",
                    "technique": "arrival",
                    "weight": 1,
                    "pause_intent": "embodied",
                },
                {
                    "id": "rest",
                    "title": "Rest",
                    "purpose": "Rest with natural breathing.",
                    "technique": "resting_awareness",
                    "weight": 2,
                    "pause_intent": "extended",
                },
                {
                    "id": "sleep",
                    "title": "Sleep",
                    "purpose": "Taper into quiet rest.",
                    "technique": "sleep_transition",
                    "weight": 1,
                    "pause_intent": "extended",
                },
            ],
        }
    )

    plan = _allocate_plan(proposed, 600, prompt="A ten minute sleep meditation.")

    assert plan.schema_version == 2
    assert plan.meditation_kind == "sleep"
    assert plan.blueprint_id == "sleep@1"
    assert plan.ending_behavior == "sleep_taper"
    assert plan.sections[-1].delivery == "sleepy"
    assert plan.sections[1].pause_after_ms > plan.sections[0].pause_after_ms
    assert plan.silence_budget_seconds == pytest.approx(150, abs=1)


def test_allocate_plan_honors_explicit_reviewed_kind() -> None:
    proposed = ProposedPlan.model_validate_json(_plan())

    plan = _allocate_plan(
        proposed,
        180,
        prompt="A quiet practice",
        meditation_kind="loving_kindness",
    )

    assert plan.meditation_kind == "loving_kindness"
    assert plan.blueprint_id == "loving_kindness@1"


def test_duration_fitting_can_compress_sentence_rests_without_invalid_section_pauses() -> None:
    plan = _allocate_plan(ProposedPlan.model_validate_json(_plan()), 60)
    script = "# A Steady Moment\n\n" + "\n\n".join(
        f"[delivery: {section.delivery}]\n\n"
        "Notice the support.\n\n[pause: 2400ms]\n\n"
        "Return to the room.\n\n[pause: 6000ms]"
        for section in plan.sections
    )

    fitted_script, fitted_plan = _fit_script_to_requested_duration(
        script, plan, spoken_seconds=55, requested_seconds=60
    )
    timeline = build_script_timeline(
        run_id=RUN_ID,
        script=fitted_script,
        created_at=CREATED_AT,
        source="generated_prompt",
    )
    silence = [segment for segment in timeline.segments if isinstance(segment, SilenceSegment)]

    assert sum(segment.duration_ms for segment in silence) == 5_000
    assert any(segment.duration_ms < 1_000 for segment in silence)
    assert [segment.duration_ms for segment in silence[1::2]] == [
        section.pause_after_ms for section in fitted_plan.sections
    ]
    assert all(section.pause_after_ms >= 1_000 for section in fitted_plan.sections)
    assert type(plan).model_validate_json(fitted_plan.model_dump_json()) == fitted_plan


@pytest.mark.parametrize("explicit_kind", ["body_scan", "grounding", "breath_awareness"])
def test_explicit_daytime_kind_controls_validation_and_allocation(explicit_kind: str) -> None:
    prompt = "I struggle with sleep but want a daytime body scan"
    kind = cast(MeditationKind, explicit_kind)
    proposed = _validate_plan_for_request(json.loads(_plan()), prompt=prompt, meditation_kind=kind)
    plan = _allocate_plan(proposed, 60, prompt=prompt, meditation_kind=kind)

    assert plan.meditation_kind == kind
    assert plan.ending_behavior == "daytime_reorientation"
    assert plan.sections[-1].technique == "return"
    wrong_ending = json.loads(_plan())
    wrong_ending["sections"][-1]["technique"] = "sleep_transition"
    with pytest.raises(ValueError, match="final section technique must be 'return'"):
        _validate_plan_for_request(wrong_ending, prompt=prompt, meditation_kind=kind)


@pytest.mark.parametrize("prompt", ["Help me fall asleep", "A sleeping meditation", "Bedtime"])
def test_inferred_sleep_kind_agrees_with_plan_validation(prompt: str) -> None:
    value = json.loads(_plan())
    value["sections"][-1]["technique"] = "sleep_transition"
    proposed = _validate_plan_for_request(value, prompt=prompt)
    plan = _allocate_plan(proposed, 60, prompt=prompt)

    assert plan.meditation_kind == "sleep"
    assert plan.ending_behavior == "sleep_taper"


def test_plan_allocation_uses_installed_blueprints_outside_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_prefix = tmp_path / "installed"
    installed_config = installed_prefix / "share" / "whoopy" / "config"
    shutil.copytree(Path("config"), installed_config)
    monkeypatch.setattr("sys.prefix", str(installed_prefix))
    monkeypatch.chdir(tmp_path)

    plan = _allocate_plan(ProposedPlan.model_validate_json(_plan()), 60, prompt="grounding")

    assert plan.blueprint_id == "grounding@1"
    assert plan.ending_behavior == "daytime_reorientation"
