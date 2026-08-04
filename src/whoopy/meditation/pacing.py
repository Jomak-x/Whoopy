"""Deterministic pacing rules for calm, reviewable meditation narration."""

from __future__ import annotations

import re

SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
WORD_PATTERN = re.compile(r"\b[\w'-]+\b")

# These pauses are deliberately audio events rather than prompt suggestions.
# That makes the rhythm visible in script.md, reproducible on resume, and
# independently testable without listening to every generated meditation.
CONTINUATION_PAUSE_MS = 1_000
ORDINARY_PAUSE_MS = 1_600
EMBODIED_PAUSE_MS = 2_400
BREATH_PAUSE_MS = 3_600
REFLECTION_PAUSE_MS = 4_800
PRACTICE_PAUSE_MS = 6_000
PLANNING_PAUSE_MS = 2_400
TARGET_SENTENCE_WORDS = 11
MAX_SENTENCE_WORDS = 22

BREATH_WORDS = re.compile(r"\b(?:breath|breathe|breathing|inhale|exhale)\b", re.I)
EMBODIED_WORDS = re.compile(
    r"\b(?:allow|feel|listen|notice|rest|sense|settle|soften|support|weight)\b",
    re.I,
)
CONTINUATION_WORDS = re.compile(
    r"^(?:and|as|for now|if|now|then|when|with)\b|\b(?:again|instead|simply)\b",
    re.I,
)
REFLECTION_WORDS = re.compile(
    r"\b(?:ask|consider|imagine|picture|question|recall|reflect|wonder)\b|[?]\s*$",
    re.I,
)
PRACTICE_WORDS = re.compile(
    r"\b(?:for a few moments|for a little while|rest here|stay here|take a moment)\b",
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

    if PRACTICE_WORDS.search(sentence):
        return PRACTICE_PAUSE_MS
    if REFLECTION_WORDS.search(sentence):
        return REFLECTION_PAUSE_MS
    if BREATH_WORDS.search(sentence):
        return BREATH_PAUSE_MS
    if EMBODIED_WORDS.search(sentence):
        return EMBODIED_PAUSE_MS
    if CONTINUATION_WORDS.search(sentence) or sentence_word_count(sentence) <= 6:
        return CONTINUATION_PAUSE_MS
    return ORDINARY_PAUSE_MS


def _should_join(first: str, second: str) -> bool:
    """Keep closely related short sentences in one natural TTS utterance."""

    combined_words = sentence_word_count(first) + sentence_word_count(second)
    if combined_words > 24:
        return False
    if pause_after_sentence_ms(first) != CONTINUATION_PAUSE_MS:
        return False
    # Questions and explicit practice invitations need audible thinking room.
    return not REFLECTION_WORDS.search(first) and not PRACTICE_WORDS.search(first)


def pace_section(text: str, *, section_pause_ms: int) -> str:
    """Create varied, deterministic phrasing and a longer section practice."""

    sentences = split_sentences(text)
    if not sentences:
        raise ValueError("a meditation section must contain spoken sentences")

    phrases: list[str] = []
    index = 0
    while index < len(sentences):
        phrase = sentences[index]
        if index + 1 < len(sentences) and _should_join(phrase, sentences[index + 1]):
            phrase = f"{phrase} {sentences[index + 1]}"
            index += 1
        phrases.append(phrase)
        index += 1

    blocks: list[str] = []
    for phrase in phrases[:-1]:
        blocks.extend([phrase, f"[pause: {pause_after_sentence_ms(phrase)}ms]"])
    blocks.extend([phrases[-1], f"[pause: {section_pause_ms}ms]"])
    return "\n\n".join(blocks)


def estimated_seconds_per_word(articulation_words_per_minute: int) -> float:
    """Estimate speech plus sentence rests before section-ending long pauses."""

    speech_seconds = 60 / articulation_words_per_minute
    average_rest_seconds = PLANNING_PAUSE_MS / 1_000 / TARGET_SENTENCE_WORDS
    return speech_seconds + average_rest_seconds
