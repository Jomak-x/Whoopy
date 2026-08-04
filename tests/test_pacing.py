from __future__ import annotations

from whoopy.meditation.pacing import (
    BREATH_PAUSE_MS,
    CONTINUATION_PAUSE_MS,
    EMBODIED_PAUSE_MS,
    ORDINARY_PAUSE_MS,
    PRACTICE_PAUSE_MS,
    REFLECTION_PAUSE_MS,
    pace_section,
    pause_after_sentence_ms,
)


def test_pause_categories_make_breath_and_body_invitations_more_spacious() -> None:
    assert pause_after_sentence_ms("This quiet moment belongs to you today.") == ORDINARY_PAUSE_MS
    assert pause_after_sentence_ms("Notice the support beneath you.") == EMBODIED_PAUSE_MS
    assert pause_after_sentence_ms("Let your breathing remain natural.") == BREATH_PAUSE_MS
    assert pause_after_sentence_ms("What would feel supportive now?") == REFLECTION_PAUSE_MS
    assert pause_after_sentence_ms("Stay here for a few moments.") == PRACTICE_PAUSE_MS
    assert pause_after_sentence_ms("And come back again.") == CONTINUATION_PAUSE_MS


def test_paced_section_joins_short_continuation_but_keeps_meaningful_rest() -> None:
    script = pace_section(
        "You are here. Notice the surface beneath you. Let your breath remain natural.",
        section_pause_ms=12_000,
    )

    assert script == (
        "You are here. Notice the surface beneath you.\n\n"
        "[pause: 2400ms]\n\n"
        "Let your breath remain natural.\n\n"
        "[pause: 12000ms]"
    )
