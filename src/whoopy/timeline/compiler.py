"""Deterministic text/Markdown script compilation into a canonical timeline."""

from __future__ import annotations

import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from whoopy.timeline.models import SilenceSegment, SpeechSegment, Timeline, TimelineSegment

PAUSE_PATTERN = re.compile(
    r"^\[\s*pause\s*:\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s)\s*\]$",
    re.IGNORECASE,
)
MARKDOWN_LINK = re.compile(r"!?\[([^\]]+)\]\([^)]+\)")
MARKDOWN_PREFIX = re.compile(r"^(?:>\s*|[-*+]\s+|\d+[.)]\s+)")
MAX_SCRIPT_CHARACTERS = 200_000
MAX_SPEECH_CHARACTERS = 600
MAX_SEGMENTS = 500


class ScriptCompileError(ValueError):
    """Raised when a local script cannot become an unambiguous timeline."""


def _pause_milliseconds(marker: str) -> int:
    match = PAUSE_PATTERN.fullmatch(marker)
    if match is None:
        raise ScriptCompileError(
            f"Invalid pause marker {marker!r}; use a line such as [pause: 3s]."
        )
    try:
        value = Decimal(match.group("value"))
    except InvalidOperation as error:
        raise ScriptCompileError(f"Invalid pause duration: {marker!r}") from error
    milliseconds = value if match.group("unit").lower() == "ms" else value * 1_000
    rounded = int(milliseconds.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if not 1 <= rounded <= 600_000:
        raise ScriptCompileError("Pause duration must be between 1 ms and 10 minutes.")
    return rounded


def _plain_spoken_text(text: str) -> str:
    """Remove a deliberately small safe subset of inline Markdown notation."""

    text = MARKDOWN_LINK.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("`", "")
    text = text.replace("*", "").replace("_", "")
    return " ".join(text.split())


def _bounded_chunks(paragraph: str) -> list[str]:
    """Split long prose at sentence/word boundaries for predictable TTS work."""

    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > MAX_SPEECH_CHARACTERS:
            words = sentence.split()
            for word in words:
                candidate = f"{current} {word}".strip()
                if current and len(candidate) > MAX_SPEECH_CHARACTERS:
                    chunks.append(current)
                    current = word
                else:
                    current = candidate
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > MAX_SPEECH_CHARACTERS:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        chunks.append(current)
    if any(len(chunk) > MAX_SPEECH_CHARACTERS for chunk in chunks):
        raise ScriptCompileError(
            "A script contains a word longer than the supported speech-segment limit."
        )
    return chunks


def build_script_timeline(
    *,
    run_id: UUID,
    script: str,
    created_at: datetime,
    source: Literal["script_file", "generated_prompt"] = "script_file",
) -> Timeline:
    """Compile paragraphs and standalone pause markers without model behavior."""

    if len(script) > MAX_SCRIPT_CHARACTERS:
        raise ScriptCompileError(
            f"Script exceeds the {MAX_SCRIPT_CHARACTERS:,}-character safety limit."
        )
    if not script.strip():
        raise ScriptCompileError("Script cannot be empty.")

    raw_blocks: list[str | int] = []
    paragraph_lines: list[str] = []
    in_front_matter = script.lstrip().startswith("---\n")
    front_matter_started = False
    in_code_fence = False

    def flush_paragraph() -> None:
        if not paragraph_lines:
            return
        paragraph = _plain_spoken_text(" ".join(paragraph_lines))
        paragraph_lines.clear()
        if paragraph:
            raw_blocks.extend(_bounded_chunks(paragraph))

    for raw_line in script.splitlines():
        line = raw_line.strip()
        if in_front_matter:
            if line == "---":
                if front_matter_started:
                    in_front_matter = False
                else:
                    front_matter_started = True
            continue
        if line.startswith("```"):
            flush_paragraph()
            in_code_fence = not in_code_fence
            continue
        if in_code_fence or line.startswith("<!--"):
            continue
        if not line or line == "---":
            flush_paragraph()
            continue
        if line.startswith("#"):
            flush_paragraph()
            continue
        if line.lower().startswith("[pause"):
            flush_paragraph()
            raw_blocks.append(_pause_milliseconds(line))
            continue
        cleaned_line = MARKDOWN_PREFIX.sub("", line)
        paragraph_lines.append(cleaned_line)
    flush_paragraph()

    segments: list[TimelineSegment] = []
    speech_number = 0
    silence_number = 0
    for block in raw_blocks:
        if isinstance(block, int):
            if segments and isinstance(segments[-1], SilenceSegment):
                combined = segments[-1].duration_ms + block
                if combined > 600_000:
                    raise ScriptCompileError("Adjacent pauses exceed the 10-minute limit.")
                segments[-1] = segments[-1].model_copy(update={"duration_ms": combined})
                continue
            silence_number += 1
            segments.append(
                SilenceSegment(
                    id=f"silence-{silence_number:04d}",
                    duration_ms=block,
                )
            )
        else:
            speech_number += 1
            segments.append(
                SpeechSegment(
                    id=f"speech-{speech_number:04d}",
                    text=block,
                )
            )
    if not any(isinstance(segment, SpeechSegment) for segment in segments):
        raise ScriptCompileError("Script must contain at least one spoken paragraph.")
    if len(segments) > MAX_SEGMENTS:
        raise ScriptCompileError(f"Script exceeds the {MAX_SEGMENTS}-segment safety limit.")

    return Timeline(
        schema_version=3 if source == "script_file" else 4,
        run_id=run_id,
        created_at=created_at,
        source=source,
        segments=segments,
    )
