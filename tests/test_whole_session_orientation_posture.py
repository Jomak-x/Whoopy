"""Whole-session posture and external-orientation regression coverage."""

from __future__ import annotations

import pytest

from tests.test_recipe_whole_session import FixtureDraft, WholeSessionAdapter, run
from whoopy.meditation import GenerationError


def _replace_section(output: FixtureDraft, section_id: str, paragraph: str) -> None:
    section = next(item for item in output["sections"] if item["section_id"] == section_id)
    section["paragraphs"][0] = paragraph


@pytest.mark.parametrize(
    ("need", "comfort"),
    [
        (
            "Sitting on a pillow before my meeting",
            "Sit on your pillow and feel its support. You may adjust your position.",
        ),
        (
            "Sitting on the edge of my bed before my meeting",
            "Sit on the edge of the bed and notice support. You may adjust your position.",
        ),
        (
            "Sitting on my mattress before my meeting",
            "Sit on the mattress and notice contact. You may adjust your position.",
        ),
        (
            "Seated focus before my meeting",
            "Rest against a pillow while sitting. You may adjust and notice support.",
        ),
    ],
)
def test_seated_furniture_support_preserves_authored_draft(need: str, comfort: str) -> None:
    adapter = WholeSessionAdapter(
        lambda output: _replace_section(output, "comfort", comfort), always_mutate=True
    )

    result = run(adapter, need, 180)

    assert result.plan.posture == "sitting"
    assert len(adapter.requests) == 1
    assert result.raw_attempts[0].validation_error is None
    assert result.sections[0].text == comfort
    assert comfort in result.script


@pytest.mark.parametrize(
    ("need", "posture", "instruction"),
    [
        ("Seated focus", "sitting", "Do not lie down."),
        ("Seated focus", "sitting", "You don't need to lie down."),
        ("Seated focus", "sitting", "No need to lie down or recline."),
        ("Seated focus", "sitting", "Avoid lying on your back."),
        ("Seated focus", "sitting", "You need not recline."),
        ("Seated focus", "sitting", "You are not lying down."),
        ("Seated focus", "sitting", "There is no need to be lying down."),
        ("Lying down before sleep", "lying_down", "Do not sit upright."),
        ("Lying down before sleep", "lying_down", "You don't need to straighten your spine."),
        ("Lying down before sleep", "lying_down", "No need to sit tall or sit up."),
    ],
)
def test_negated_incompatible_posture_does_not_exhaust_revision(
    need: str, posture: str, instruction: str
) -> None:
    comfort = f"{instruction} You may adjust your position and notice support."
    adapter = WholeSessionAdapter(
        lambda output: _replace_section(output, "comfort", comfort), always_mutate=True
    )

    result = run(adapter, need, 180)

    assert result.plan.posture == posture
    assert len(adapter.requests) == 1
    assert result.raw_attempts[0].validation_error is None
    assert result.sections[0].text == comfort


@pytest.mark.parametrize(
    "instruction",
    [
        "Lie down.",
        "Lie on your back.",
        "Lie on the mattress.",
        "Recline.",
        "Rest flat on your back.",
        "Do not lie down, but recline.",
        "You don't need to lie down. Now lie on your side.",
    ],
)
def test_seated_session_rejects_affirmative_lying_guidance(instruction: str) -> None:
    comfort = f"{instruction} You may adjust your position and notice support."
    adapter = WholeSessionAdapter(
        lambda output: _replace_section(output, "comfort", comfort), always_mutate=True
    )

    with pytest.raises(GenerationError, match="posture contradicts sitting"):
        run(adapter, "Seated focus", 180)

    assert len(adapter.requests) == 2
    assert "posture contradicts sitting" in adapter.requests[1].prompt


@pytest.mark.parametrize(
    "listening",
    [
        "Listen to the sound of each breath",
        "Hear the sounds of every breath",
        "Notice the sound of your heartbeat",
        "Listen to your heartbeat",
        "Hear the sound of breathing in the room",
        "Do not listen to the room",
        "Avoid listening to ambient sounds",
        "No need to hear the sounds around you",
    ],
)
def test_orientation_requires_affirmative_external_sound(listening: str) -> None:
    orientation = f"{listening} and feel contact with the chair."
    adapter = WholeSessionAdapter(
        lambda output: _replace_section(output, "orientation", orientation), always_mutate=True
    )

    with pytest.raises(
        GenerationError, match="orientation: establish surrounding sound before inward practice"
    ):
        run(adapter, "Seated focus with natural breathing", 180)

    assert len(adapter.requests) == 2
    assert (
        "orientation: establish surrounding sound before inward practice"
        in adapter.requests[1].prompt
    )


@pytest.mark.parametrize(
    "listening",
    [
        "Listen to surrounding sounds",
        "Hear the room around you",
        "Notice, if you wish, the sounds outside the room",
        "Attend to ambient sounds",
        "Listen to each breath, then listen to the room",
        "Do not listen to your heartbeat; instead notice surrounding sounds",
        "No need to listen to the room. You may notice nearby sounds",
    ],
)
def test_orientation_accepts_external_sound_without_rewriting(listening: str) -> None:
    orientation = f"{listening} and feel contact with the chair."
    adapter = WholeSessionAdapter(
        lambda output: _replace_section(output, "orientation", orientation), always_mutate=True
    )

    result = run(adapter, "Seated focus with natural breathing", 180)

    assert result.plan.anchor == "natural_breath"
    assert len(adapter.requests) == 1
    assert result.raw_attempts[0].validation_error is None
    assert result.sections[1].text == orientation
    assert orientation in result.script
