"""Small deterministic safety boundary for generated meditation prose."""

from __future__ import annotations

import re

UNSAFE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"\b(?:cure|heal|treat|prevent)\b.{0,35}"
            r"\b(?:anxiety|depression|trauma|pain)\b",
            re.I,
        ),
        "medical treatment claim",
    ),
    (re.compile(r"\bguarantee(?:d|s)?\b", re.I), "guaranteed outcome"),
    (re.compile(r"\bhold (?:in |your )?breath\b", re.I), "required breath holding"),
    (
        re.compile(
            r"\b(?:inhale|breathe in|breath in)\b.{0,100}"
            r"\bhold(?:ing)? (?:it|that|the breath)\b",
            re.I,
        ),
        "required breath holding",
    ),
    (
        re.compile(r"\bhold(?:ing)? (?:it|the breath) for (?:a|one|the)\b", re.I),
        "required breath holding",
    ),
    (
        re.compile(
            r"\b(?:take|draw|breathe in) (?:a |one )?(?:slow,? )?deep breath\b",
            re.I,
        ),
        "prescribed deep breathing",
    ),
    (re.compile(r"\bfill your lungs\b", re.I), "prescribed deep breathing"),
    (
        re.compile(r"\b(?:breathe in|inhale|exhale|breathe out) (?:slowly|deeply|for)\b", re.I),
        "prescribed breath timing",
    ),
    (re.compile(r"\byou (?:should|must) (?:not )?feel\b", re.I), "prescriptive emotional claim"),
)
CONTROL_SYNTAX = re.compile(r"(?:\[pause\s*:|<speak>|</?[a-z]+>|^#{1,6}\s|```)", re.I | re.M)


class ContentSafetyError(ValueError):
    """Raised when text cannot enter a meditation artifact."""


def validate_meditation_text(text: str) -> None:
    """Reject a narrow, reviewable set of unsafe claims and control syntax."""

    if CONTROL_SYNTAX.search(text):
        raise ContentSafetyError("section prose contains forbidden markup or pause syntax")
    for pattern, description in UNSAFE_PATTERNS:
        if pattern.search(text):
            raise ContentSafetyError(f"section prose contains a {description}")
