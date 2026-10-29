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
DeliveryStyle = Literal[
    "neutral",
    "warm",
    "calm",
    "soothing",
    "grounded",
    "compassionate",
    "sleepy",
    "encouraging",
]
DeliveryIntensity = Literal["subtle", "moderate"]
VoiceMode = Literal["preset", "reference", "designed"]
PaceIntent = Literal["natural", "unhurried", "slow", "very_slow"]


class PronunciationOverride(BaseModel):
    """A visible, adapter-neutral pronunciation replacement."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    pronunciation: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=160),
    ]


class SpeechSegment(BaseModel):
    """One spoken block plus portable delivery intent.

    Delivery metadata is deliberately separate from ``text``. Adapters may
    translate supported intent into native controls, but unsupported bracket
    tags can never leak into narration.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SegmentId
    type: Literal["SPEECH"] = "SPEECH"
    text: PromptText
    delivery: DeliveryStyle = "neutral"
    intensity: DeliveryIntensity = "subtle"
    language: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=2, max_length=64),
    ] = "English"
    voice_mode: VoiceMode = "preset"
    pace: PaceIntent = "natural"
    pronunciation_overrides: tuple[PronunciationOverride, ...] = ()
    # Optional native speech-duration intent. Exact deliberate quiet remains a
    # separate SILENCE/BREATH event; this value never creates model pause markup.
    target_duration_ms: int | None = Field(default=None, ge=400, le=120_000)


class SilenceSegment(BaseModel):
    """An exact deliberate pause measured in milliseconds."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SegmentId
    type: Literal["SILENCE"] = "SILENCE"
    duration_ms: int = Field(gt=0, le=600_000)


class BreathSegment(BaseModel):
    """One reviewed inhale or exhale phase rendered as exact silence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SegmentId
    type: Literal["BREATH"] = "BREATH"
    duration_ms: int = Field(gt=0, le=30_000)
    phase: Literal["inhale", "exhale"]
    cycle: int = Field(ge=1, le=20)
    protocol_id: Annotated[
        str,
        StringConstraints(strip_whitespace=True, pattern=r"^[a-z][a-z0-9_-]+@[1-9][0-9]*$"),
    ]


TimelineSegment = Annotated[
    SpeechSegment | SilenceSegment | BreathSegment,
    Field(discriminator="type"),
]


class Timeline(BaseModel):
    """Versioned source of truth shared by the worker and audio renderer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2, 3, 4] = 2
    run_id: UUID
    created_at: AwareDatetime
    source: Literal[
        "phase_1_prompt_passthrough",
        "phase_2_fixture_meditation",
        "script_file",
        "generated_prompt",
    ]
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
            if any(not isinstance(segment, SpeechSegment) for segment in self.segments):
                raise ValueError("timeline schema v1 supports only SPEECH segments")
        elif self.schema_version == 2 and self.source != "phase_2_fixture_meditation":
            raise ValueError("timeline schema v2 requires the Phase 2 source")
        elif self.schema_version == 3 and self.source != "script_file":
            raise ValueError("timeline schema v3 requires the script-file source")
        elif self.schema_version == 4 and self.source != "generated_prompt":
            raise ValueError("timeline schema v4 requires the generated-prompt source")
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
