from __future__ import annotations

import json

import pytest

from tests.test_recipe_whole_session import FixtureDraft, WholeSessionAdapter, run
from whoopy.meditation import GenerationError
from whoopy.meditation.whole_session import RETURN_SOUND, _has_positive_return_cue

RETURN_ERROR = "return: include surrounding sound and an optional small movement"


def test_negated_movement_requires_revision_before_accepting_three_minute_draft() -> None:
    ending = "Do not move your fingers. Listen to the surrounding sounds."

    def mutate(output: FixtureDraft) -> None:
        output["sections"][-1]["paragraphs"][0] = ending

    adapter = WholeSessionAdapter(mutate)
    result = run(adapter, "Grounding with ordinary sounds and bodily contact", 180)

    assert len(adapter.requests) == 2
    error = result.raw_attempts[0].validation_error
    assert error is not None
    assert error.count(RETURN_ERROR) == 1
    assert "offer a positive optional movement of a named small body part" in error
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
        "Notice the sounds and the ground beneath you. Wiggle your toes if you wish. "
        "Bring the same awareness into the next moment.",
        "Listen to sounds. You may wiggle your toes.",
        "Hear the sounds. Move one finger if you wish.",
        "Notice again the sounds around you. Wiggle your toes if you wish.",
        "Do not listen to sounds. Instead, hear the sounds. Wiggle your toes.",
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


@pytest.mark.parametrize(
    "ending",
    [
        "Imagine hearing sounds. Wiggle your toes if you wish.",
        "Imagine listening to surrounding sounds. You may move your fingers.",
        "Remember hearing sounds. Wiggle your toes if you wish.",
        "Recall listening to sounds around you. You may move a finger.",
        "List the sounds: hear sounds. Wiggle your toes if you wish.",
        "Notice sounds from your memory. Wiggle your toes if you wish.",
        "Notice sounds you imagine. Wiggle your toes if you wish.",
        "Notice the sounds of an imagined forest. Move a finger if you wish.",
        "Do not notice the sounds. Wiggle your toes if you wish.",
        "Avoid listening to sounds and moving your toes. You may curl a finger.",
        "Do not hear sounds or move your toes. You may move a finger.",
        "Notice sound advice. Wiggle your toes if you wish.",
    ],
)
def test_bare_sound_language_must_be_actual_positive_perception(ending: str) -> None:
    def mutate(output: FixtureDraft) -> None:
        output["sections"][-1]["paragraphs"][0] = ending

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    with pytest.raises(GenerationError, match=RETURN_ERROR):
        run(adapter, "Grounding with ordinary sounds and bodily contact", 180)
    assert len(adapter.requests) == 2
    assert all(output["sections"][-1]["paragraphs"] == [ending] for output in adapter.outputs)


def test_orientation_accepts_present_sound_perception_without_a_fixed_adjective() -> None:
    orientation = "Notice the sounds and the support under your body."

    def mutate(output: FixtureDraft) -> None:
        output["sections"][1]["paragraphs"][0] = orientation

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    result = run(adapter, "Grounding with ordinary sounds and bodily contact", 180)
    assert len(adapter.requests) == 1
    assert result.sections[1].text == orientation


ATTENTION_ACTIONS = [
    "Turn your attention to",
    "Turn your awareness to",
    "Turn again to",
    "Turn back toward",
    "Bring your attention back to",
    "Bring awareness to",
    "Direct your attention to",
    "Direct your awareness toward",
    "Shift your attention to",
    "Shift awareness back to",
    "Return your focus to",
    "Be aware of",
]


@pytest.mark.parametrize("action", ATTENTION_ACTIONS)
@pytest.mark.parametrize("reference", ["sounds", "the sounds around you", "ordinary sounds"])
def test_directional_attention_family_establishes_present_perception(
    action: str, reference: str
) -> None:
    assert _has_positive_return_cue(f"{action} {reference}.", RETURN_SOUND)


@pytest.mark.parametrize("action", ATTENTION_ACTIONS)
@pytest.mark.parametrize(
    "context",
    [
        "Do not {action} the sounds.",
        "Imagine that you {action} the sounds.",
        "Remember how you {action} the sounds.",
        "Think about how you {action} the sounds.",
        "{action} imagined sounds.",
        "{action} the sounds from your memory.",
        "{action} the sounds of your heartbeat.",
    ],
)
def test_directional_attention_still_requires_actual_external_positive_perception(
    action: str, context: str
) -> None:
    assert not _has_positive_return_cue(context.format(action=action.lower()), RETURN_SOUND)


@pytest.mark.parametrize("action", ATTENTION_ACTIONS)
def test_separate_positive_directional_attention_survives_a_negated_clause(action: str) -> None:
    text = f"Do not {action.lower()} sounds. Instead, {action.lower()} sounds."
    assert _has_positive_return_cue(text, RETURN_SOUND)


@pytest.mark.parametrize(
    "text",
    [
        "Turn your body toward the sounds.",
        "Shift your weight toward sounds.",
        "Bring your imagined attention to sounds.",
        "Turn your attention away from sounds.",
        "Direct your attention to sound reasoning.",
    ],
)
def test_movement_or_modified_objects_do_not_supply_a_perception_cue(text: str) -> None:
    assert not _has_positive_return_cue(text, RETURN_SOUND)


def test_actual_0940_orientation_and_return_paraphrases_keep_their_exact_prose() -> None:
    orientation = (
        "Turn your awareness to the sounds near you—perhaps a fan, a distant voice, or stillness. "
        "Feel where your body rests against the seat."
    )
    ending = (
        "Turn again to the sounds around you and the surface beneath you. "
        "Move your fingers or toes gently. Carry this attention into whatever follows."
    )

    def mutate(output: FixtureDraft) -> None:
        output["sections"][1]["paragraphs"][0] = orientation
        output["sections"][-1]["paragraphs"][0] = ending

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    result = run(adapter, "Grounding with ordinary sounds and bodily contact", 180)
    assert len(adapter.requests) == 1
    assert result.sections[1].text == orientation
    assert result.sections[-1].text == ending
    # This tests gate semantics and exact preservation, not editorial quality:
    # the source examples and generic next-activity wording still need review.


NOTICING_ACTIONS = ["Notice", "Begin by noticing", "Start by noticing"]


@pytest.mark.parametrize("action", NOTICING_ACTIONS)
@pytest.mark.parametrize(
    "reference", ["whatever sounds are present", "what sounds are around you", "any sounds"]
)
def test_present_sound_noticing_accepts_bounded_indefinite_objects(
    action: str, reference: str
) -> None:
    assert _has_positive_return_cue(f"{action} {reference}.", RETURN_SOUND)


@pytest.mark.parametrize("action", NOTICING_ACTIONS)
@pytest.mark.parametrize(
    "context",
    [
        "Do not {action} whatever sounds are present.",
        "No need to {action} whatever sounds are present.",
        "Imagine that you {action} whatever sounds are present.",
        "Remember how you {action} whatever sounds are present.",
        "List how you {action} whatever sounds are present.",
        "{action} whatever imagined sounds are present.",
        "{action} whatever sounds you imagine.",
        "{action} whatever sounds are imagined.",
        "{action} whatever sounds are present only in your memory.",
        "{action} whatever sounds are in your mind.",
        "{action} whatever sounds of your heartbeat are present.",
    ],
)
def test_indefinite_sound_noticing_still_requires_actual_external_positive_perception(
    action: str, context: str
) -> None:
    assert not _has_positive_return_cue(context.format(action=action.lower()), RETURN_SOUND)


@pytest.mark.parametrize("action", ["beginning", "starting"])
def test_negated_starting_instruction_does_not_supply_sound_orientation(action: str) -> None:
    assert not _has_positive_return_cue(
        f"Avoid {action} by noticing whatever sounds are present.", RETURN_SOUND
    )


def test_independent_noticing_after_negation_remains_positive() -> None:
    assert _has_positive_return_cue(
        "Do not begin by noticing imagined sounds. "
        "Instead, start by noticing whatever sounds are present.",
        RETURN_SOUND,
    )


def test_diverse_sleep_orientation_is_preserved_as_actual_perception() -> None:
    orientation = (
        "Begin by noticing whatever sounds are present—soft clicks, distant voices, "
        "or even silence. "
        "They belong to this moment."
    )

    def mutate(output: FixtureDraft) -> None:
        output["sections"][1]["paragraphs"][0] = orientation
        output["sections"][1]["paragraphs"][1] = (
            "Turn your attention now to where your body rests on the mattress beneath you. "
            "Notice the surface supporting you."
        )

    adapter = WholeSessionAdapter(mutate, always_mutate=True)
    result = run(adapter, "Lying down before sleep without a body scan or reflection", 300)
    assert len(adapter.requests) == 1
    assert result.sections[1].text.split("\n\n")[0] == orientation
    # The perception cue is valid; invented source examples still need editorial review.
