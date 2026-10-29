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

from whoopy.ports import ScriptGenerationTelemetry

SectionId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, pattern=r"^[a-z0-9][a-z0-9-]{0,47}$"),
]
ShortText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
SpokenText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000),
]
MeditationTechnique = Literal[
    "arrival",
    "body_scan",
    "focused_attention",
    "loving_kindness",
    "noting",
    "reflection",
    "resting_awareness",
    "return",
    "sleep_transition",
    "visualization",
]
MeditationKind = Literal[
    "grounding",
    "focused_attention",
    "breath_awareness",
    "body_scan",
    "noting",
    "reflection",
    "loving_kindness",
    "self_compassion",
    "visualization",
    "open_awareness",
    "sleep",
]
PracticeBeat = Literal[
    "arrival",
    "instruction",
    "practice",
    "reminder",
    "deeper_practice",
    "ending",
]
PauseIntent = Literal["instruction", "embodied", "reflection", "practice", "extended"]
BreathMode = Literal["natural_awareness", "paced_no_hold", "external_anchor"]
EndingBehavior = Literal["daytime_reorientation", "sleep_taper"]
DeliveryIntent = Literal[
    "neutral",
    "warm",
    "calm",
    "soothing",
    "grounded",
    "compassionate",
    "sleepy",
    "encouraging",
]


class ProposedSection(BaseModel):
    """A model-proposed section before deterministic time allocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: SectionId
    title: ShortText
    purpose: ShortText
    technique: MeditationTechnique
    weight: int = Field(ge=1, le=5)
    pause_intent: PauseIntent = "practice"
    # Accepted only for v1 prompt compatibility. V2 allocates duration from
    # typed intent rather than trusting a model-proposed number.
    pause_seconds: float | None = Field(default=None, ge=6, le=20)


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
    technique: MeditationTechnique = "focused_attention"
    beat: PracticeBeat = "practice"
    pause_intent: PauseIntent = "practice"
    delivery: DeliveryIntent = "calm"
    target_speech_seconds: int = Field(ge=8)
    # Very short requests with many sections may scale the model's generous
    # proposal down, while normal practices retain the requested 6-20 seconds.
    pause_after_ms: int = Field(ge=1_000, le=120_000)
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

    schema_version: Literal[1, 2] = 2
    title: ShortText
    intention: ShortText
    requested_outcome: ShortText = "Support the requested meditation outcome."
    meditation_kind: MeditationKind = "focused_attention"
    blueprint_id: str = "focused_attention@1"
    blueprint_version: int = Field(default=1, ge=1)
    breath_mode: BreathMode = "natural_awareness"
    ending_behavior: EndingBehavior = "daytime_reorientation"
    recipe_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    writing_prompt_id: str | None = None
    writing_prompt_version: int | None = Field(default=None, ge=1)
    writing_prompt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    timing_compiler_algorithm_version: int | None = Field(default=None, ge=1)
    response_schema_algorithm_version: int | None = Field(default=None, ge=1)
    response_schema_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    posture: Literal["sitting", "lying_down"] | None = None
    anchor: Literal["natural_breath", "sound", "contact"] | None = None
    silence_budget_seconds: int = Field(default=0, ge=0)
    requested_duration_seconds: int = Field(ge=60, le=1_800)
    words_per_minute: int = Field(default=165, ge=80, le=220)
    sections: list[PlannedSection] = Field(min_length=3, max_length=12)


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
    request_seed: int | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    telemetry: ScriptGenerationTelemetry = Field(default_factory=ScriptGenerationTelemetry)
    validation_error: str | None = None
