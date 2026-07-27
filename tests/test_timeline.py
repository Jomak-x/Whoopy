from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from whoopy.timeline import (
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
