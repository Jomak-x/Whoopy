"""Preference routing and writer contracts; fixture prose is not quality evidence."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.test_recipe_whole_session import (
    RUN_ID,
    FixtureDraft,
    WholeSessionAdapter,
    generator,
    run,
)
from whoopy.meditation import GenerationError
from whoopy.meditation.models import MeditationKind
from whoopy.meditation.recipes import Anchor, route_session
from whoopy.meditation.whole_session import _prepared_for_quiet


@pytest.mark.parametrize(
    ("prompt", "anchor", "excluded"),
    [
        ("No breathing; use contact", "contact", ["natural_breath"]),
        ("No breath or sound; use contact", "contact", ["natural_breath", "sound"]),
        ("No sound, no breathing. Help me focus", "contact", ["natural_breath", "sound"]),
        ("No sound; help me focus on natural breathing", "natural_breath", ["sound"]),
        ("Do not listen to sounds; use contact", "contact", ["sound"]),
        ("I cannot hear. Use contact", "contact", ["sound"]),
        ("Avoid noise and breathing. Help me settle", "contact", ["natural_breath", "sound"]),
        ("Sound is overwhelming. Use contact", "contact", ["sound"]),
        ("No contact; use sound", "sound", ["contact"]),
        ("Use sound, not breathing", "sound", ["natural_breath"]),
        ("No box breathing; use natural breathing", "natural_breath", []),
        ("No sound, but use contact", "contact", ["sound"]),
        ("No sound and use contact", "contact", ["sound"]),
    ],
)
def test_affirmative_anchor_precedes_defaults_and_exclusions(
    prompt: str, anchor: Anchor, excluded: list[Anchor]
) -> None:
    session = route_session(prompt, 180)
    assert session.anchor == anchor
    assert session.excluded_anchors == excluded
    assert session.breath_excluded == ("natural_breath" in excluded)


def test_no_supported_anchor_fails_before_generation() -> None:
    adapter = WholeSessionAdapter()
    with pytest.raises(GenerationError, match="All supported anchors are excluded"):
        run(adapter, "No sound, no contact, no breathing")
    assert adapter.requests == []


@pytest.mark.parametrize(
    ("prompt", "recipe_id"),
    [
        ("Good night after a busy day", "sleep-settling"),
        ("A sleep meditation, no reflection", "sleep-settling"),
        ("Help me sleep without reviewing the day", "sleep-settling"),
        ("A good-night reflection", "good-night-reflection"),
        ("Help me sleep; no body scan", "sleep-anchor-rest"),
        ("Help me sleep; avoid body scanning", "sleep-anchor-rest"),
        ("Reflect on my day before sleep; no body scan", "good-night-anchor-reflection"),
        ("Help me sleep; no body scan or reflection", "sleep-anchor-rest"),
    ],
)
def test_sleep_uses_only_requested_reflection_and_permitted_practices(
    prompt: str, recipe_id: str
) -> None:
    session = route_session(prompt, 300)
    assert session.recipe.id == recipe_id
    assert session.recipe.ending == "sleep_taper"
    if session.body_scan_excluded:
        assert all(beat.technique != "body_scan" for beat in session.recipe.beats)


@pytest.mark.parametrize(
    ("kind", "prompt"),
    [
        ("body_scan", "No body scan"),
        ("reflection", "Do not ask me to reflect"),
        ("visualization", "No imagery"),
    ],
)
def test_explicit_kind_cannot_override_an_exclusion(kind: MeditationKind, prompt: str) -> None:
    with pytest.raises(ValueError, match="excluded"):
        route_session(prompt, 180, meditation_kind=kind)


def _contact_fixture(output: FixtureDraft) -> None:
    for section in output["sections"]:
        section["paragraphs"] = [
            re.sub(
                r"\b(?:sound|sounds)\b",
                "contact",
                re.sub(r"\b(?:Hear|Listen)\b", "Notice", text),
            )
            for text in section["paragraphs"]
        ]


def test_sound_exclusion_applies_to_orientation_and_ending() -> None:
    adapter = WholeSessionAdapter(_contact_fixture, always_mutate=True)
    result = run(adapter, "No sound or breathing; use contact before a meeting", 180)
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    assert contract["excluded_anchors"] == ["natural_breath", "sound"]
    orientation = next(b for b in contract["beats_in_order"] if b["section_id"] == "orientation")
    assert orientation["required_content"] == ["A concrete point of bodily contact"]
    assert "sound" not in result.script.casefold()
    assert len(adapter.requests) == 1


def test_sound_exclusion_rejects_generated_sound_cues_after_holistic_retry() -> None:
    adapter = WholeSessionAdapter()
    with pytest.raises(GenerationError, match="sound attention is excluded"):
        run(adapter, "No sound or breathing; use contact before a meeting", 180)
    assert len(adapter.requests) == 2
    assert "sound attention is excluded" in adapter.requests[1].prompt


def test_sleep_without_body_scan_uses_anchor_practice_and_retains_taper() -> None:
    adapter = WholeSessionAdapter()
    result = run(adapter, "Help me sleep; no body scan and no reflection", 300)
    assert result.plan.blueprint_id == "sleep-anchor-rest@1"
    assert result.plan.ending_behavior == "sleep_taper"
    assert all(section.technique != "body_scan" for section in result.plan.sections)
    assert "?" not in result.script
    assert len(adapter.requests) == 1


@pytest.mark.parametrize(
    ("prompt", "instruction", "failure"),
    [
        (
            "Help me sleep; no body scan",
            "Scan your body from head to toe, noticing support.",
            "body scanning is excluded",
        ),
        (
            "Help me focus; no imagery",
            "Imagine a mountain path and notice the trees around you.",
            "imagery is excluded",
        ),
    ],
)
def test_generated_excluded_practice_is_rejected(
    prompt: str, instruction: str, failure: str
) -> None:
    def mutate(output: FixtureDraft) -> None:
        practice = next(
            section for section in output["sections"] if section["section_id"] == "practice"
        )
        practice["paragraphs"][0] = instruction

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(GenerationError, match=failure):
        run(adapter, prompt, 300)
    assert len(adapter.requests) == 2


@pytest.mark.parametrize(
    ("kind", "technique", "progression"),
    [
        ("visualization", "visualization", "Release the scene"),
        ("open_awareness", "resting_awareness", "broaden to permitted experiences"),
        ("body_scan", "body_scan", "ordered sequence"),
        ("noting", "noting", "without analysis"),
        ("self_compassion", "loving_kindness", "optional kindness"),
    ],
)
def test_selected_technique_has_compatible_contract_and_plan_metadata(
    kind: MeditationKind, technique: str, progression: str
) -> None:
    adapter = WholeSessionAdapter()
    result = generator(adapter).generate(
        prompt="A quiet seated practice with sound",
        duration_seconds=300,
        meditation_kind=kind,
        run_id=RUN_ID,
        writing_method="recipe_whole_session",
    )
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    assert progression in contract["practice_progression"]
    practice = next(section for section in result.plan.sections if section.id == "practice")
    assert practice.technique == technique
    assert contract["imagery"] == (
        "one optional original imagined scene"
        if kind == "visualization"
        else "not selected; use actual experience"
    )


def test_unrequested_imagery_is_absent_from_grounding_contract() -> None:
    adapter = WholeSessionAdapter()
    run(adapter, "Grounding after an overstimulating day", 180)
    contract = json.JSONDecoder().raw_decode(adapter.requests[0].prompt.split("\n", 1)[1])[0]
    assert contract["practice_kind"] == "grounding"
    assert contract["imagery"] == "not selected; use actual experience"
    system_prompt = adapter.requests[0].system_prompt
    assert system_prompt is not None
    assert "Otherwise, do not introduce imaginary scenes or sounds" in system_prompt


@pytest.mark.parametrize(
    "prompt",
    [
        "I keep imagining mistakes before my meeting; help me focus",
        "My mind tells me a story about failure; help me settle",
        "I feel pressure before a meeting",
    ],
)
def test_context_does_not_request_imagery_or_a_pressure_anchor(prompt: str) -> None:
    session = route_session(prompt, 180)
    assert session.kind != "visualization"
    assert session.anchor == "natural_breath"


@pytest.mark.parametrize(
    "prompt",
    ["A guided visualization", "Help me imagine a quiet place", "Please visualize a garden"],
)
def test_explicit_visualization_request_selects_the_existing_kind(prompt: str) -> None:
    assert route_session(prompt, 180).kind == "visualization"


@pytest.mark.parametrize(
    ("kind", "instruction", "prepared"),
    [
        ("visualization", "If you wish, imagine a simple garden for a moment.", True),
        ("grounding", "If you wish, imagine a simple garden for a moment.", False),
        ("self_compassion", "You may repeat those words of kindness in your own time.", True),
        ("loving_kindness", "If you wish, offer the phrase again in your own time.", True),
        ("grounding", "You may repeat those words of kindness in your own time.", False),
    ],
)
def test_quiet_invitation_matches_the_selected_practice(
    kind: MeditationKind, instruction: str, prepared: bool
) -> None:
    assert _prepared_for_quiet("practice", kind, instruction) is prepared


def test_checked_in_quality_corpus_routes_the_requested_practices() -> None:
    corpus = json.loads(Path("config/evaluation/generation_quality_2026_11.json").read_text())
    expected = {
        "meeting-grounding": ("contact", ["natural_breath"], False, "daytime-steadying"),
        "contact-only-grounding": (
            "contact",
            ["natural_breath", "sound"],
            True,
            "daytime-steadying",
        ),
        "sleep-without-scan": ("contact", [], True, "sleep-anchor-rest"),
        "loving-kindness": ("natural_breath", [], False, "daytime-steadying"),
        "body-scan": ("contact", [], False, "daytime-steadying"),
        "breath-observation": ("natural_breath", [], False, "daytime-steadying"),
    }
    assert {case["id"] for case in corpus["cases"]} == set(expected)
    for case in corpus["cases"]:
        session = route_session(
            case["prompt"], case["minutes"] * 60, meditation_kind=case["meditation_kind"]
        )
        assert session.kind == case["meditation_kind"]
        assert (
            session.anchor,
            session.excluded_anchors,
            session.body_scan_excluded,
            session.recipe.id,
        ) == expected[case["id"]], case["id"]


@pytest.mark.parametrize(
    ("prompt", "excluded"),
    [
        ("Observe natural breathing. No breath holds or paced breathing.", False),
        ("Observe natural breathing. Do not prescribe a breathing rhythm or count.", False),
        ("Observe natural breathing. Do not guide breath attention or prescribe a rhythm.", True),
        ("No sound, listening, hearing, breath, breathing, or contact.", True),
    ],
)
def test_breath_modification_exclusion_does_not_erase_attention_preferences(
    prompt: str, excluded: bool
) -> None:
    if "or contact" in prompt:
        with pytest.raises(ValueError, match="All supported anchors are excluded"):
            route_session(prompt, 180)
    else:
        session = route_session(prompt, 180)
        assert session.breath_excluded is excluded
        assert (session.anchor == "natural_breath") is not excluded
