"""Reviewed technique boundaries used by Meditation Engine V2."""

from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from whoopy.locations import default_config_directory
from whoopy.meditation.models import (
    BreathMode,
    EndingBehavior,
    MeditationKind,
    MeditationTechnique,
    PracticeBeat,
)

REQUIRED_PROGRESSION: tuple[PracticeBeat, ...] = (
    "arrival",
    "instruction",
    "practice",
    "reminder",
    "deeper_practice",
    "ending",
)


class TechniqueBlueprint(BaseModel):
    """One original, versioned technique boundary reviewed for local use."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    version: int = Field(ge=1)
    kind: MeditationKind
    purpose: str = Field(min_length=20, max_length=500)
    allowed_techniques: list[MeditationTechnique] = Field(min_length=2)
    primary_technique: MeditationTechnique
    breath_mode: BreathMode
    ending_behavior: EndingBehavior
    progression: tuple[PracticeBeat, ...] = REQUIRED_PROGRESSION

    @model_validator(mode="after")
    def validate_contract(self) -> TechniqueBlueprint:
        if self.id != self.kind:
            raise ValueError("blueprint ID must match meditation kind")
        if self.progression != REQUIRED_PROGRESSION:
            raise ValueError("blueprint must retain the reviewed six-beat progression")
        expected_ending = "sleep_taper" if self.kind == "sleep" else "daytime_reorientation"
        if self.ending_behavior != expected_ending:
            raise ValueError(f"{self.kind} requires {expected_ending}")
        required_technique = "sleep_transition" if self.kind == "sleep" else "return"
        if required_technique not in self.allowed_techniques:
            raise ValueError(f"blueprint must allow ending technique {required_technique}")
        if len(set(self.allowed_techniques)) != len(self.allowed_techniques):
            raise ValueError("allowed techniques must be unique")
        if self.primary_technique not in self.allowed_techniques:
            raise ValueError("primary technique must be allowed by the blueprint")
        if self.primary_technique in {"arrival", required_technique}:
            raise ValueError("primary technique must be a practice technique")
        return self

    @property
    def versioned_id(self) -> str:
        return f"{self.id}@{self.version}"


class BlueprintLibrary(BaseModel):
    """Complete reviewed blueprint set; missing kinds are a startup error."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    blueprints: list[TechniqueBlueprint] = Field(min_length=11)

    @model_validator(mode="after")
    def complete_and_unique(self) -> BlueprintLibrary:
        expected = set(get_args(MeditationKind))
        kinds = [blueprint.kind for blueprint in self.blueprints]
        if len(kinds) != len(set(kinds)):
            raise ValueError("blueprint kinds must be unique")
        if set(kinds) != expected:
            missing = sorted(expected - set(kinds))
            extra = sorted(set(kinds) - expected)
            raise ValueError(f"blueprint set mismatch; missing={missing}, extra={extra}")
        return self

    def for_kind(self, kind: MeditationKind) -> TechniqueBlueprint:
        return next(blueprint for blueprint in self.blueprints if blueprint.kind == kind)


class BlueprintLoadError(ValueError):
    """Raised when reviewed blueprint data is incomplete or invalid."""


def load_blueprint_library(
    path: Path | None = None,
) -> BlueprintLibrary:
    path = path if path is not None else default_config_directory() / "meditation_blueprints.yaml"
    try:
        return BlueprintLibrary.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, ValidationError, yaml.YAMLError) as error:
        raise BlueprintLoadError(
            f"Could not load meditation blueprints from {path}: {error}"
        ) from error
