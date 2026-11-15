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
    excluded_anchors: list[Anchor] = Field(default_factory=list)
    body_scan_excluded: bool = False
    imagery_excluded: bool = False


NO_BREATH = re.compile(
    r"\b(?:no|without|avoid|skip)\b[^.!?\n]{0,35}\b(?:breath|breathing)\b|"
    r"\b(?:don['\u2019]t|do not|rather not)\b[^.!?\n]{0,35}\b(?:breath|breathing)\b|"
    r"\b(?:breath|breathing)\b[^.!?\n]{0,30}\b(?:uncomfortable|panic|trigger|worse)\w*\b",
    re.I,
)
SLEEP = re.compile(
    r"\b(?:sleep|sleeping|bedtime|good[\s-]?night|fall asleep|drift(?:ing)? off|insomnia)\b",
    re.I,
)
INTENT_NEGATION = (
    r"\b(?:no|not|without|avoid|skip|(?:do|is|are|should|must)n['\u2019]t|"
    r"(?:do|is|are|should|must) not|rather not)\b"
    r"(?:(?!\b(?:but|however|yet|instead)\b)[^.!?;\n])*"
)
NEGATED_SLEEP = re.compile(rf"{INTENT_NEGATION}{SLEEP.pattern}", re.I)
SLEEP_DIFFICULTY = re.compile(
    r"^\s*not\s+(?:sleeping\b|"
    r"getting(?:\s+[\w'\u2019-]+){0,4}\s+sleep\b)|"
    r"\b(?:could|can|did|was|were)\s+not\b|"
    r"\b(?:(?:am|is|are)\s+not|(?:is|are)n['\u2019]t|"
    r"(?:i['\u2019]m|(?:we|they|you)['\u2019]re|(?:he|she)['\u2019]s)\s+not)\s+"
    r"(?:sleeping|getting(?:\s+[\w'\u2019-]+){0,4}\s+sleep)\b",
    re.I,
)
SLEEP_INTENT_SCOPE = re.compile(
    r"\b(?:during|throughout|for|in)\s+(?:(?:this|the|my|our|a)\s+)?"
    r"(?:(?!(?:and|or|but|however|yet|instead)\b)[\w'\u2019-]+\s+){0,4}"
    r"(?:meditation|session|meeting)\b|"
    r"\b(?:goal|aim|intention|preference)\s+(?:is|are|isn['\u2019]t|aren['\u2019]t)\b|"
    r"\b(?:is|are)\s+(?:(?:my|our|the)\s+)?(?:goal|aim|intention|preference)\b",
    re.I,
)
AWAKE = re.compile(r"\b(?:(?:stay|remain)|keep\s+(?:me|us))\s+awake\b", re.I)
NEGATED_AWAKE = re.compile(rf"{INTENT_NEGATION}{AWAKE.pattern}", re.I)
BOX_BREATHING = re.compile(r"\b(?:box|square)\s+breathing\b", re.I)
NEGATED_BOX_BREATHING = re.compile(
    r"\b(?:no|without|avoid|skip|don['\u2019]t|do not|rather not)\b"
    r"(?:(?!\b(?:but|however|yet|instead)\b)[^.!?;\n]){0,35}"
    r"\b(?:box|square)\s+breathing\b",
    re.I,
)
REQUEST_SUBJECT_CLAUSE = (
    r"(?:"
    r"(?:i|you|we|he|she|they|it)\b|"
    r"(?:(?!(?:and|or|but|however|yet|instead)\b)[a-z][\w'\u2019-]*\s+){1,6}"
    r"(?:am|is|are|was|were|can|could|will|would|shall|should|must|may|might|"
    r"has|have|does|do|wants?|needs?|aims?|seeks?|prefers?|includes?)\b)"
)
REQUEST_CLAUSE_BOUNDARY = re.compile(
    rf",(?=\s*{REQUEST_SUBJECT_CLAUSE})|"
    rf"[.!?;\n]+|,(?!\s*(?:(?:and|or|breath|breathing|box|square)\b|{SLEEP.pattern}))|"
    r"\b(?:but|however|yet|instead)\b|"
    rf"\b(?:and|or)\s+(?=(?:please\s+)?{REQUEST_SUBJECT_CLAUSE})|"
    r"\band\s+(?=(?:please\s+)?(?:use|try|include|allow|focus|concentrate|guide|help)\b)",
    re.I,
)
REFLECTION = re.compile(r"\b(?:reflect\w*|review(?:ing)? (?:the|my) day)\b", re.I)
BODY_SCAN = re.compile(r"\bbody[ -]scan(?:s|ning)?\b|\bscan(?:ning)? (?:my|the|your) body\b", re.I)
IMAGERY = re.compile(r"\b(?:visuali[sz]\w*|imagery|imagin\w*|fantasy|stories|story)\b", re.I)
VISUALIZATION_REQUEST = re.compile(
    r"\b(?:visuali[sz]ation|guided imagery|with imagery)\b|"
    r"\b(?:help me|let me|want to|like to)\s+(?:imagine|visuali[sz]e)\b|"
    r"^\s*(?:please\s+)?(?:imagine|visuali[sz]e)\b",
    re.I,
)
ANCHOR_CUES: dict[Anchor, re.Pattern[str]] = {
    "sound": re.compile(r"\b(?:sounds?|noise|listen(?:ing)?|hear(?:ing)?|auditory)\b", re.I),
    "contact": re.compile(r"\b(?:contact|touch|tactile)\b", re.I),
    "natural_breath": re.compile(r"\b(?:breath\w*|respiration|inhal\w*|exhal\w*)\b", re.I),
}
PREFERENCE_NEGATION = (
    r"\b(?:no|not|without|avoid|skip|never|don['\u2019]t|do not|rather not|"
    r"cannot|can['\u2019]t)\b"
)
PREFERENCE_RESTART = r"\b(?:use|try|include|allow|choose|prefer|with|focus on)\b"
PREFERENCE_CLAUSE_BOUNDARY = re.compile(
    rf"[.!?;\n]+|\b(?:but|however|yet|instead)\b|"
    rf",(?=\s*(?:{REQUEST_SUBJECT_CLAUSE}|(?:please\s+)?{PREFERENCE_RESTART}))|"
    rf"\band\s+(?=(?:{REQUEST_SUBJECT_CLAUSE}|(?:please\s+)?{PREFERENCE_RESTART}))",
    re.I,
)
BREATH_MODIFICATION_EXCLUSION = re.compile(
    rf"{PREFERENCE_NEGATION}\s+"
    r"(?:prescrib\w*|control\w*|chang\w*|alter\w*|regulat\w*|forc\w*|pac\w*)\b|"
    rf"{PREFERENCE_NEGATION}\s+(?:(?:any|the)\s+)?"
    r"(?:breath\s+holds?|breath\s+counting|paced\s+breathing|deep\s+breathing)\b",
    re.I,
)
BREATH_ATTENTION = re.compile(r"\b(?:focus|attention|observ\w*|awareness|guide)\b", re.I)
BREATH_MODIFICATION = re.compile(
    r"\bbreath\s+(?:holds?|counting|control)\b|"
    r"\b(?:paced|deep|controlled)\s+breathing\b|"
    r"\b(?:prescrib\w*|control\w*|chang\w*|alter\w*|regulat\w*|forc\w*|pac\w*)\s+"
    r"(?:(?!(?:attention|awareness|focus)\b)[\w]+\s+){0,3}breath\w*\b",
    re.I,
)


def _anchor_priority(clause: str, cue: re.Pattern[str]) -> int:
    """Prefer an instruction's object over incidental sensory context.

    Ties retain the existing deterministic order; this is a bounded language
    contract, not a semantic parser for arbitrary prose.
    """
    mentions = sorted(
        (match for pattern in ANCHOR_CUES.values() for match in pattern.finditer(clause)),
        key=lambda match: match.start(),
    )
    priority = 0
    for index, match in enumerate(mentions):
        if not cue.fullmatch(match.group()):
            continue
        start = mentions[index - 1].end() if index else 0
        end = mentions[index + 1].start() if index + 1 < len(mentions) else len(clause)
        prefix, suffix = clause[start : match.start()], clause[match.end() : end]
        # A role before/after a conjunction belongs to its own sensory object:
        # "contact and background sound" does not make contact background.
        role_boundary = r"\b(?:and|or|while|whereas|then)\b"
        role_prefix = re.split(role_boundary, prefix, flags=re.I)[-1]
        role_suffix = re.split(role_boundary, suffix, flags=re.I)[0]
        # Background/orientation is permitted experience, not a primary practice.
        if re.search(r"\b(?:background|incidental|orientation)\b", role_prefix + role_suffix, re.I):
            continue
        if re.fullmatch(r"\s+(?:and|or)\s+(?:\w+\s+){0,2}", prefix, re.I):
            # Coordinated objects share their instruction: "use contact and
            # ordinary sound" remains an ambiguous request, not a new priority.
            prefix = clause[: match.start()]
        if re.search(
            r"\b(?:primary|main|sole|single)\b|\bone\s+(?:simple\s+)?$", role_prefix, re.I
        ) or re.search(
            r"\b(?:primary|main|sole|single)\s+(?:anchor|practice|focus)\b", role_suffix, re.I
        ):
            priority = max(priority, 3)
        elif re.search(
            r"\b(?:use|using|choose|prefer|observe|notice|explore|"
            r"focus(?:ing)?\s+on|attention\s+to|awareness\s+of)\b",
            prefix,
            re.I,
        ):
            priority = max(priority, 2)
        else:
            priority = max(priority, 1)
    return priority


def _preference(clauses: list[str], cue: re.Pattern[str]) -> tuple[bool, bool]:
    """Resolve bounded explicit exclusions before affirmative mentions.

    A new instruction ends the scope of an earlier negative. This is deliberately
    a small language contract, not a claim to understand arbitrary user prose.
    """

    requested = excluded = False
    for clause in clauses:
        for match in cue.finditer(clause):
            prefix = clause[: match.start()]
            negative = list(re.finditer(PREFERENCE_NEGATION, prefix, re.I))
            negated = False
            if negative:
                tail = prefix[negative[-1].end() :]
                # "do not want to use" remains negative; "no breath and use
                # contact" starts a separate affirmative instruction.
                restarts = re.search(r"(?:\band\s+|,\s*)" + PREFERENCE_RESTART, tail, re.I)
                negated = restarts is None
            suffix = clause[match.end() :]
            unwanted = re.match(
                r"\s+(?:(?:focus|attention)\s+)?(?:is|are|feels?|makes? me)\s+"
                r"(?:too\s+)?(?:uncomfortable|unwelcome|overwhelming|anxious|worse)",
                suffix,
                re.I,
            )
            if negated or unwanted:
                excluded = True
            else:
                requested = True
    return requested and not excluded, excluded


def _requested_anchor(prompt: str) -> tuple[Anchor, list[Anchor]]:
    # Quoted examples/reported instructions are context. Quoted direct requests
    # (for example, use "contact") remain requests.
    prompt = re.sub(
        r'\b(?:phrase|example|said|says|wrote)\s*:?\s*["“][^"”]*["”]',
        "",
        prompt,
        flags=re.I,
    )
    clauses = PREFERENCE_CLAUSE_BOUNDARY.split(prompt)
    primary_clauses = [
        clause
        for clause in clauses
        if not re.search(r"\bas\s+(?:an?\s+)?(?:alternative|fallback)\b", clause, re.I)
        and not re.search(
            r"^\s*(?:if|when)\b.*\b(?:uncomfortable|unwelcome|overwhelming|"
            r"difficult|worse)\b",
            clause,
            re.I,
        )
    ]
    breath_clauses = []
    for clause in primary_clauses:
        clause = BOX_BREATHING.sub("", clause)
        # Excluding prescribed rhythm/holds does not exclude observing natural
        # breathing. A mixed prohibition that also names attention still applies.
        if BREATH_MODIFICATION_EXCLUSION.search(clause) and not BREATH_ATTENTION.search(clause):
            continue
        if re.search(PREFERENCE_NEGATION, clause, re.I):
            clause = BREATH_MODIFICATION.sub("", clause)
        breath_clauses.append(clause)
    preferences = {
        anchor: _preference(
            breath_clauses if anchor == "natural_breath" else primary_clauses,
            cue,
        )
        for anchor, cue in ANCHOR_CUES.items()
    }
    excluded = {anchor for anchor, (_, negative) in preferences.items() if negative}
    if any(NO_BREATH.search(clause) for clause in breath_clauses):
        excluded.add("natural_breath")
    priorities = {
        anchor: max(
            (
                _anchor_priority(clause, ANCHOR_CUES[anchor])
                for clause in (breath_clauses if anchor == "natural_breath" else primary_clauses)
                if _preference([clause], ANCHOR_CUES[anchor])[0]
            ),
            default=0,
        )
        for anchor, (requested, _) in preferences.items()
        if requested and anchor not in excluded
    }
    if priorities and max(priorities.values()) > 0:
        return max(priorities, key=lambda anchor: priorities[anchor]), sorted(excluded)
    if "natural_breath" in excluded or re.search(
        r"\b(?:noise|overstimulat|panic)\w*\b", prompt, re.I
    ):
        defaults: tuple[Anchor, ...] = ("sound", "contact", "natural_breath")
    elif re.search(r"\b(?:body|ground|support)\w*\b", prompt, re.I):
        defaults = ("contact", "sound", "natural_breath")
    else:
        defaults = ("natural_breath", "contact", "sound")
    for anchor in defaults:
        if anchor not in excluded:
            return anchor, sorted(excluded)
    raise ValueError("All supported anchors are excluded; allow natural breath, sound or contact.")


POSTURE_CUES: dict[Posture, re.Pattern[str]] = {
    "lying_down": re.compile(
        r"\b(?:(?:lying|lie|lay)(?:\s+(?:down|(?:in|on)\s+(?:(?:my|a|the)\s+)?"
        r"(?:bed|mattress|floor|mat)))?|reclining)\b",
        re.I,
    ),
    "sitting": re.compile(
        r"\b(?:(?:sitting|seated|sit)(?:\s+(?:in|on)\s+(?:(?:my|a|the)\s+)?"
        r"(?:chair|seat|floor))?)\b",
        re.I,
    ),
}
POSTURE_ACTION = "(?:" + "|".join(cue.pattern for cue in POSTURE_CUES.values()) + ")"
NEGATED_POSTURE = re.compile(
    r"\b(?:no|not|without|avoid|skip|never|don['\u2019]t|do not|cannot|can['\u2019]t)\s+"
    r"(?:while\s+)?(?:(?:(?:want|need|have)\s+)?to\s+)?(?:be\s+)?"
    rf"{POSTURE_ACTION}(?:\s+(?:or|and)\s+(?:to\s+)?{POSTURE_ACTION})*",
    re.I,
)


def _requests_sleep(clauses: list[str]) -> bool:
    affirmative_sleep = any(
        SLEEP.search(clause) and not NEGATED_SLEEP.search(clause) for clause in clauses
    )
    sleep = False
    for clause in clauses:
        intent_clause = (
            clause
            if SLEEP_INTENT_SCOPE.search(clause)
            or (not affirmative_sleep and re.fullmatch(r"\s*not\s+sleeping\s*", clause, re.I))
            else SLEEP_DIFFICULTY.sub("", clause)
        )
        if NEGATED_SLEEP.search(intent_clause):
            return False
        if AWAKE.search(NEGATED_AWAKE.sub("", intent_clause)):
            return False
        sleep = sleep or SLEEP.search(clause) is not None
    return sleep


def _requested_posture(clauses: list[str], *, sleep: bool) -> Posture:
    excluded: set[Posture] = set()
    requested: set[Posture] = set()
    for clause in clauses:
        negated = [match.span() for match in NEGATED_POSTURE.finditer(clause)]
        for posture, cue in POSTURE_CUES.items():
            for match in cue.finditer(clause):
                if any(start <= match.start() < end for start, end in negated):
                    excluded.add(posture)
                else:
                    requested.add(posture)
    for posture in POSTURE_CUES:
        if posture in requested and posture not in excluded:
            return posture
    default: Posture = "lying_down" if sleep else "sitting"
    alternative: Posture = "sitting" if sleep else "lying_down"
    for posture in (default, alternative):
        if posture not in excluded:
            return posture
    raise ValueError("Both supported postures are excluded; choose sitting or lying down.")


def route_session(
    prompt: str,
    duration_seconds: int,
    *,
    posture: Posture | None = None,
    meditation_kind: MeditationKind | None = None,
    library: RecipeLibrary | None = None,
) -> RoutedSession:
    """Use need and explicit constraints before selecting the hidden grammar."""

    clauses = REQUEST_CLAUSE_BOUNDARY.split(prompt)
    if any(BOX_BREATHING.search(NEGATED_BOX_BREATHING.sub("", clause)) for clause in clauses):
        raise ValueError(
            "Box breathing is not supported by a reviewed holding protocol. "
            "Request natural breathing or gentle no-hold breathing instead."
        )
    sleep = meditation_kind == "sleep" or (meditation_kind is None and _requests_sleep(clauses))
    if posture is None:
        posture = _requested_posture(clauses, sleep=sleep)
    preference_clauses = PREFERENCE_CLAUSE_BOUNDARY.split(prompt)
    anchor, excluded_anchors = _requested_anchor(prompt)
    reflection, reflection_excluded = _preference(preference_clauses, REFLECTION)
    body_scan, body_scan_excluded = _preference(preference_clauses, BODY_SCAN)
    _, imagery_excluded = _preference(preference_clauses, IMAGERY)
    visualization, _ = _preference(preference_clauses, VISUALIZATION_REQUEST)
    if meditation_kind == "reflection" and reflection_excluded:
        raise ValueError(
            "Reflection is selected but excluded by the request; choose another practice."
        )
    if meditation_kind == "body_scan" and body_scan_excluded:
        raise ValueError(
            "Body scan is selected but excluded by the request; choose another practice."
        )
    if meditation_kind == "visualization" and imagery_excluded:
        raise ValueError(
            "Visualization is selected but imagery is excluded; choose another practice."
        )
    if sleep:
        recipe_id = (
            "good-night-reflection" if duration_seconds >= 180 and reflection else "sleep-settling"
        )
        kind: MeditationKind = "sleep"
        practice = "An ordered, neutral body scan connected to the chosen support anchor."
        if body_scan_excluded:
            recipe_id = (
                "good-night-anchor-reflection"
                if recipe_id == "good-night-reflection"
                else "sleep-anchor-rest"
            )
            practice = "Rest with the selected anchor, reducing effort without scanning body areas."
    else:
        recipe_id = "daytime-steadying"
        kind = meditation_kind or "grounding"
        if meditation_kind is None:
            if reflection:
                kind = "reflection"
            elif body_scan:
                kind = "body_scan"
            elif visualization and not imagery_excluded:
                kind = "visualization"
            elif re.search(
                r"\b(?:setback|self.critic|compassion|failed|failure|harsh)\w*\b", prompt, re.I
            ):
                kind = "self_compassion"
            elif re.search(r"\b(?:anxious|anxiety|thought|worr|ruminat)\w*\b", prompt, re.I):
                kind = "noting"
            elif re.search(r"\b(?:focus|meeting|concentrat|attention)\w*\b", prompt, re.I):
                kind = "focused_attention"
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
                "After settling with the anchor, optionally imagine one simple original scene. "
                "Keep it low-stakes, without plot or a required vivid image; return to the "
                "scene after wandering, then release it before reorientation."
            ),
            "open_awareness": (
                "After settling with the anchor, broaden attention to permitted sensations "
                "and thoughts as they arise, without selecting or changing them. The anchor "
                "is an optional way to settle again, not a competing task."
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
        breath_excluded="natural_breath" in excluded_anchors,
        excluded_anchors=excluded_anchors,
        body_scan_excluded=body_scan_excluded,
        imagery_excluded=imagery_excluded,
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
