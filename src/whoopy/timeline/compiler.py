"""Deterministic text/Markdown script compilation into a canonical timeline."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Literal, cast
from uuid import UUID

from whoopy.timeline.models import (
    BreathSegment,
    DeliveryIntensity,
    DeliveryStyle,
    PaceIntent,
    SilenceSegment,
    SpeechSegment,
    Timeline,
    TimelineSegment,
    VoiceMode,
)

PAUSE_PATTERN = re.compile(
    r"^\[\s*pause\s*:\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s)\s*\]$",
    re.IGNORECASE,
)
BREATH_PATTERN = re.compile(
    r"^\[\s*breath\s*:\s*(?P<phase>inhale|exhale)\s+"
    r"(?P<value>\d+(?:\.\d+)?)s\s+cycle=(?P<cycle>\d+)\s+"
    r"protocol=(?P<protocol>[a-z][a-z0-9_-]+@[1-9][0-9]*)\s*\]$",
    re.IGNORECASE,
)
DELIVERY_PATTERN = re.compile(
    r"^\[\s*delivery\s*:\s*(?P<value>[a-z_]+)\s*\]$",
    re.IGNORECASE,
)
INTENSITY_PATTERN = re.compile(
    r"^\[\s*intensity\s*:\s*(?P<value>[a-z_]+)\s*\]$",
    re.IGNORECASE,
)
PACE_PATTERN = re.compile(
    r"^\[\s*pace\s*:\s*(?P<value>[a-z_]+)\s*\]$",
    re.IGNORECASE,
)
VOICE_MODE_PATTERN = re.compile(
    r"^\[\s*voice-mode\s*:\s*(?P<value>[a-z_]+)\s*\]$",
    re.IGNORECASE,
)
CONTROL_VALUES: dict[str, tuple[str, ...]] = {
    "delivery": (
        "neutral",
        "warm",
        "calm",
        "soothing",
        "grounded",
        "compassionate",
        "sleepy",
        "encouraging",
    ),
    "intensity": ("subtle", "moderate"),
    "pace": ("natural", "unhurried", "slow", "very_slow"),
    "voice_mode": ("preset", "reference", "designed"),
}
MARKDOWN_LINK = re.compile(r"!?\[([^\]]+)\]\([^)]+\)")
BRACKETED_CONTROL = re.compile(r"\[[^\]\r\n]*\]")
ANGLE_BRACKETED_MARKUP = re.compile(r"<[^>\r\n]*>")
MARKDOWN_PREFIX = re.compile(r"^(?:>\s*|[-*+]\s+|\d+[.)]\s+)")
MAX_SCRIPT_CHARACTERS = 200_000
MAX_SPEECH_CHARACTERS = 600
MAX_SEGMENTS = 1_000


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
    unsupported = BRACKETED_CONTROL.search(text)
    if unsupported is not None:
        raise ScriptCompileError(
            f"Unknown inline script control {unsupported.group(0)!r}; put supported "
            "pause, breath, delivery, intensity, pace, or voice-mode controls on "
            "their own line."
        )
    model_markup = ANGLE_BRACKETED_MARKUP.search(text)
    if model_markup is not None:
        raise ScriptCompileError(
            f"Unsupported model-native markup {model_markup.group(0)!r}; deliberate "
            "pauses must use standalone Whoopy pause controls."
        )
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


@dataclass(frozen=True)
class _PauseMarker:
    duration_ms: int
    line_number: int


def build_script_timeline(
    *,
    run_id: UUID,
    script: str,
    created_at: datetime,
    source: Literal["script_file", "generated_prompt"] = "script_file",
) -> Timeline:
    """Compile paragraphs and standalone pause markers without model behavior."""

    timeline, _, _ = compile_script(
        run_id=run_id, script=script, created_at=created_at, source=source
    )
    return timeline


def rewrite_script_pauses(*, script: str, timeline: Timeline, durations: dict[str, int]) -> str:
    _, pause_lines, _ = compile_script(
        run_id=timeline.run_id,
        script=script,
        created_at=timeline.created_at,
        source=cast(Literal["script_file", "generated_prompt"], timeline.source),
    )
    lines = script.splitlines(keepends=True)
    for segment_id, duration_ms in durations.items():
        indexes = pause_lines[segment_id]
        first = indexes[0]
        lines[first] = lines[first].replace(lines[first].strip(), f"[pause: {duration_ms}ms]")
        for index in indexes[1:]:
            lines[index] = ""
    return "".join(lines)


def compile_script(
    *,
    run_id: UUID,
    script: str,
    created_at: datetime,
    source: Literal["script_file", "generated_prompt"],
) -> tuple[Timeline, dict[str, list[int]], dict[str, dict[str, tuple[int, int]]]]:
    """Compile the timeline and marker origins used for non-destructive edits."""
    if len(script) > MAX_SCRIPT_CHARACTERS:
        raise ScriptCompileError(
            f"Script exceeds the {MAX_SCRIPT_CHARACTERS:,}-character safety limit."
        )
    if not script.strip():
        raise ScriptCompileError("Script cannot be empty.")

    raw_blocks: list[str | _PauseMarker | tuple[str, str] | tuple[str, int, int, str]] = []
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

    for line_number, raw_line in enumerate(script.splitlines()):
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
            raw_blocks.append(_PauseMarker(_pause_milliseconds(line), line_number))
            continue
        if line.lower().startswith("[breath"):
            flush_paragraph()
            breath_match = BREATH_PATTERN.fullmatch(line)
            if breath_match is None:
                raise ScriptCompileError(
                    "Invalid breath marker; breath phases must come from a reviewed protocol."
                )
            duration_ms = _pause_milliseconds(f"[pause: {breath_match.group('value')}s]")
            raw_blocks.append(
                (
                    breath_match.group("phase").lower(),
                    duration_ms,
                    int(breath_match.group("cycle")),
                    breath_match.group("protocol").lower(),
                )
            )
            continue
        control_match = None
        for control, pattern in (
            ("delivery", DELIVERY_PATTERN),
            ("intensity", INTENSITY_PATTERN),
            ("pace", PACE_PATTERN),
            ("voice_mode", VOICE_MODE_PATTERN),
        ):
            control_match = pattern.fullmatch(line)
            if control_match is not None:
                flush_paragraph()
                raw_blocks.append((control, control_match.group("value").lower()))
                break
        if control_match is not None:
            continue
        if line.startswith("[") and line.endswith("]"):
            raise ScriptCompileError(
                f"Unknown script control {line!r}; use pause, breath, delivery, "
                "intensity, pace, or voice-mode."
            )
        cleaned_line = MARKDOWN_PREFIX.sub("", line)
        paragraph_lines.append(cleaned_line)
    flush_paragraph()

    segments: list[TimelineSegment] = []
    speech_number = 0
    silence_number = 0
    pause_lines: dict[str, list[int]] = {}
    breath_number = 0
    speech_controls: dict[str, str] = {
        "delivery": "neutral",
        "intensity": "subtle",
        "pace": "natural",
        "voice_mode": "preset",
    }
    control_origins: dict[str, dict[str, tuple[int, int]]] = {}
    active_origins: dict[str, tuple[int, int]] = {}
    marker_counts: dict[tuple[str, int], int] = {}
    for block in raw_blocks:
        if isinstance(block, tuple) and len(block) == 2:
            name, value = block
            allowed = CONTROL_VALUES[name]
            if value not in allowed:
                choices = ", ".join(allowed)
                raise ScriptCompileError(
                    f"Unsupported {name.replace('_', '-')} {value!r}; use {choices}."
                )
            speech_controls[name] = value
            # Speech position is stable across prose/whitespace edits; marker
            # occurrence distinguishes an explicit reset to a default value.
            marker_key = (name, speech_number)
            marker_counts[marker_key] = marker_counts.get(marker_key, 0) + 1
            active_origins[name] = (speech_number, marker_counts[marker_key])
            continue
        if isinstance(block, tuple):
            phase, duration_ms, cycle, protocol_id = block
            breath_number += 1
            segments.append(
                BreathSegment(
                    id=f"breath-{breath_number:04d}",
                    duration_ms=duration_ms,
                    phase=cast(Literal["inhale", "exhale"], phase),
                    cycle=cycle,
                    protocol_id=protocol_id,
                )
            )
            continue
        if isinstance(block, _PauseMarker):
            if segments and isinstance(segments[-1], SilenceSegment):
                combined = segments[-1].duration_ms + block.duration_ms
                if combined > 600_000:
                    raise ScriptCompileError("Adjacent pauses exceed the 10-minute limit.")
                segments[-1] = segments[-1].model_copy(update={"duration_ms": combined})
                pause_lines[segments[-1].id].append(block.line_number)
                continue
            silence_number += 1
            pause_lines[f"silence-{silence_number:04d}"] = [block.line_number]
            segments.append(
                SilenceSegment(
                    id=f"silence-{silence_number:04d}",
                    duration_ms=block.duration_ms,
                )
            )
        else:
            speech_number += 1
            control_origins[f"speech-{speech_number:04d}"] = active_origins.copy()
            segments.append(
                SpeechSegment(
                    id=f"speech-{speech_number:04d}",
                    text=block,
                    delivery=cast(DeliveryStyle, speech_controls["delivery"]),
                    intensity=cast(DeliveryIntensity, speech_controls["intensity"]),
                    pace=cast(PaceIntent, speech_controls["pace"]),
                    voice_mode=cast(VoiceMode, speech_controls["voice_mode"]),
                )
            )
    if not any(isinstance(segment, SpeechSegment) for segment in segments):
        raise ScriptCompileError("Script must contain at least one spoken paragraph.")
    if len(segments) > MAX_SEGMENTS:
        raise ScriptCompileError(f"Script exceeds the {MAX_SEGMENTS}-segment safety limit.")

    timeline = Timeline(
        schema_version=3 if source == "script_file" else 4,
        run_id=run_id,
        created_at=created_at,
        source=source,
        segments=segments,
    )
    return timeline, pause_lines, control_origins
