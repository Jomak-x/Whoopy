"""Behavioral regressions for daytime external-sound reorientation."""

from __future__ import annotations

import pytest

from tests.test_recipe_whole_session import FixtureDraft, WholeSessionAdapter, run
from whoopy.meditation import GenerationError


def _replace_return(output: FixtureDraft, ending: str) -> None:
    section = next(item for item in output["sections"] if item["section_id"] == "return")
    section["paragraphs"][0] = ending


@pytest.mark.parametrize(
    "ending",
    [
        "Listen to your heartbeat. You may move your fingers gently.",
        "Hear your breathing. You may move your fingers gently.",
        "Listen to the sound of your heartbeat. You may move your fingers gently.",
        "Hear the sounds of your breathing. You may move your fingers gently.",
        "Notice the sound of your heartbeat. You may move your fingers gently.",
        "Notice breathing sounds. You may move your fingers gently.",
        "Listen to the sound of each breath. You may move your fingers gently.",
        "Listen to the sounds of every breath. You may move your fingers gently.",
        "Notice the sound of one heartbeat. You may move your fingers gently.",
        "Hear the sound of breathing in the room. You may move your fingers gently.",
        "Notice your breathing sounds around you. You may move your fingers gently.",
        "Listen to sounds. You may move your fingers gently.",
        "Notice an ordinary sound. You may move your fingers gently.",
    ],
)
def test_daytime_return_requires_external_listening_as_reorientation(ending: str) -> None:
    adapter = WholeSessionAdapter(
        lambda output: _replace_return(output, ending), always_mutate=True
    )

    with pytest.raises(
        GenerationError, match="include surrounding sound and an optional small movement"
    ):
        run(adapter, "Grounding before my meeting", 180)

    assert len(adapter.requests) == 2
    assert "include surrounding sound and an optional small movement" in adapter.requests[1].prompt


@pytest.mark.parametrize(
    "ending",
    [
        "Listen around you. You may move your fingers gently.",
        "Listen to surrounding sounds. You may move your fingers gently.",
        "Listen to the room. You may move your fingers gently.",
        "Hear the room around you. You may move your fingers gently.",
        "Listen to the ordinary sounds around you. You may move your fingers gently.",
        "Notice the sounds from your surroundings. You may move your fingers gently.",
        "Attend to ambient sounds. You may move your fingers gently.",
        "Become aware of sounds in the room. You may move your fingers gently.",
        "Listen to the sounds outside the room. You may move your fingers gently.",
        "Return to nearby sounds. You may move your fingers gently.",
        "Notice, if you wish, surrounding sounds. You may move your fingers gently.",
    ],
)
def test_daytime_return_accepts_explicit_external_sound_reorientation(ending: str) -> None:
    adapter = WholeSessionAdapter(
        lambda output: _replace_return(output, ending), always_mutate=True
    )

    result = run(adapter, "Grounding before my meeting", 180)

    assert len(adapter.requests) == 1
    assert result.raw_attempts[0].validation_error is None
    assert result.sections[-1].text.startswith(ending)


@pytest.mark.parametrize(
    "ending",
    [
        "Do not listen to the room. You may move your fingers gently.",
        "Do not notice the sounds around you. You may move your fingers gently.",
        "Avoid listening to ambient sounds. You may move your fingers gently.",
        "No need to hear the sounds in the room. You may move your fingers gently.",
    ],
)
def test_daytime_return_rejects_negated_external_sound_with_permissive_movement(
    ending: str,
) -> None:
    adapter = WholeSessionAdapter(
        lambda output: _replace_return(output, ending), always_mutate=True
    )

    with pytest.raises(
        GenerationError, match="include surrounding sound and an optional small movement"
    ):
        run(adapter, "Grounding before my meeting", 180)

    assert len(adapter.requests) == 2
    assert "include surrounding sound and an optional small movement" in adapter.requests[1].prompt
