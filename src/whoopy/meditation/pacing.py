"""Deterministic pacing rules for calm, reviewable meditation narration."""

from __future__ import annotations

import re

SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
WORD_PATTERN = re.compile(r"\b[\w'-]+\b")

# These pauses are deliberately audio events rather than prompt suggestions.
# That makes the rhythm visible in script.md, reproducible on resume, and
# independently testable without listening to every generated meditation.
ORDINARY_PAUSE_MS = 1_800
EMBODIED_PAUSE_MS = 2_800
BREATH_PAUSE_MS = 4_500
PLANNING_PAUSE_MS = 2_400
TARGET_SENTENCE_WORDS = 11
MAX_SENTENCE_WORDS = 22

BREATH_WORDS = re.compile(r"\b(?:breath|breathe|breathing|inhale|exhale)\b", re.I)
EMBODIED_WORDS = re.compile(
    r"\b(?:allow|feel|listen|notice|rest|sense|settle|soften|support|weight)\b",
    re.I,
)


def split_sentences(text: str) -> list[str]:
    """Return normalized sentences while rejecting punctuation-free monologues."""

    return [
        sentence.strip() for sentence in SENTENCE_BOUNDARY.split(text.strip()) if sentence.strip()
    ]


def sentence_word_count(sentence: str) -> int:
    """Count speakable words using the same definition as generation."""

    return len(WORD_PATTERN.findall(sentence))


def pause_after_sentence_ms(sentence: str) -> int:
    """Choose a stable rest based on the kind of invitation just spoken."""

    if BREATH_WORDS.search(sentence):
        return BREATH_PAUSE_MS
    if EMBODIED_WORDS.search(sentence):
        return EMBODIED_PAUSE_MS
    return ORDINARY_PAUSE_MS


def pace_section(text: str, *, section_pause_ms: int) -> str:
    """Place one speech block per sentence and a longer rest after the section."""

    sentences = split_sentences(text)
    if not sentences:
        raise ValueError("a meditation section must contain spoken sentences")

    blocks: list[str] = []
    for sentence in sentences[:-1]:
        blocks.extend([sentence, f"[pause: {pause_after_sentence_ms(sentence)}ms]"])
    blocks.extend([sentences[-1], f"[pause: {section_pause_ms}ms]"])
    return "\n\n".join(blocks)


def estimated_seconds_per_word(articulation_words_per_minute: int) -> float:
    """Estimate speech plus sentence rests before section-ending long pauses."""

    speech_seconds = 60 / articulation_words_per_minute
    average_rest_seconds = PLANNING_PAUSE_MS / 1_000 / TARGET_SENTENCE_WORDS
    return speech_seconds + average_rest_seconds
