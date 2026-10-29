from __future__ import annotations

from typing import get_args

import pytest

from whoopy.meditation.models import MeditationKind
from whoopy.meditation.recipes import route_session


@pytest.mark.parametrize("kind", get_args(MeditationKind))
@pytest.mark.parametrize(
    "prompt",
    [
        "Anxiety before an important meeting",
        "Reflect on a busy day with self-compassion before sleep",
        "A body scan for focus after a setback",
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
