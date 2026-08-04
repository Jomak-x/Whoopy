from __future__ import annotations

import json
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.meditation import (
    GenerationError,
    GenerationWorkspace,
    LocalMeditationGenerator,
    load_prompt_bundle,
)
from whoopy.meditation.generator import _allocate_plan, _compact_plan_for_duration
from whoopy.meditation.models import ProposedPlan
from whoopy.meditation.prompts import PromptLoadError
from whoopy.meditation.safety import ContentSafetyError, validate_meditation_text
from whoopy.ports import (
    AdapterMetadata,
    ScriptGenerationRequest,
    ScriptGenerationResult,
)
from whoopy.timeline import SilenceSegment

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
        "Notice the steady support beneath you. "
        "Let this simple experience be enough for now."
    )
    return json.dumps({"section_id": section_id, "text": text})


def test_prompt_bundle_loads_reviewable_versions() -> None:
    prompts = load_prompt_bundle(Path("config/prompts"))

    assert prompts.plan.prompt_id == "whoopy.plan"
    assert prompts.plan.version == 4
    assert prompts.section.version == 3
    assert "Return exactly one JSON object" in prompts.section.text


def test_prompt_without_front_matter_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "plan_system.md").write_text("missing metadata", encoding="utf-8")

    with pytest.raises(PromptLoadError, match="front matter"):
        load_prompt_bundle(tmp_path)


def test_plan_first_generation_produces_valid_script_and_timeline() -> None:
    adapter = SequenceGenerator(
        [
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
    )

    assert [request.seed for request in adapter.requests] == [42, 142, 152, 162]
    assert [section.section_id for section in result.sections] == [
        "arrive",
        "notice",
        "return",
    ]
    assert result.script.startswith("# A Steady Moment")
    assert len(result.timeline.segments) == 16
    assert (
        sum(
            segment.duration_ms
            for segment in result.timeline.segments
            if isinstance(segment, SilenceSegment)
        )
        == 30_000
    )
    assert result.estimated_duration_seconds == pytest.approx(59.2683, rel=0.001)


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
