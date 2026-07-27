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
from whoopy.meditation.prompts import PromptLoadError
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
                    "weight": 1,
                    "pause_seconds": 3,
                },
                {
                    "id": "notice",
                    "title": "Notice",
                    "purpose": "Notice simple body sensations.",
                    "weight": 1,
                    "pause_seconds": 3,
                },
                {
                    "id": "return",
                    "title": "Return",
                    "purpose": "Return attention to the room.",
                    "weight": 1,
                    "pause_seconds": 3,
                },
            ],
        }
    )


def _section(section_id: str, word: str) -> str:
    text = " ".join([word] * 46)
    return json.dumps({"section_id": section_id, "text": text})


def test_prompt_bundle_loads_reviewable_versions() -> None:
    prompts = load_prompt_bundle(Path("config/prompts"))

    assert prompts.plan.prompt_id == "whoopy.plan"
    assert prompts.plan.version == 1
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
    assert len(result.timeline.segments) == 6
    assert (
        sum(
            segment.duration_ms
            for segment in result.timeline.segments
            if isinstance(segment, SilenceSegment)
        )
        == 9_000
    )
    assert result.estimated_duration_seconds == pytest.approx(59.1818, rel=0.001)


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
