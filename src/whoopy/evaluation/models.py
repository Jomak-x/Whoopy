"""Typed input and output contracts for the Phase 3.5 bake-off."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from whoopy.ports import AdapterMetadata


class EvaluationCase(BaseModel):
    """One reviewed request in the versioned test set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    category: Literal[
        "sleep",
        "grounding",
        "anxiety",
        "body_scan",
        "breath_awareness",
        "focus",
    ]
    prompt: str = Field(min_length=1, max_length=2_000)
    duration_seconds: int = Field(ge=60, le=600)


class EvaluationSet(BaseModel):
    """Immutable collection that makes comparisons use identical requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> EvaluationSet:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case IDs must be unique")
        return self


class BakeoffCaseResult(BaseModel):
    """Automatic measurements for one model/case pair; no hidden total score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    profile: Literal["lite", "standard"]
    success: bool
    elapsed_seconds: float = Field(ge=0)
    peak_process_tree_mb: float = Field(ge=0)
    model_artifact_bytes: int = Field(gt=0)
    section_count: int | None = Field(default=None, ge=1)
    validation_retries: int = Field(ge=0)
    estimated_duration_seconds: float | None = Field(default=None, gt=0)
    timing_error_percent: float | None = Field(default=None, ge=0)
    repeated_trigram_ratio: float | None = Field(default=None, ge=0, le=1)
    invitational_phrase_count: int | None = Field(default=None, ge=0)
    safety_validation_passed: bool
    failure: str | None = None

    @model_validator(mode="after")
    def consistent_success(self) -> BakeoffCaseResult:
        if self.success == (self.failure is not None):
            raise ValueError("success requires no failure; failure requires a message")
        return self


class BakeoffReport(BaseModel):
    """Machine-readable evidence plus explicit fields reserved for humans."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str
    evaluation_version: int
    created_at: AwareDatetime
    seed: int
    platform: str
    candidates: dict[str, AdapterMetadata]
    results: list[BakeoffCaseResult] = Field(min_length=1)
    human_review_status: Literal["pending", "complete"] = "pending"
    human_review_artifact: str | None = None
    decision: str = "No default changes until blind listening review is complete."


class EvaluationSetError(ValueError):
    """Raised when the versioned evaluation fixture is malformed."""


def load_evaluation_set(path: Path) -> EvaluationSet:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return EvaluationSet.model_validate(payload)
    except (OSError, ValidationError, yaml.YAMLError) as error:
        raise EvaluationSetError(f"Could not load evaluation set {path}: {error}") from error
