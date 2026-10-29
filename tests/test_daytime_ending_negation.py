from __future__ import annotations

import json

import pytest

from tests.test_recipe_whole_session import FixtureDraft, WholeSessionAdapter, run
from whoopy.meditation import GenerationError

RETURN_ERROR = "return: include surrounding sound and an optional small movement"


def test_negated_movement_requires_revision_before_accepting_three_minute_draft() -> None:
    ending = "Do not move your fingers. Listen to the surrounding sounds."

    def mutate(output: FixtureDraft) -> None:
        output["sections"][-1]["paragraphs"][0] = ending

    adapter = WholeSessionAdapter(mutate)
    result = run(adapter, "Grounding with ordinary sounds and bodily contact", 180)

    assert len(adapter.requests) == 2
    assert result.raw_attempts[0].validation_error == RETURN_ERROR
    assert result.raw_attempts[0].text == json.dumps(adapter.outputs[0])
    assert adapter.outputs[0]["sections"][-1]["paragraphs"] == [ending]
    assert result.raw_attempts[-1].validation_error is None
    assert result.sections[-1].text == "\n\n".join(
        adapter.outputs[-1]["sections"][-1]["paragraphs"]
    )
    assert result.estimated_duration_seconds == pytest.approx(180, abs=0.02)


@pytest.mark.parametrize(
    "ending",
    [
        "Don't gently wiggle your toes. Listen to surrounding sounds.",
        "You don\u2019t have to lift your hands. Listen to surrounding sounds.",
        "You needn't stretch a little. Listen to surrounding sounds.",
        "No need to shift your weight. Listen to surrounding sounds.",
        "Not moving your fingers, listen to surrounding sounds.",
        "Avoid stretching your arms. Listen to surrounding sounds.",
        "Without moving your hands, listen to surrounding sounds.",
        "Do not listen to surrounding sounds. You may move your fingers gently.",
        "Do not hear the room. You may move your fingers gently.",
        "Do not notice surrounding sounds. You may lift an arm gently.",
        "Notice contact, do not listen to sounds. Move your hands gently.",
        "Do not listen to the surrounding sounds or move your hands.",
        "Do not move your hands or listen to the surrounding sounds.",
        "Do not listen to the surrounding sounds or move your hands. You may curl a toe.",
        "Do not move your hands or listen to surrounding sounds. Listen to nearby sounds.",
        "Do not move your hands and feet or listen to sounds. You may curl a toe.",
        "Do not listen to sounds or move your hands and feet. Listen to nearby sounds.",
    ],
)
def test_negated_daytime_invitations_do_not_satisfy_required_cues(ending: str) -> None:
    def mutate(output: FixtureDraft) -> None:
        output["sections"][-1]["paragraphs"][0] = ending

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(GenerationError, match=RETURN_ERROR):
        run(adapter, "Grounding with ordinary sounds and bodily contact", 180)

    assert len(adapter.requests) == 2
    assert RETURN_ERROR in adapter.requests[-1].prompt
    assert all(output["sections"][-1]["paragraphs"] == [ending] for output in adapter.outputs)


@pytest.mark.parametrize(
    "ending",
    [
        "Do not move your fingers, but you may move your hands. Listen to surrounding sounds.",
        "Do not listen to the room. Hear surrounding sounds and move your fingers gently.",
        "No need to move your hands or listen to sounds. "
        "You may lift an arm and hear nearby sounds.",
        "You may gently wiggle a finger if you wish. Listen to surrounding sounds.",
        "You do not need to move your fingers and you may shift your weight. "
        "Listen to surrounding sounds.",
        "Do not listen to sounds, then hear the room and move your hands gently.",
        "Notice your hands, do not move your fingers, but listen to surrounding sounds. "
        "You may lift an arm gently.",
        "Do not notice your hands, but listen to nearby sounds. You may move your fingers gently.",
        "Notice, if you wish, surrounding sounds. You may lift an arm gently.",
    ],
)
def test_separate_permissive_daytime_invitations_remain_positive(ending: str) -> None:
    def mutate(output: FixtureDraft) -> None:
        output["sections"][-1]["paragraphs"][0] = ending

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    result = run(adapter, "Grounding with ordinary sounds and bodily contact", 180)

    assert len(adapter.requests) == 1
    assert result.raw_attempts[0].validation_error is None
    assert result.sections[-1].text == ending
    assert ending in result.script
