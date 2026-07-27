"""The minimal, versioned timeline artifact produced by the Phase 1 worker."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

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


class Timeline(BaseModel):
    """Durable Phase 1 output shared by the worker and future renderers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: UUID
    created_at: AwareDatetime
    source: Literal["phase_1_prompt_passthrough"] = "phase_1_prompt_passthrough"
    segments: list[SpeechSegment] = Field(min_length=1)


def build_prompt_timeline(*, run_id: UUID, prompt: str, created_at: datetime) -> Timeline:
    """Create the honest Phase 1 placeholder: one speech segment from the prompt.

    This is deliberately deterministic and contains no model behavior. It makes
    the storage and worker flow executable while keeping script generation for a
    later adapter PR.
    """

    return Timeline(
        run_id=run_id,
        created_at=created_at,
        segments=[SpeechSegment(id="speech-0001", text=prompt)],
    )
