"""Versioned structural recipes and deterministic routing, never canned prose."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from whoopy.locations import default_config_directory
from whoopy.meditation.models import EndingBehavior, MeditationKind, MeditationTechnique

Posture = Literal["sitting", "lying_down"]
Anchor = Literal["natural_breath", "sound", "contact"]


class RecipeBeat(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    technique: MeditationTechnique
    weight: int = Field(ge=1, le=4)
    purpose: str = Field(min_length=10, max_length=240)


class SessionRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    version: int = Field(ge=1)
    ending: EndingBehavior
    beats: list[RecipeBeat] = Field(min_length=3, max_length=12)

    @model_validator(mode="after")
    def coherent_beats(self) -> SessionRecipe:
        if len({beat.id for beat in self.beats}) != len(self.beats):
            raise ValueError("recipe beat IDs must be unique")
        if self.beats[0].technique != "arrival":
            raise ValueError("recipe must begin with arrival")
        expected = "sleep_transition" if self.ending == "sleep_taper" else "return"
        if self.beats[-1].technique != expected:
            raise ValueError("recipe ending technique contradicts its ending contract")
        return self

    @property
    def versioned_id(self) -> str:
        return f"{self.id}@{self.version}"


class RecipeLibrary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    recipes: list[SessionRecipe] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_recipes(self) -> RecipeLibrary:
        if len({recipe.id for recipe in self.recipes}) != len(self.recipes):
            raise ValueError("recipe IDs must be unique")
        return self


def load_recipe_library(path: Path | None = None) -> RecipeLibrary:
    active_path = path if path is not None else default_config_directory() / "session_recipes.yaml"
    return RecipeLibrary.model_validate(yaml.safe_load(active_path.read_text(encoding="utf-8")))


class RoutedSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    recipe: SessionRecipe
    posture: Posture
    anchor: Anchor
    kind: MeditationKind
    main_practice: str
    breath_excluded: bool


NO_BREATH = re.compile(
    r"\b(?:no|without|avoid|skip)\b[^.!?\n]{0,35}\b(?:breath|breathing)\b|"
    r"\b(?:don['\u2019]t|do not|rather not)\b[^.!?\n]{0,35}\b(?:breath|breathing)\b|"
    r"\b(?:breath|breathing)\b[^.!?\n]{0,30}\b(?:uncomfortable|panic|trigger|worse)\w*\b",
    re.I,
)
SLEEP = re.compile(r"\b(?:sleep|sleeping|bedtime|good[\s-]?night|fall asleep|insomnia)\b", re.I)
NEGATED_SLEEP = re.compile(
    r"\b(?:no|without|avoid|skip|don['\u2019]t|do not|rather not)\b"
    r"(?:(?!\b(?:but|however|yet|instead)\b)[^.!?,;\n]){0,35}"
    r"\b(?:sleep|sleeping|bedtime|good[\s-]?night|fall asleep|insomnia)\b",
    re.I,
)
BOX_BREATHING = re.compile(r"\b(?:box|square)\s+breathing\b", re.I)
NEGATED_BOX_BREATHING = re.compile(
    r"\b(?:no|without|avoid|skip|don['\u2019]t|do not|rather not)\b"
    r"(?:(?!\b(?:but|however|yet|instead)\b)[^.!?,;\n]){0,20}"
    r"\b(?:box|square)\s+breathing\b",
    re.I,
)
REFLECTION = re.compile(r"\b(?:reflect\w*|review the day|busy day|good[ -]?night)\b", re.I)


def route_session(
    prompt: str,
    duration_seconds: int,
    *,
    posture: Posture | None = None,
    meditation_kind: MeditationKind | None = None,
    library: RecipeLibrary | None = None,
) -> RoutedSession:
    """Use need and explicit constraints before selecting the hidden grammar."""

    breath_constraints = NEGATED_BOX_BREATHING.sub("", prompt)
    if BOX_BREATHING.search(breath_constraints):
        raise ValueError(
            "Box breathing is not supported by a reviewed holding protocol. "
            "Request natural breathing or gentle no-hold breathing instead."
        )
    sleep_constraints = NEGATED_SLEEP.sub("", prompt)
    sleep = meditation_kind == "sleep" or (
        meditation_kind is None and bool(SLEEP.search(sleep_constraints))
    )
    if posture is None:
        if re.search(r"\b(?:lying|lie|lay)\s+(?:down|in bed)|\breclining\b", prompt, re.I):
            posture = "lying_down"
        elif re.search(r"\b(?:sitting|seated|sit|in (?:my|a|the) chair)\b", prompt, re.I):
            posture = "sitting"
        else:
            posture = "lying_down" if sleep else "sitting"
    excluded = bool(NO_BREATH.search(breath_constraints))
    if excluded or re.search(r"\b(?:sound|listen|noise|overstimulat|panic)\w*\b", prompt, re.I):
        anchor: Anchor = "sound"
    elif re.search(r"\b(?:body|contact|ground|support)\w*\b", prompt, re.I):
        anchor = "contact"
    else:
        anchor = "natural_breath"
    if sleep:
        recipe_id = (
            "good-night-reflection"
            if duration_seconds >= 180 and REFLECTION.search(prompt)
            else "sleep-settling"
        )
        kind: MeditationKind = "sleep"
        practice = "An ordered, neutral body scan connected to the chosen support anchor."
    else:
        recipe_id = "daytime-steadying"
        kind = meditation_kind or "grounding"
        if meditation_kind is None:
            if REFLECTION.search(prompt):
                kind = "reflection"
            elif re.search(
                r"\b(?:setback|self.critic|compassion|failed|failure|harsh)\w*\b", prompt, re.I
            ):
                kind = "self_compassion"
            elif re.search(r"\b(?:anxious|anxiety|thought|worr|ruminat)\w*\b", prompt, re.I):
                kind = "noting"
            elif re.search(r"\b(?:focus|meeting|concentrat|attention)\w*\b", prompt, re.I):
                kind = "focused_attention"
            elif re.search(r"\bbody.scan\b", prompt, re.I):
                kind = "body_scan"
        practices = {
            "reflection": (
                "Recall one bounded ordinary event; ask one genuine question, then release it."
            ),
            "self_compassion": (
                "Acknowledge one difficulty; offer one modest optional phrase of kindness."
            ),
            "noting": (
                "Notice one thought, optionally name it thinking, and return to the same anchor."
            ),
            "focused_attention": (
                "Rehearse returning to one concrete anchor; let the next real task wait."
            ),
            "body_scan": (
                "Notice body areas in one ordered neutral sequence without demanding relaxation."
            ),
            "grounding": (
                "Explore the selected anchor as it is, returning to that same anchor after "
                "wandering."
            ),
            "breath_awareness": (
                "Observe natural breathing without controlling it, or use the chosen external "
                "anchor when breathing is excluded."
            ),
            "loving_kindness": "Offer one or two optional well-wishes without demanding a feeling.",
            "visualization": (
                "Imagine one simple scene while staying connected to the chosen anchor."
            ),
            "open_awareness": (
                "Notice sound, sensation, and thought without selecting or changing any of them."
            ),
        }
        practice = practices[kind]
    recipe = next(
        item for item in (library or load_recipe_library()).recipes if item.id == recipe_id
    )
    return RoutedSession(
        recipe=recipe,
        posture=posture,
        anchor=anchor,
        kind=kind,
        main_practice=practice,
        breath_excluded=excluded,
    )


class ParagraphSlot(BaseModel):
    """One bounded prose-and-quiet interval; actual words determine its exact quiet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    duration_ms: int
    minimum_words: int
    maximum_words: int
    maximum_quiet_ms: int


def allocate_slots(
    session: RoutedSession, duration_seconds: int, words_per_minute: int
) -> list[list[ParagraphSlot]]:
    weights = [beat.weight for beat in session.recipe.beats]
    remaining = duration_seconds * 1_000
    beats: list[list[ParagraphSlot]] = []
    for index, weight in enumerate(weights):
        beat_ms = (
            remaining
            if index == len(weights) - 1
            else round(duration_seconds * 1_000 * weight / sum(weights))
        )
        remaining -= beat_ms
        maximum_quiet = (
            22_000
            if session.recipe.ending == "sleep_taper" and index >= len(weights) - 2
            else 16_000
        )
        # Keep guidance brief at the selected speech rate; longer beats gain
        # practice invitations rather than requiring longer paragraphs.
        cycle_ms = min(35_000, maximum_quiet + 24 * 60_000 / words_per_minute)
        count = max(1, math.ceil(beat_ms / cycle_ms))
        slot_ms = [beat_ms // count] * count
        slot_ms[-1] += beat_ms - sum(slot_ms)
        beats.append(
            [
                ParagraphSlot(
                    duration_ms=duration,
                    minimum_words=max(
                        4, math.ceil((duration - maximum_quiet) * words_per_minute / 60_000)
                    ),
                    maximum_words=max(
                        4, math.floor((duration - 1_200) * words_per_minute / 60_000)
                    ),
                    maximum_quiet_ms=maximum_quiet,
                )
                for duration in slot_ms
            ]
        )
    return beats
