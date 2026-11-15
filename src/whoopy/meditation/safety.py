"""Small deterministic safety boundary for generated meditation prose."""

from __future__ import annotations

import re

SAFETY_ASSURANCE = re.compile(
    r"\b(?:you are|you're|you\u2019re|you will be)\s+"
    r"(?:(?:always|completely|perfectly|entirely)\s+)?"
    r"(?:safe\b|in\s+(?:a|the|this)\s+(?:(?:completely|perfectly)\s+)?"
    r"safe\s+(?:space|place|room|environment)\b)|"
    r"\b(?:this|the|your)\s+(?:space|place|room|environment)\s+is\s+"
    r"(?:(?:completely|perfectly|entirely)\s+)?safe\b|"
    r"\bthis\s+is\s+(?:a|your)\s+safe\s+(?:space|place|room|environment)\b|"
    r"\bnothing can harm you\b",
    re.I,
)
HYPOTHETICAL_SAFETY_PREFIX = re.compile(
    r"\b(?:imagine|visuali[sz]e|picture|suppose)\b"
    r"(?:(?!\b(?:but|however|actually|in reality)\b)[^.!?;,\n]){0,100}$|"
    r"\b(?:if|when)\s+(?:you\s+(?:feel|think|believe)(?:\s+that)?\s+)?$|"
    r"\b(?:do not|don't|don\u2019t|never|cannot|can't|can\u2019t|won't|will not)\s+"
    r"(?:claim|say|assert|assume|promise|tell yourself)\s+(?:that\s+)?$",
    re.I,
)


def has_actual_safety_assurance(text: str) -> bool:
    """Catch bounded actual-safety claims, not explicitly hypothetical clauses."""

    for match in SAFETY_ASSURANCE.finditer(text):
        prefix = re.split(r"[.!?;,\n]", text[: match.start()])[-1].rstrip(" \"'\u201c\u2018")
        # Keep trailing whitespace for the clause-prefix patterns. A later
        # sentence/clause does not inherit an earlier imagined or conditional frame.
        if HYPOTHETICAL_SAFETY_PREFIX.search(prefix + " ") is None:
            return True
    return False


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
CONTROL_SYNTAX = re.compile(
    r"(?:\[[^\]\r\n]*\]|<[^>\r\n]*>|^#{1,6}\s|```)",
    re.I | re.M,
)


class ContentSafetyError(ValueError):
    """Raised when text cannot enter a meditation artifact."""


def validate_meditation_text(text: str) -> None:
    """Reject a narrow, reviewable set of unsafe claims and control syntax."""

    if CONTROL_SYNTAX.search(text):
        raise ContentSafetyError("section prose contains forbidden markup or pause syntax")
    if has_actual_safety_assurance(text):
        raise ContentSafetyError("do not assert the listener is safe")
    for pattern, description in UNSAFE_PATTERNS:
        if pattern.search(text):
            raise ContentSafetyError(f"section prose contains a {description}")
