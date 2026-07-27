from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from whoopy.timeline import SpeechSegment, Timeline, build_prompt_timeline

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
