from __future__ import annotations

import pytest

from tests.test_recipe_whole_session import FixtureDraft, WholeSessionAdapter, run
from whoopy.meditation import GenerationError
from whoopy.meditation.safety import ContentSafetyError, validate_meditation_text


@pytest.mark.parametrize(
    "text",
    [
        "You are in a safe space where you can be as still or as mobile as you need to be.",
        "You're in a safe place now.",
        "You are in a completely safe room.",
        "This place is safe.",
        "This is a safe space.",
        "You will be completely safe.",
        "Nothing can harm you.",
        "Imagine a garden. You are in a safe place now.",
        "If your mind wanders, you are safe.",
        "Do not claim you are safe; nothing can harm you.",
        "Imagine a room, but you are safe here in reality.",
    ],
)
def test_actual_safety_assertions_are_rejected(text: str) -> None:
    with pytest.raises(ContentSafetyError, match="do not assert the listener is safe"):
        validate_meditation_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "If you wish, imagine a safe place.",
        "Imagine that you are in a safe place in a story.",
        "Picture a scene where you are safe.",
        "If you are in a safe place, you may choose to pause.",
        "When you are safe, choose whether to continue.",
        "If you feel that you are safe, you may pause.",
        "Do not claim you are safe.",
        "Don't claim that you are in a safe space.",
        "Never say 'nothing can harm you'.",
        "I cannot promise that you are safe.",
        "You do not have to feel safe or calm.",
        "Choose a place that feels comfortable to you.",
    ],
)
def test_hypothetical_or_negated_safety_language_is_not_an_actual_claim(text: str) -> None:
    validate_meditation_text(text)


def test_v7_safe_space_failure_receives_the_single_holistic_revision() -> None:
    def mutate(output: FixtureDraft) -> None:
        output["sections"][0]["paragraphs"][0] = (
            "You are in a safe space. " + output["sections"][0]["paragraphs"][0]
        )

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(GenerationError, match="do not assert the listener is safe"):
        run(adapter, "Grounding with ordinary sounds", 180)
    assert len(adapter.requests) == 2
    assert "comfort paragraph 1: do not assert the listener is safe" in adapter.requests[1].prompt
    assert "You are in a safe space." in adapter.requests[1].prompt
