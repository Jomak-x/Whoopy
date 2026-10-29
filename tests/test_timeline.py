from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from whoopy.timeline import (
    BreathSegment,
    ScriptCompileError,
    SilenceSegment,
    SpeechSegment,
    Timeline,
    build_prompt_timeline,
    build_script_timeline,
)

RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
CREATED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def test_phase_one_timeline_remains_readable() -> None:
    timeline = build_prompt_timeline(
        run_id=RUN_ID,
        prompt="An old prompt.",
        created_at=CREATED_AT,
    )

    restored = Timeline.model_validate_json(timeline.model_dump_json())

    assert restored == timeline
    assert restored.schema_version == 1


def test_timeline_rejects_duplicate_segment_ids() -> None:
    with pytest.raises(ValidationError, match="segment IDs must be unique"):
        Timeline(
            schema_version=2,
            run_id=RUN_ID,
            created_at=CREATED_AT,
            source="phase_2_fixture_meditation",
            segments=[
                SpeechSegment(id="duplicate", text="First."),
                SpeechSegment(id="duplicate", text="Second."),
            ],
        )


def test_markdown_script_compiles_to_bounded_speech_and_exact_pauses() -> None:
    script = """---
title: Evening reset
---
# Evening Reset

Welcome **back** to [this moment](https://example.test).

[pause: 2.5s]

> Let your shoulders soften.
[pause: 500ms]
[pause: 1s]
"""

    timeline = build_script_timeline(
        run_id=RUN_ID,
        script=script,
        created_at=CREATED_AT,
    )

    assert timeline.schema_version == 3
    assert timeline.source == "script_file"
    assert timeline.segments == [
        SpeechSegment(
            id="speech-0001",
            text="Welcome back to this moment.",
        ),
        SilenceSegment(id="silence-0001", duration_ms=2_500),
        SpeechSegment(
            id="speech-0002",
            text="Let your shoulders soften.",
        ),
        SilenceSegment(id="silence-0002", duration_ms=1_500),
    ]


def test_script_compiler_splits_long_tts_work_at_word_boundaries() -> None:
    timeline = build_script_timeline(
        run_id=RUN_ID,
        script=" ".join(["gentle"] * 200),
        created_at=CREATED_AT,
    )
    speech = [segment for segment in timeline.segments if isinstance(segment, SpeechSegment)]

    assert len(speech) > 1
    assert all(len(segment.text) <= 600 for segment in speech)


def test_invalid_pause_marker_is_not_synthesized_as_words() -> None:
    with pytest.raises(ScriptCompileError, match="Invalid pause marker"):
        build_script_timeline(
            run_id=RUN_ID,
            script="Welcome.\n\n[pause eventually]",
            created_at=CREATED_AT,
        )


def test_delivery_controls_are_typed_and_never_enter_spoken_text() -> None:
    timeline = build_script_timeline(
        run_id=RUN_ID,
        script="""[delivery: calm]
[intensity: moderate]
[pace: slow]
[voice-mode: reference]

Let the next breath arrive by itself.

[delivery: compassionate]

If this feels uncomfortable, return to your natural rhythm.
""",
        created_at=CREATED_AT,
    )

    first, second = [segment for segment in timeline.segments if isinstance(segment, SpeechSegment)]
    assert first.text == "Let the next breath arrive by itself."
    assert first.delivery == "calm"
    assert first.intensity == "moderate"
    assert first.pace == "slow"
    assert first.voice_mode == "reference"
    assert second.delivery == "compassionate"
    assert "[" not in first.text + second.text


def test_unknown_or_unsupported_delivery_control_is_rejected() -> None:
    with pytest.raises(ScriptCompileError, match="Unsupported delivery"):
        build_script_timeline(
            run_id=RUN_ID,
            script="[delivery: furious]\n\nWelcome.",
            created_at=CREATED_AT,
        )


@pytest.mark.parametrize(
    "script",
    (
        "Welcome. [emotional] Let your shoulders soften.",
        "Continue gently with [delivery: calm] attention.",
        "Notice this breath [pause: 4s] before continuing.",
    ),
)
def test_inline_control_like_tags_never_reach_spoken_text(script: str) -> None:
    with pytest.raises(ScriptCompileError, match="Unknown inline script control"):
        build_script_timeline(
            run_id=RUN_ID,
            script=script,
            created_at=CREATED_AT,
        )


def test_markdown_links_are_not_mistaken_for_inline_controls() -> None:
    timeline = build_script_timeline(
        run_id=RUN_ID,
        script="Read [the local guide](https://example.invalid/guide) slowly.",
        created_at=CREATED_AT,
    )

    speech = [segment for segment in timeline.segments if isinstance(segment, SpeechSegment)]
    assert [segment.text for segment in speech] == ["Read the local guide slowly."]


@pytest.mark.parametrize(
    "script",
    (
        'Welcome. <break time="4s"/> Continue gently.',
        "<speak>Notice your breathing.</speak>",
        "Let the shoulders soften <!-- model cue --> now.",
    ),
)
def test_model_native_markup_never_reaches_spoken_text(script: str) -> None:
    with pytest.raises(ScriptCompileError, match="Unsupported model-native markup"):
        build_script_timeline(
            run_id=RUN_ID,
            script=script,
            created_at=CREATED_AT,
        )


def test_reviewed_breath_markers_compile_to_typed_exact_phases() -> None:
    timeline = build_script_timeline(
        run_id=RUN_ID,
        script=(
            "You can return to natural breathing at any time.\n\n"
            "[breath: inhale 4s cycle=1 protocol=gentle_even_4_4@1]\n\n"
            "[breath: exhale 4s cycle=1 protocol=gentle_even_4_4@1]"
        ),
        created_at=CREATED_AT,
    )

    breath = [segment for segment in timeline.segments if isinstance(segment, BreathSegment)]
    assert [(segment.phase, segment.duration_ms, segment.cycle) for segment in breath] == [
        ("inhale", 4_000, 1),
        ("exhale", 4_000, 1),
    ]
    assert all(segment.protocol_id == "gentle_even_4_4@1" for segment in breath)


def test_unreviewed_breath_syntax_cannot_enter_the_timeline() -> None:
    with pytest.raises(ScriptCompileError, match="reviewed protocol"):
        build_script_timeline(
            run_id=RUN_ID,
            script="Welcome.\n\n[breath: hold 8s]",
            created_at=CREATED_AT,
        )
    with pytest.raises(ScriptCompileError, match="Unknown script control"):
        build_script_timeline(
            run_id=RUN_ID,
            script="[emotion: calm]\n\nWelcome.",
            created_at=CREATED_AT,
        )
