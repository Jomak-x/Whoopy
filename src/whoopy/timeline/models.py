"""The minimal, versioned timeline artifact produced by the Phase 1 worker."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

PromptText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
]
SegmentId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")]


class SpeechSegment(BaseModel):
    """One piece of text that a future speech adapter can synthesize."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SegmentId
    type: Literal["SPEECH"] = "SPEECH"
    text: PromptText


class SilenceSegment(BaseModel):
    """An exact deliberate pause measured in milliseconds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SegmentId
    type: Literal["SILENCE"] = "SILENCE"
    duration_ms: int = Field(gt=0, le=600_000)


TimelineSegment = Annotated[SpeechSegment | SilenceSegment, Field(discriminator="type")]


class Timeline(BaseModel):
    """Versioned source of truth shared by the worker and audio renderer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 2
    run_id: UUID
    created_at: AwareDatetime
    source: Literal["phase_1_prompt_passthrough", "phase_2_fixture_meditation"]
    segments: list[TimelineSegment] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_version_and_segments(self) -> Timeline:
        """Keep old Phase 1 artifacts readable while making v2 unambiguous."""

        segment_ids = [segment.id for segment in self.segments]
        if len(segment_ids) != len(set(segment_ids)):
            raise ValueError("timeline segment IDs must be unique")
        if not any(isinstance(segment, SpeechSegment) for segment in self.segments):
            raise ValueError("a timeline must contain at least one SPEECH segment")
        if self.schema_version == 1:
            if self.source != "phase_1_prompt_passthrough":
                raise ValueError("timeline schema v1 requires the Phase 1 source")
            if any(isinstance(segment, SilenceSegment) for segment in self.segments):
                raise ValueError("timeline schema v1 does not support SILENCE segments")
        elif self.source != "phase_2_fixture_meditation":
            raise ValueError("timeline schema v2 requires the Phase 2 source")
        return self


def build_prompt_timeline(*, run_id: UUID, prompt: str, created_at: datetime) -> Timeline:
    """Create the honest Phase 1 placeholder: one speech segment from the prompt.

    This is deliberately deterministic and contains no model behavior. It makes
    the storage and worker flow executable while keeping script generation for a
    later adapter PR.
    """

    return Timeline(
        schema_version=1,
        run_id=run_id,
        created_at=created_at,
        source="phase_1_prompt_passthrough",
        segments=[SpeechSegment(id="speech-0001", text=prompt)],
    )


def build_fixture_timeline(
    *,
    run_id: UUID,
    prompt: str,
    created_at: datetime,
    pause_ms: int = 1_500,
) -> Timeline:
    """Create an audible-test timeline with one exact join between two tones."""

    return Timeline(
        schema_version=2,
        run_id=run_id,
        created_at=created_at,
        source="phase_2_fixture_meditation",
        segments=[
            SpeechSegment(id="speech-0001", text=prompt),
            SilenceSegment(id="silence-0001", duration_ms=pause_ms),
            SpeechSegment(
                id="speech-0002",
                text="The deterministic Phase 2 fixture is complete.",
            ),
        ],
    )
