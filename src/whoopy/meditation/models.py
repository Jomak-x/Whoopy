"""Typed artifacts between untrusted model text and the Whoopy timeline."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

SectionId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9-]{0,47}$"),
]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
SpokenText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=5_000),
]


class ProposedSection(BaseModel):
    """A model-proposed section before deterministic time allocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SectionId
    title: ShortText
    purpose: ShortText
    weight: int = Field(ge=1, le=5)
    pause_seconds: float = Field(ge=1, le=12)


class ProposedPlan(BaseModel):
    """Strict shape accepted from the local model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: ShortText
    intention: ShortText
    sections: list[ProposedSection] = Field(min_length=3, max_length=6)

    @model_validator(mode="after")
    def unique_sections(self) -> ProposedPlan:
        ids = [section.id for section in self.sections]
        if len(ids) != len(set(ids)):
            raise ValueError("section IDs must be unique")
        return self


class PlannedSection(BaseModel):
    """A validated section with a deterministic time and word budget."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SectionId
    title: ShortText
    purpose: ShortText
    target_speech_seconds: int = Field(ge=8)
    pause_after_ms: int = Field(ge=1_000, le=12_000)
    minimum_words: int = Field(ge=8)
    maximum_words: int = Field(ge=8)

    @model_validator(mode="after")
    def ordered_word_range(self) -> PlannedSection:
        if self.maximum_words < self.minimum_words:
            raise ValueError("maximum_words must be at least minimum_words")
        return self


class MeditationPlan(BaseModel):
    """Canonical plan safe for section drafting."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    title: ShortText
    intention: ShortText
    requested_duration_seconds: int = Field(ge=60, le=1_800)
    words_per_minute: int = Field(default=165, ge=80, le=220)
    sections: list[PlannedSection] = Field(min_length=3, max_length=6)


class DraftedSection(BaseModel):
    """One validated, speakable model result."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    section_id: SectionId
    text: SpokenText
    word_count: int = Field(ge=1)

    @model_validator(mode="after")
    def accurate_word_count(self) -> DraftedSection:
        actual = len(re.findall(r"\b[\w'-]+\b", self.text))
        if actual != self.word_count:
            raise ValueError("word_count does not match the spoken text")
        return self


class ProposedDraft(BaseModel):
    """Strict shape expected directly from the model."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: SectionId
    text: SpokenText


class RawGenerationAttempt(BaseModel):
    """Untrusted output retained for debugging, never used as a domain input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str = Field(min_length=1)
    attempt: int = Field(ge=1)
    text: str
    elapsed_seconds: float = Field(ge=0)
    validation_error: str | None = None
