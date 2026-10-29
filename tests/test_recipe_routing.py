from __future__ import annotations

from typing import get_args

import pytest

from whoopy.meditation.models import MeditationKind
from whoopy.meditation.recipes import allocate_slots, route_session


@pytest.mark.parametrize("kind", get_args(MeditationKind))
@pytest.mark.parametrize(
    "prompt",
    [
        "Anxiety before an important meeting",
        "Reflect on a busy day with self-compassion before sleep",
        "A body scan for focus after a setback",
        "Help me with sleeping",
    ],
)
def test_explicit_kind_controls_practice_despite_request_keywords(
    kind: MeditationKind, prompt: str
) -> None:
    requested = route_session(prompt, 180, meditation_kind=kind)
    neutral = route_session("A quiet moment", 180, meditation_kind=kind)

    assert requested.kind == kind
    assert requested.main_practice == neutral.main_practice


@pytest.mark.parametrize(
    ("prompt", "kind"),
    [
        ("Reflect on my day", "reflection"),
        ("Support after a setback", "self_compassion"),
        ("Anxiety before an important meeting", "noting"),
        ("Help me focus", "focused_attention"),
        ("A body scan", "body_scan"),
        ("A quiet moment", "grounding"),
        ("Help me sleep", "sleep"),
        ("Help me with sleeping", "sleep"),
    ],
)
def test_request_keywords_select_kind_when_no_explicit_kind(
    prompt: str, kind: MeditationKind
) -> None:
    assert route_session(prompt, 180).kind == kind


def test_explicit_practice_preserves_external_anchor_constraint() -> None:
    session = route_session(
        "Anxiety before an important meeting, without breathing exercises",
        180,
        meditation_kind="body_scan",
    )

    assert session.kind == "body_scan"
    assert session.anchor == "sound"
    assert session.breath_excluded


@pytest.mark.parametrize(
    "prompt", ["Help me with sleeping", "Help me sleep", "Good\tnight", "I have insomnia"]
)
def test_sleep_wording_routes_posture_and_ending(prompt: str) -> None:
    session = route_session(prompt, 60)

    assert session.kind == "sleep"
    assert session.posture == "lying_down"
    assert session.recipe.id == "sleep-settling"
    assert session.recipe.ending == "sleep_taper"


def test_explicit_daytime_kind_overrides_sleeping_wording() -> None:
    session = route_session("Help me with sleeping", 60, meditation_kind="body_scan")

    assert session.kind == "body_scan"
    assert session.posture == "sitting"
    assert session.recipe.ending == "daytime_reorientation"


def test_explicitly_staying_awake_does_not_route_to_sleep() -> None:
    session = route_session("Keep me awake; do not fall asleep during my meeting", 180)

    assert session.kind == "focused_attention"
    assert session.posture == "sitting"
    assert session.recipe.id == "daytime-steadying"
    assert session.recipe.ending == "daytime_reorientation"


def test_negated_box_breathing_request_routes_natural_breathing() -> None:
    session = route_session("No box breathing; use natural breathing", 180)

    assert session.anchor == "natural_breath"
    assert not session.breath_excluded


@pytest.mark.parametrize(
    "prompt",
    [
        "No box breathing; help me sleep",
        "No box breathing, but help me sleep",
        "No box breathing but help me sleep",
    ],
)
def test_negated_box_breathing_clause_preserves_separate_sleep_request(prompt: str) -> None:
    session = route_session(prompt, 180)

    assert session.kind == "sleep"
    assert session.recipe.ending == "sleep_taper"


@pytest.mark.parametrize(
    "prompt",
    [
        "No sleep; use box breathing",
        "No sleep, but use box breathing",
        "No sleep but use box breathing",
    ],
)
def test_negated_sleep_clause_preserves_separate_box_breathing_rejection(prompt: str) -> None:
    with pytest.raises(ValueError, match="Box breathing is not supported"):
        route_session(prompt, 180)


@pytest.mark.parametrize("words_per_minute", [80, 133, 220])
@pytest.mark.parametrize("duration", [60, 180, 300, 1800])
@pytest.mark.parametrize("need", ["Seated focus with sound", "Good-night reflection in bed"])
def test_guidance_cadence_preserves_time_and_bounded_quiet(
    words_per_minute: int, duration: int, need: str
) -> None:
    session = route_session(need, duration)
    beats = allocate_slots(session, duration, words_per_minute)

    assert sum(slot.duration_ms for beat in beats for slot in beat) == duration * 1_000
    for index, beat in enumerate(beats):
        expected_quiet = (
            22_000 if session.recipe.ending == "sleep_taper" and index >= len(beats) - 2 else 16_000
        )
        for slot in beat:
            assert 0 < slot.duration_ms <= 35_000
            assert slot.maximum_quiet_ms == expected_quiet
            # Splitting a beat keeps the required guidance near 24 words or
            # fewer; integer millisecond/word rounding can add one word.
            assert 4 <= slot.minimum_words <= 25
            assert slot.minimum_words <= slot.maximum_words


def test_faster_speech_adds_brief_practice_cycles_without_extra_quiet_per_cycle() -> None:
    session = route_session("Good-night reflection in bed", 300)
    slower = allocate_slots(session, 300, 80)
    faster = allocate_slots(session, 300, 220)

    assert sum(map(len, faster)) > sum(map(len, slower))
    assert all(
        fast[0].maximum_quiet_ms == slow[0].maximum_quiet_ms
        for fast, slow in zip(faster, slower, strict=True)
    )
