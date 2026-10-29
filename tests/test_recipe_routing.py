from __future__ import annotations

from typing import get_args

import pytest

from whoopy.meditation.models import MeditationKind
from whoopy.meditation.recipes import Posture, allocate_slots, route_session


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


@pytest.mark.parametrize(
    "prompt",
    [
        "I couldn't sleep; please help me drift off now.",
        "I couldnt sleep; please help me drift off now.",
        "I couldn’t sleep; please help me drift off now.",
        "I could not sleep; please help me drift off now.",
        "I could  not sleep; please help me drift off now.",
        "I could\tnot sleep; please help me drift off now.",
        "I can't sleep; please help me drift off now.",
        "I cannot sleep; please help me drift off now.",
        "I did not sleep last night; help me rest now.",
        "I wasn't sleeping last night; help me rest now.",
        "I was not sleeping last night; help me rest now.",
        "No breath focus and I could not sleep; help me rest now.",
    ],
)
def test_inability_or_history_of_sleep_is_not_an_exclusion(prompt: str) -> None:
    session = route_session(prompt, 180)

    assert session.kind == "sleep"
    assert session.posture == "lying_down"
    assert session.recipe.ending == "sleep_taper"
    if prompt.startswith("No breath focus"):
        assert session.breath_excluded
        assert session.anchor == "sound"


def test_explicit_daytime_kind_overrides_sleeping_wording() -> None:
    session = route_session("Help me with sleeping", 60, meditation_kind="body_scan")

    assert session.kind == "body_scan"
    assert session.posture == "sitting"
    assert session.recipe.ending == "daytime_reorientation"


@pytest.mark.parametrize(
    ("prompt", "posture"),
    [
        ("Help me focus while sitting in a chair, not lying in bed", "sitting"),
        ("Help me focus while sitting in a chair not lying in bed", "sitting"),
        ("Help me focus while sitting, not while lying down", "sitting"),
        ("Help me focus while sitting on the floor, not in a chair", "sitting"),
        ("Help me focus while sitting on the floor not in a chair", "sitting"),
        ("Not in a chair; help me focus while sitting on the floor", "sitting"),
        ("Help me sleep while sitting on the floor, not in a chair", "sitting"),
        ("Help me focus while lying on the floor, not in bed", "lying_down"),
        ("Help me focus while lying on the floor not in bed", "lying_down"),
        ("Not in bed; help me focus while lying on the floor", "lying_down"),
        ("Help me sleep while lying on the floor, not in bed", "lying_down"),
        ("Help me sleep, not lying; I prefer sitting on the floor", "sitting"),
        ("Help me sleep, not lying on the floor", "sitting"),
        ("Help me sleep lying in bed, not sitting in a chair", "lying_down"),
        ("Help me sleep lying in bed not sitting in a chair", "lying_down"),
        ("Do not lie down; help me sleep while seated", "sitting"),
        ("Don't sit in a chair; guide me lying down", "lying_down"),
        ("Don’t lie down and I want to sit in a chair", "sitting"),
        ("Avoid sitting in a chair, but help me rest lying down", "lying_down"),
        ("I do not want to lie down; help me focus", "sitting"),
        ("I prefer not to lie down; help me focus", "sitting"),
        ("No reclining; help me sleep", "sitting"),
        ("Help me focus without sitting", "lying_down"),
        ("I cannot sit; help me rest", "lying_down"),
        ("No breath focus and help me sleep while sitting", "sitting"),
        ("Do not fall asleep while sitting in a chair", "sitting"),
        ("Help me sleep", "lying_down"),
        ("Help me focus", "sitting"),
        ("Help me focus lying down", "lying_down"),
        ("Help me sleep while sitting", "sitting"),
    ],
)
def test_posture_routing_honors_explicit_exclusions(prompt: str, posture: Posture) -> None:
    session = route_session(prompt, 180)

    assert session.posture == posture


@pytest.mark.parametrize(
    "prompt",
    [
        "No sitting or lying down",
        "Do not lie down or sit in a chair",
        "Avoid sitting in a chair and lying in bed",
        "Not sitting; not lying down",
    ],
)
def test_excluding_both_supported_postures_is_rejected(prompt: str) -> None:
    with pytest.raises(ValueError, match="Both supported postures are excluded"):
        route_session(prompt, 180)


def test_explicit_posture_control_remains_authoritative() -> None:
    session = route_session("Help me sleep lying down", 180, posture="sitting")

    assert session.posture == "sitting"


@pytest.mark.parametrize("negation", ["do not", "don't", "don\u2019t"])
def test_explicitly_staying_awake_does_not_route_to_sleep(negation: str) -> None:
    session = route_session(f"Keep me awake; {negation} fall asleep during my meeting", 180)

    assert session.kind == "focused_attention"
    assert session.posture == "sitting"
    assert session.recipe.id == "daytime-steadying"
    assert session.recipe.ending == "daytime_reorientation"


@pytest.mark.parametrize("negation", ["No", "Don't use", "Don\u2019t use"])
def test_negated_box_breathing_request_routes_natural_breathing(negation: str) -> None:
    session = route_session(f"{negation} box breathing; use natural breathing", 180)

    assert session.anchor == "natural_breath"
    assert not session.breath_excluded


@pytest.mark.parametrize(
    "prompt",
    [
        "No breath focus or box breathing, please",
        "No breath focus and box breathing, please",
        "Avoid breath focus or square breathing",
        "Don’t use breath focus or box breathing",
        "No box breathing; no breath focus either",
        "No box breathing or breath focus, please",
        "No box breathing and breath focus, please",
        "No box breathing, or breath focus, please",
        "No breath focus, or box breathing, please",
        "No box breathing, breath focus, or breath counting, please",
        "Don’t use square breathing or breath focus",
        "Do not use box breathing or breath focus; use contact instead",
        "Avoid box breathing or breath focus, but include surrounding sounds",
    ],
)
def test_compound_breath_exclusions_preserve_external_anchor(prompt: str) -> None:
    session = route_session(prompt, 180)

    assert session.breath_excluded
    assert session.anchor == "sound"


@pytest.mark.parametrize(
    "prompt",
    [
        "No box breathing, use natural breathing",
        "No box breathing. Use natural breathing",
        "No box breathing but use natural breathing",
        "Don’t use box breathing; use natural breathing",
        "Don’t use square breathing, but natural breathing is fine",
        "Avoid box breathing; instead use natural breathing",
        "No box breathing and use natural breathing",
        "No box breathing, and focus on natural breathing",
        "No box breathing and I want natural breathing",
        "Don’t use box breathing, and you can guide natural breathing",
        "No box breathing and my preference is natural breathing",
        "No box breathing and the session should use natural breathing",
    ],
)
def test_box_only_exclusion_does_not_extend_into_positive_clause(prompt: str) -> None:
    session = route_session(prompt, 180)

    assert not session.breath_excluded
    assert session.anchor == "natural_breath"


@pytest.mark.parametrize(
    "prompt",
    [
        "No breath focus, but use box breathing",
        "No box breathing; instead use box breathing",
        "Don’t use breath focus. Use square breathing",
        "No breath focus and use box breathing",
        "No breath focus and I want box breathing",
        "No breath focus or you can guide square breathing",
        "No breath focus and my preference is box breathing",
        "No breath focus and the session should use square breathing",
    ],
)
def test_positive_box_breathing_after_exclusion_is_still_unsupported(prompt: str) -> None:
    with pytest.raises(ValueError, match="Box breathing is not supported"):
        route_session(prompt, 180)


@pytest.mark.parametrize(
    "prompt",
    [
        "No box breathing; help me sleep",
        "No box breathing, but help me sleep",
        "No box breathing but help me sleep",
        "Don\u2019t use box breathing; help me sleep",
    ],
)
def test_negated_box_breathing_clause_preserves_separate_sleep_request(prompt: str) -> None:
    session = route_session(prompt, 180)

    assert session.kind == "sleep"
    assert session.recipe.ending == "sleep_taper"


@pytest.mark.parametrize(
    "prompt",
    [
        "No breath focus and help me sleep",
        "No breath focus, and help me sleep",
        "Don’t use breath focus and please help me sleep",
        "No box breathing or breath focus and help me sleep",
        "No breath focus or box breathing; help me sleep",
        "Help me sleep, but no breath focus",
        "No breath focus and I want to sleep",
        "No breath focus and you can help me sleep",
        "No breath focus, and I want to sleep",
        "Don’t use breath focus and I want to sleep",
        "No box breathing or breath focus and I want to sleep",
        "No breath focus or box breathing and you can help me sleep",
        "No breath focus or I would like help sleeping",
        "No breath focus and we want to sleep",
        "No breath focus and my goal is sleep",
        "No breath focus and our aim is sleep",
        "No breath focus and the session should help me sleep",
        "No breath focus, and a calming session could help me sleep",
        "Don’t use breath focus and sleep is my goal",
        "No box breathing or breath focus and my goal is sleep",
        "No breath focus or box breathing and our aim is sleep",
        "No breath focus and my partner wants to sleep",
        "No breath focus, sleep is my goal",
    ],
)
def test_independent_sleep_request_keeps_breath_exclusion(prompt: str) -> None:
    session = route_session(prompt, 180)

    assert session.kind == "sleep"
    assert session.posture == "lying_down"
    assert session.recipe.ending == "sleep_taper"
    assert session.breath_excluded
    assert session.anchor == "sound"


@pytest.mark.parametrize(
    "prompt",
    [
        "Do not fall asleep and help me focus",
        "Don’t fall asleep; no breath focus or box breathing",
        "No breath focus and do not fall asleep",
        "No breath focus and I don't want to sleep",
        "No breath focus and I don’t want to sleep",
        "No breath focus and you should avoid helping me fall asleep",
        "No breath focus and my goal is not sleep",
        "No breath focus and our aim isn’t sleep",
        "No breath focus and the session should not help me sleep",
        "My goal is relaxation, not sleep",
        "The session shouldn't help me sleep",
        "The session must not help me sleep",
        "Help me stay awake and not fall asleep during my meeting.",
        "Help me relax and not sleep during my meeting.",
        "I want relaxation and not sleep.",
        "I prefer not to fall asleep.",
        "No breath focus and please help me not fall asleep.",
    ],
)
def test_actual_sleep_negation_remains_daytime_across_clauses(prompt: str) -> None:
    session = route_session(prompt, 180)

    assert session.kind != "sleep"
    assert session.posture == "sitting"
    assert session.recipe.ending == "daytime_reorientation"


@pytest.mark.parametrize(
    "prompt",
    [
        "No breath focus and sleep",
        "No sleep or breath focus",
        "No breath focus or box breathing or sleep",
        "No sleep or box breathing or breath focus",
        "Don’t use breath focus or box breathing or sleep",
        "Avoid sleep and breath focus",
        "No breath focus, box breathing, or sleep",
        "No breath focus, sleep, or box breathing",
    ],
)
def test_coordinated_sleep_and_breath_objects_keep_shared_negation(prompt: str) -> None:
    session = route_session(prompt, 180)

    assert session.kind != "sleep"
    assert session.posture == "sitting"
    assert session.recipe.ending == "daytime_reorientation"
    assert session.breath_excluded
    assert session.anchor == "sound"


@pytest.mark.parametrize(
    "prompt",
    [
        "No sleep; use box breathing",
        "No sleep, but use box breathing",
        "No sleep but use box breathing",
        "Don\u2019t fall asleep; use box breathing",
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
