"""Versioned, reviewable pacing profiles used by Studio and the Lab.

Pacing is product policy, not an executor magic constant.  This registry keeps
the speech speed and the narrowly allowed canonical-timeline mutations in one
strict, hashable document so evidence remains reproducible after defaults move.
"""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from whoopy.timeline import SilenceSegment, Timeline


class PacingProfileError(ValueError):
    """Raised when reviewed pacing policy is missing or malformed."""


class PacingProfile(BaseModel):
    """One immutable version of a product-level pacing policy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,95}$")
    version: int = Field(ge=1)
    label: str = Field(min_length=1, max_length=120)
    speech_speed: float = Field(ge=0.5, le=1.2)
    silence_strategy: Literal["preserve", "progressive_quiet"] = "preserve"
    allowed_timeline_mutations: tuple[Literal["SILENCE.duration_ms"], ...] = ()

    @model_validator(mode="after")
    def mutation_policy_matches_strategy(self) -> PacingProfile:
        expected = ("SILENCE.duration_ms",) if self.silence_strategy == "progressive_quiet" else ()
        if self.allowed_timeline_mutations != expected:
            raise ValueError(
                f"{self.silence_strategy} must declare exactly {list(expected)!r} as its "
                "timeline mutation allowlist"
            )
        return self

    @property
    def versioned_id(self) -> str:
        return f"{self.profile_id}@{self.version}"

    @property
    def digest(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class PacingProfileDocument(BaseModel):
    """Strict on-disk pacing registry schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    registry_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,95}$")
    profiles: tuple[PacingProfile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_profiles(self) -> PacingProfileDocument:
        ids = [profile.profile_id for profile in self.profiles]
        if len(ids) != len(set(ids)):
            raise ValueError("pacing profile IDs must be unique")
        return self


class PacingProfileRegistry:
    """Resolve reviewed pacing policy and expose its content identity."""

    def __init__(self, document: PacingProfileDocument, *, source: Path) -> None:
        self.document = document
        self.source = source
        self._profiles = {profile.profile_id: profile for profile in document.profiles}

    @classmethod
    def load(cls, path: Path = Path("config/pacing_profiles.yaml")) -> PacingProfileRegistry:
        try:
            return cls._from_yaml(path.read_text(encoding="utf-8"), source=path)
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise PacingProfileError(
                f"Could not load pacing profiles from {path}: {error}"
            ) from error

    @classmethod
    def load_bundled(cls) -> PacingProfileRegistry:
        """Load immutable package data when no project override is present."""

        source = files("whoopy.resources").joinpath("pacing_profiles.yaml")
        try:
            return cls._from_yaml(source.read_text(encoding="utf-8"), source=Path("<bundled>"))
        except (OSError, yaml.YAMLError, ValidationError) as error:
            raise PacingProfileError(f"Could not load bundled pacing profiles: {error}") from error

    @classmethod
    def _from_yaml(cls, text: str, *, source: Path) -> PacingProfileRegistry:
        raw = yaml.safe_load(text)
        document = PacingProfileDocument.model_validate(raw)
        return cls(document, source=source)

    def get(self, profile_id: str) -> PacingProfile:
        try:
            return self._profiles[profile_id]
        except KeyError as error:
            raise PacingProfileError(
                f"Pacing profile {profile_id!r} is not in reviewed registry "
                f"{self.document.registry_id!r}."
            ) from error

    def profiles(self) -> tuple[PacingProfile, ...]:
        return self.document.profiles


def apply_pacing_profile(timeline: Timeline, profile: PacingProfile) -> Timeline:
    """Apply one reviewed pacing policy without changing topology or total quiet.

    Prompt generation has already budgeted speech and silence against the requested
    duration.  A Studio pacing preset may redistribute that exact silence budget,
    but it must never edit breath events, invent pauses, or change total timing.
    """

    if profile.silence_strategy == "preserve":
        return timeline
    if profile.silence_strategy != "progressive_quiet":  # pragma: no cover - typed schema
        raise PacingProfileError(f"Unsupported pacing strategy {profile.silence_strategy!r}.")
    silence = [segment for segment in timeline.segments if isinstance(segment, SilenceSegment)]
    if len(silence) < 2:
        return timeline
    minimum_ms = 1_000
    budget = sum(segment.duration_ms for segment in silence)
    if budget < minimum_ms * len(silence):
        raise PacingProfileError("Progressive quiet cannot preserve the minimum rest duration.")
    count = len(silence)
    weights = [
        max(1, segment.duration_ms - minimum_ms) * (1.0 + 0.35 * index / (count - 1))
        for index, segment in enumerate(silence)
    ]
    remaining = budget - minimum_ms * count
    raw = [remaining * weight / sum(weights) for weight in weights]
    extras = [int(value) for value in raw]
    remainder = remaining - sum(extras)
    order = sorted(range(count), key=lambda index: raw[index] - extras[index], reverse=True)
    for index in order[:remainder]:
        extras[index] += 1
    durations = iter(minimum_ms + extra for extra in extras)
    return timeline.model_copy(
        update={
            "segments": [
                segment.model_copy(update={"duration_ms": next(durations)})
                if isinstance(segment, SilenceSegment)
                else segment
                for segment in timeline.segments
            ]
        }
    )
