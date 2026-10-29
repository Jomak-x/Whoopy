"""Plan-first, schema-validated local meditation generation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from whoopy.meditation.blueprints import BlueprintLibrary, load_blueprint_library
from whoopy.meditation.editorial import (
    EditorialReport,
    apply_reviewed_ending_contract,
    apply_reviewed_style_contract,
    apply_reviewed_technique_contract,
    review_script,
)
from whoopy.meditation.models import (
    DeliveryIntent,
    DraftedSection,
    MeditationKind,
    MeditationPlan,
    PlannedSection,
    ProposedDraft,
    ProposedPlan,
    RawGenerationAttempt,
)
from whoopy.meditation.pacing import (
    MAX_SENTENCE_WORDS,
    estimated_seconds_per_word,
    pace_section,
    sentence_word_count,
    split_sentences,
)
from whoopy.meditation.prompts import PromptBundle
from whoopy.meditation.recipes import SLEEP
from whoopy.meditation.safety import ContentSafetyError, validate_meditation_text
from whoopy.ports import ScriptGenerationRequest, ScriptGenerator
from whoopy.timeline import SilenceSegment, Timeline, build_script_timeline

WORD_PATTERN = re.compile(r"\b[\w'-]+\b")
MEDITATION_KIND_PATTERNS: tuple[tuple[MeditationKind, re.Pattern[str]], ...] = (
    ("sleep", SLEEP),
    ("body_scan", re.compile(r"\bbody[\s-]?scan\b", re.I)),
    ("self_compassion", re.compile(r"\bself[\s-]?compassion\b", re.I)),
    ("loving_kindness", re.compile(r"\b(?:loving[\s-]?kindness|goodwill)\b", re.I)),
    ("visualization", re.compile(r"\b(?:visuali[sz]|imagine|imagery)\w*\b", re.I)),
    ("reflection", re.compile(r"\b(?:reflect|reflection|review the day)\b", re.I)),
    ("noting", re.compile(r"\bnoting\b", re.I)),
    ("open_awareness", re.compile(r"\b(?:open|resting) awareness\b", re.I)),
    ("breath_awareness", re.compile(r"\b(?:breath|breathing)\b", re.I)),
    ("grounding", re.compile(r"\b(?:ground|settle|busy day|stress)\w*\b", re.I)),
)
PAUSE_INTENT_WEIGHTS = {
    "instruction": 4,
    "embodied": 8,
    "reflection": 12,
    "practice": 18,
    "extended": 30,
}
TECHNIQUE_DELIVERY: dict[str, DeliveryIntent] = {
    "arrival": "warm",
    "body_scan": "grounded",
    "focused_attention": "calm",
    "loving_kindness": "compassionate",
    "noting": "calm",
    "reflection": "warm",
    "resting_awareness": "soothing",
    "return": "encouraging",
    "sleep_transition": "sleepy",
    "visualization": "soothing",
}
TECHNIQUE_GUIDANCE = {
    "arrival": (
        "Orient to the present through contact, sound, or posture. Give one concrete "
        "choice for comfort; do not promise relaxation."
    ),
    "body_scan": (
        "Move in a clear sequence through two to four body areas. Invite neutral "
        "observation of pressure, temperature, tension, or ease without forcing change."
    ),
    "focused_attention": (
        "Choose one physical anchor such as natural breathing or contact with the chair. "
        "Acknowledge wandering once and show how to return without judgment."
    ),
    "loving_kindness": (
        "Offer one or two simple, secular well-wishes. Keep them invitational and do not "
        "claim that the listener already feels them."
    ),
    "noting": (
        "Invite the listener to notice a thought, feeling, or sound; give it a simple "
        "mental label; then return attention to a physical anchor."
    ),
    "reflection": (
        "Ask one clear, gentle question connected to the user's intention. Do not answer "
        "it for the listener; leave space for their own response."
    ),
    "resting_awareness": (
        "Release the narrow anchor and notice the whole field of sound, sensation, and "
        "thought without selecting or changing anything."
    ),
    "return": (
        "Reorient through sounds, contact, and a small optional movement. Close simply "
        "without motivational slogans or a new exercise."
    ),
    "sleep_transition": (
        "Let the guidance taper toward quiet rest. Do not reorient to the room, ask the "
        "listener to open their eyes, or promise that they will wake refreshed."
    ),
    "visualization": (
        "Develop one coherent, concrete image with no more than three sensory details. "
        "Connect the image to the user's intention instead of adding decorative poetry."
    ),
}
CONTENT_STOP_WORDS = {
    "a",
    "and",
    "attention",
    "bring",
    "gently",
    "guide",
    "in",
    "of",
    "on",
    "the",
    "to",
    "your",
}
ValidatedT = TypeVar("ValidatedT")
WritingMethod = Literal[
    "plan_first",
    "blueprint_first_sparse",
    "three_candidate_structural_selection",
    "recipe_whole_session",
]

if TYPE_CHECKING:
    from whoopy.meditation.workspace import GenerationWorkspace


class GenerationError(RuntimeError):
    """Raised after bounded model-output repair has been exhausted."""


class MeditationGenerationResult(BaseModel):
    """Every validated output needed by the later audio workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    plan: MeditationPlan
    sections: list[DraftedSection]
    script: str
    timeline: Timeline
    estimated_duration_seconds: float = Field(gt=0)
    editorial: EditorialReport | None = None
    raw_attempts: list[RawGenerationAttempt]
    writing_method: WritingMethod = "plan_first"


def _fit_script_to_requested_duration(
    script: str,
    plan: MeditationPlan,
    *,
    spoken_seconds: float,
    requested_seconds: int,
) -> tuple[str, MeditationPlan]:
    lines = script.splitlines()
    pauses: dict[int, int] = {}
    section_ends: list[int] = []
    for index, line in enumerate(lines):
        if line.startswith("[delivery:") and pauses:
            section_ends.append(next(reversed(pauses)))
        match = re.fullmatch(r"\[pause: (\d+)ms\]", line)
        if match is not None:
            pauses[index] = int(match.group(1))
    if not pauses:
        return script, plan
    section_ends.append(next(reversed(pauses)))
    section_end_set = set(section_ends)
    target_silence_ms = max(1, round((requested_seconds - spoken_seconds) * 1_000))
    delta = target_silence_ms - sum(pauses.values())
    if delta >= 0:
        remaining = delta
        for index in reversed(pauses):
            maximum = 120_000 if index in section_end_set else 600_000
            addition = min(maximum - pauses[index], remaining)
            pauses[index] += addition
            remaining -= addition
            if remaining == 0:
                break
    else:
        remaining = -delta
        for index in sorted(pauses, key=lambda item: pauses[item], reverse=True):
            minimum = 1_000 if index in section_end_set else 250
            reduction = min(max(0, pauses[index] - minimum), remaining)
            pauses[index] -= reduction
            remaining -= reduction
            if remaining == 0:
                break
    for index, duration in pauses.items():
        lines[index] = f"[pause: {duration}ms]"
    sections = [
        section.model_copy(update={"pause_after_ms": pauses[index]})
        for section, index in zip(plan.sections, section_ends, strict=True)
    ]
    return "\n".join(lines) + "\n", plan.model_copy(update={"sections": sections})


def _reallocate_section_pauses_for_actual_prose(
    plan: MeditationPlan,
    preliminary_timeline: Timeline,
    *,
    spoken_seconds: float,
) -> MeditationPlan:
    """Put any post-draft quiet back into its intended practice beats.

    Drafts may validly land anywhere inside each section's word range. The old
    final-duration repair put the resulting difference into the last timeline
    silence, which could leave a daytime listener sitting through a long quiet
    block *after* reorientation. Recompute only section-ending pauses from the
    actual prose and the reviewed pause intents; sentence-level rests remain
    untouched.
    """

    current_section_ms = sum(section.pause_after_ms for section in plan.sections)
    current_total_ms = sum(
        segment.duration_ms
        for segment in preliminary_timeline.segments
        if isinstance(segment, SilenceSegment)
    )
    sentence_rest_ms = max(0, current_total_ms - current_section_ms)
    target_total_ms = max(
        len(plan.sections) * 1_000,
        round((plan.requested_duration_seconds - spoken_seconds) * 1_000),
    )
    section_budget_ms = max(
        len(plan.sections) * 1_000,
        target_total_ms - sentence_rest_ms,
    )
    remaining = section_budget_ms - len(plan.sections) * 1_000
    weights = [PAUSE_INTENT_WEIGHTS[section.pause_intent] for section in plan.sections]
    weight_total = sum(weights)
    raw_additions = [remaining * weight / weight_total for weight in weights]
    additions = [int(value) for value in raw_additions]
    undistributed = remaining - sum(additions)
    remainder_order = sorted(
        range(len(raw_additions)),
        key=lambda index: raw_additions[index] - additions[index],
        reverse=True,
    )
    for index in remainder_order[:undistributed]:
        additions[index] += 1
    pauses = _cap_and_redistribute_pauses([1_000 + addition for addition in additions])
    sections = [
        section.model_copy(update={"pause_after_ms": pause_ms})
        for section, pause_ms in zip(plan.sections, pauses, strict=True)
    ]
    return plan.model_copy(
        update={
            "sections": sections,
            "silence_budget_seconds": round((sentence_rest_ms + sum(pauses)) / 1_000),
        }
    )


def _content_words(text: str) -> set[str]:
    return {
        word.lower()
        for word in WORD_PATTERN.findall(text)
        if len(word) > 2 and word.lower() not in CONTENT_STOP_WORDS
    }


def _compact_plan_for_duration(
    proposed: ProposedPlan,
    *,
    prompt: str,
    duration_seconds: int,
) -> ProposedPlan:
    """Keep short practices focused on the user's central intention."""

    if duration_seconds <= 180:
        maximum_sections = 3
    elif duration_seconds <= 600:
        maximum_sections = 4
    elif duration_seconds <= 1_200:
        maximum_sections = 5
    else:
        maximum_sections = 6
    if len(proposed.sections) <= maximum_sections:
        return proposed

    requested_words = _content_words(prompt)
    middle = list(enumerate(proposed.sections[1:-1], start=1))
    ranked = sorted(
        middle,
        key=lambda item: (
            len(requested_words & _content_words(f"{item[1].title} {item[1].purpose}")),
            item[1].weight,
            -item[0],
        ),
        reverse=True,
    )
    selected_indexes = {0, len(proposed.sections) - 1}
    selected_indexes.update(index for index, _section in ranked[: maximum_sections - 2])
    return proposed.model_copy(
        update={
            "sections": [
                section
                for index, section in enumerate(proposed.sections)
                if index in selected_indexes
            ]
        }
    )


def _fit_complete_sentences(
    text: str,
    *,
    purpose: str,
    minimum_words: int,
    maximum_words: int,
    keep_first_sentence: bool = False,
    maximum_sentence_words: int | None = None,
) -> str:
    """Select complete, purpose-relevant sentences within the time budget.

    Small local models sometimes obey sentence-style instructions but ignore a
    tight total word range. Selecting at reviewed sentence boundaries is safer
    than accepting an overrun and more useful than blindly keeping an unrelated
    introduction.
    """

    purpose_words = _content_words(purpose)
    sentences = split_sentences(text)
    original_word_count = len(WORD_PATTERN.findall(text))
    overlong_sentences = [
        sentence
        for sentence in sentences
        if maximum_sentence_words is not None
        and sentence_word_count(sentence) > maximum_sentence_words
    ]
    if maximum_sentence_words is not None:
        sentences = [
            sentence
            for sentence in sentences
            if sentence_word_count(sentence) <= maximum_sentence_words
        ]
    selected: list[tuple[int, str]] = []
    count = 0
    candidates = list(enumerate(sentences))
    if keep_first_sentence and candidates:
        first = candidates.pop(0)
        first_words = sentence_word_count(first[1])
        if first_words > maximum_words:
            raise ValueError(
                f"required first sentence has {first_words} words; maximum is {maximum_words}"
            )
        selected.append(first)
        count = first_words
    ranked = sorted(
        candidates,
        key=lambda item: (
            len(purpose_words & _content_words(item[1])),
            -item[0],
        ),
        reverse=True,
    )
    for index, sentence in ranked:
        sentence_words = sentence_word_count(sentence)
        if count + sentence_words > maximum_words:
            continue
        selected.append((index, sentence))
        count += sentence_words
    selected.sort()
    if count < minimum_words:
        sentence_detail = (
            f" using sentences of at most {maximum_sentence_words} words"
            if maximum_sentence_words is not None
            else ""
        )
        excluded_detail = (
            f"; excluded {len(overlong_sentences)} overlong sentence(s); "
            f"original response contained {original_word_count} words"
            if overlong_sentences
            else f"; original response contained {original_word_count} words"
        )
        raise ValueError(
            f"complete sentences{sentence_detail} provide {count} usable words; expected "
            f"{minimum_words}-{maximum_words}{excluded_detail}"
        )
    return " ".join(sentence for _index, sentence in selected)


def _repair_overlong_sentence_boundaries(
    text: str,
    *,
    maximum_sentence_words: int = MAX_SENTENCE_WORDS,
) -> str:
    """Turn safe model-written clause lists into short complete sentences.

    Small local models sometimes return one useful comma-spliced sentence even
    after being told the sentence limit in two repair prompts. This repair is
    deliberately narrow: it only replaces existing comma, semicolon, colon, or
    dash boundaries. It never invents or removes meditation instructions, and
    the normal safety, word-budget, technique, and editorial validators still
    run on the result.
    """

    def finish(fragment: str) -> str:
        normalized = fragment.strip().rstrip(",;:\u2014\u2013 ")
        normalized = re.sub(r"^(?:and|but|or|then)\s+", "", normalized, flags=re.I)
        if not normalized:
            return ""
        first_alpha = next(
            (index for index, character in enumerate(normalized) if character.isalpha()),
            None,
        )
        if first_alpha is not None:
            normalized = (
                normalized[:first_alpha]
                + normalized[first_alpha].upper()
                + normalized[first_alpha + 1 :]
            )
        if not re.search(r"[.!?][\"')\]]?$", normalized):
            normalized += "."
        return normalized

    repaired: list[str] = []
    for sentence in split_sentences(text):
        if sentence_word_count(sentence) <= maximum_sentence_words:
            repaired.append(sentence)
            continue
        raw_fragments = re.split(r"\s*(?:[,;:]|[\u2014\u2013])\s+", sentence)
        merged_fragments: list[str] = []
        index = 0
        while index < len(raw_fragments):
            fragment = raw_fragments[index]
            if (
                re.match(r"^(?:if|when|while|unless|although|because)\b", fragment, re.I)
                and index + 1 < len(raw_fragments)
                and sentence_word_count(f"{fragment}, {raw_fragments[index + 1]}")
                <= maximum_sentence_words
            ):
                fragment = f"{fragment}, {raw_fragments[index + 1]}"
                index += 1
            merged_fragments.append(fragment)
            index += 1
        fragments = [finish(fragment) for fragment in merged_fragments]
        fragments = [fragment for fragment in fragments if fragment]
        if len(fragments) < 2 or any(
            sentence_word_count(fragment) > maximum_sentence_words for fragment in fragments
        ):
            repaired.append(sentence)
            continue
        repaired.extend(fragments)
    return " ".join(repaired)


def _json_object(text: str) -> dict[str, Any]:
    """Extract exactly one JSON object, tolerating only a surrounding code fence."""

    normalized = text.strip()
    if normalized.startswith("```") and normalized.endswith("```"):
        lines = normalized.splitlines()
        normalized = "\n".join(lines[1:-1]).strip()
    decoder = json.JSONDecoder()
    start = normalized.find("{")
    if start < 0:
        raise ValueError("output does not contain a JSON object")
    value, end = decoder.raw_decode(normalized[start:])
    if normalized[start + end :].strip():
        raise ValueError("output contains text after the JSON object")
    if not isinstance(value, dict):
        raise ValueError("output JSON must be an object")
    return value


def _validate_plan_for_request(
    value: dict[str, Any],
    *,
    prompt: str,
    meditation_kind: MeditationKind | None = None,
) -> ProposedPlan:
    """Require an arrival and the correct waking or sleep-specific ending."""

    # Small local models reliably choose useful section semantics but often
    # mirror enum spellings such as ``focused_attention`` into the separate
    # kebab-case section ID field. IDs are internal labels, so repair this
    # harmless formatting mismatch deterministically while leaving technique,
    # safety, structure, and prose validation strict.
    normalized = dict(value)
    raw_sections = value.get("sections")
    if isinstance(raw_sections, list):
        normalized_sections: list[Any] = []
        for raw_section in raw_sections:
            if not isinstance(raw_section, dict):
                normalized_sections.append(raw_section)
                continue
            section = dict(raw_section)
            section_id = section.get("id")
            if isinstance(section_id, str):
                section["id"] = re.sub(
                    r"-+",
                    "-",
                    re.sub(r"[^a-z0-9]+", "-", section_id.casefold()),
                ).strip("-")[:48]
            normalized_sections.append(section)
        normalized["sections"] = normalized_sections
    plan = ProposedPlan.model_validate(normalized)
    if plan.sections[0].technique != "arrival":
        raise ValueError("the first section technique must be 'arrival'")
    expected_ending = (
        "sleep_transition" if _kind_for_request(prompt, meditation_kind) == "sleep" else "return"
    )
    if plan.sections[-1].technique != expected_ending:
        raise ValueError(
            f"the final section technique must be {expected_ending!r} for this request"
        )
    return plan


def _kind_for_request(prompt: str, meditation_kind: MeditationKind | None) -> MeditationKind:
    if meditation_kind is not None:
        return meditation_kind
    for candidate, pattern in MEDITATION_KIND_PATTERNS:
        if pattern.search(prompt):
            return candidate
    return "focused_attention"


def _blueprint_proposal(
    *,
    prompt: str,
    meditation_kind: MeditationKind | None,
    blueprints: BlueprintLibrary | None = None,
) -> ProposedPlan:
    """Build the reviewed practice skeleton before asking the LLM for prose."""

    kind = _kind_for_request(prompt, meditation_kind)
    blueprint = (blueprints or load_blueprint_library()).for_kind(kind)
    ending = "sleep_transition" if kind == "sleep" else "return"
    beats: tuple[tuple[str, str, int], ...] = (
        ("arrival", "instruction", 1),
        ("instruction", "embodied", 2),
        ("practice", "practice", 4),
        ("reminder", "instruction", 2),
        ("deeper-practice", "extended", 5),
        ("ending", "instruction", 1),
    )
    sections: list[dict[str, Any]] = []
    for index, (section_id, pause_intent, weight) in enumerate(beats):
        if index == 0:
            technique = "arrival"
        elif index == len(beats) - 1:
            technique = ending
            if kind == "sleep":
                pause_intent = "extended"
        else:
            technique = blueprint.primary_technique
        sections.append(
            {
                "id": section_id,
                "title": section_id.replace("-", " ").title(),
                "purpose": (f"{section_id.replace('-', ' ').title()}: {blueprint.purpose}")[:240],
                "technique": technique,
                "weight": weight,
                "pause_intent": pause_intent,
            }
        )
    return ProposedPlan.model_validate(
        {
            "title": f"{kind.replace('_', ' ').title()} Practice",
            "intention": prompt.strip()[:240],
            "sections": sections,
        }
    )


def _structural_candidate_score(plan: ProposedPlan, *, prompt: str) -> tuple[int, int, int, int]:
    requested = _content_words(prompt)
    overlap = len(
        requested
        & _content_words(
            " ".join(f"{section.title} {section.purpose}" for section in plan.sections)
        )
    )
    return (
        overlap,
        len({section.technique for section in plan.sections}),
        len({section.pause_intent for section in plan.sections}),
        -sum(1 for section in plan.sections if section.weight == 1),
    )


def _cap_and_redistribute_pauses(
    pauses: list[int],
    *,
    maximum_ms: int = 120_000,
) -> list[int]:
    """Preserve the quiet budget without creating an invalid oversized section pause."""

    capped = [min(pause, maximum_ms) for pause in pauses]
    overflow = sum(pauses) - sum(capped)
    while overflow > 0:
        candidates = [index for index, pause in enumerate(capped) if pause < maximum_ms]
        if not candidates:
            break
        share = max(1, (overflow + len(candidates) - 1) // len(candidates))
        distributed = 0
        for index in candidates:
            addition = min(share, maximum_ms - capped[index], overflow - distributed)
            capped[index] += addition
            distributed += addition
            if distributed >= overflow:
                break
        if distributed == 0:
            break
        overflow -= distributed
    return capped


def _allocate_plan(
    proposed: ProposedPlan,
    duration_seconds: int,
    *,
    articulation_words_per_minute: int = 123,
    prompt: str = "",
    blueprints: BlueprintLibrary | None = None,
    meditation_kind: MeditationKind | None = None,
) -> MeditationPlan:
    silence_ratio = 0.18 if duration_seconds <= 180 else 0.25 if duration_seconds <= 600 else 0.32
    silence_budget_seconds = max(
        6 * len(proposed.sections),
        round(duration_seconds * silence_ratio),
    )
    pause_weights = [PAUSE_INTENT_WEIGHTS[section.pause_intent] for section in proposed.sections]
    weight_total_for_pauses = sum(pause_weights)
    pauses = [
        max(1_000, round(silence_budget_seconds * weight / weight_total_for_pauses * 1_000))
        for weight in pause_weights
    ]
    pauses = _cap_and_redistribute_pauses(pauses)
    pause_total = sum(pauses)
    minimum_speech = 8 * len(proposed.sections)
    if pause_total + minimum_speech * 1_000 > duration_seconds * 1_000:
        scale = max(1_000, (duration_seconds - minimum_speech) * 1_000 // len(pauses))
        pauses = [min(pause, scale) for pause in pauses]
        pause_total = sum(pauses)
    available_seconds = max(minimum_speech, duration_seconds - round(pause_total / 1_000))
    weight_total = sum(section.weight for section in proposed.sections)
    # Give every section its hard minimum first. Dividing the whole budget and
    # clamping individual results can make the sum too large, which previously
    # pushed the final section back below its minimum during rounding repair.
    seconds_per_word = estimated_seconds_per_word(articulation_words_per_minute)
    total_words = max(8 * len(proposed.sections), round(available_seconds / seconds_per_word))
    remaining_words = total_words - 8 * len(proposed.sections)
    raw_extras = [remaining_words * section.weight / weight_total for section in proposed.sections]
    whole_extras = [int(extra) for extra in raw_extras]
    undistributed = remaining_words - sum(whole_extras)
    remainder_order = sorted(
        range(len(raw_extras)),
        key=lambda index: raw_extras[index] - whole_extras[index],
        reverse=True,
    )
    for index in remainder_order[:undistributed]:
        whole_extras[index] += 1
    allocated_words = [8 + extra for extra in whole_extras]
    planned: list[PlannedSection] = []
    for index, (section, target_words, pause_ms) in enumerate(
        zip(proposed.sections, allocated_words, pauses, strict=True)
    ):
        speech_seconds = max(8, round(target_words * 60 / articulation_words_per_minute))
        planned.append(
            PlannedSection(
                id=section.id,
                title=section.title,
                purpose=section.purpose,
                technique=section.technique,
                beat=(
                    "arrival"
                    if index == 0
                    else "ending"
                    if index == len(proposed.sections) - 1
                    else "practice"
                    if index % 2
                    else "reminder"
                ),
                pause_intent=section.pause_intent,
                delivery=TECHNIQUE_DELIVERY[section.technique],
                target_speech_seconds=speech_seconds,
                pause_after_ms=pause_ms,
                minimum_words=max(8, round(target_words * 0.65)),
                maximum_words=max(8, round(target_words * 1.35)),
            )
        )
    meditation_kind = _kind_for_request(prompt, meditation_kind)
    blueprint = (blueprints or load_blueprint_library()).for_kind(meditation_kind)
    beat_sequences: dict[int, tuple[str, ...]] = {
        3: ("arrival", "practice", "ending"),
        4: ("arrival", "instruction", "deeper_practice", "ending"),
        5: ("arrival", "instruction", "practice", "reminder", "ending"),
        6: ("arrival", "instruction", "practice", "reminder", "deeper_practice", "ending"),
    }
    beats = beat_sequences[len(planned)]
    planned = [
        section.model_copy(update={"beat": beat})
        for section, beat in zip(planned, beats, strict=True)
    ]
    return MeditationPlan(
        title=proposed.title,
        intention=proposed.intention,
        requested_outcome=proposed.intention,
        meditation_kind=meditation_kind,
        blueprint_id=blueprint.versioned_id,
        blueprint_version=blueprint.version,
        breath_mode=blueprint.breath_mode,
        ending_behavior=blueprint.ending_behavior,
        silence_budget_seconds=round(sum(pauses) / 1_000),
        requested_duration_seconds=duration_seconds,
        words_per_minute=articulation_words_per_minute,
        sections=planned,
    )


class LocalMeditationGenerator:
    """Generate a shared plan first, then independently validated sections."""

    def __init__(
        self,
        adapter: ScriptGenerator,
        prompts: PromptBundle,
        *,
        blueprints: BlueprintLibrary | None = None,
        max_validation_attempts: int = 3,
        max_parallel_sections: int = 1,
        articulation_words_per_minute: int = 123,
        workspace: GenerationWorkspace | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        if max_validation_attempts < 1:
            raise ValueError("max_validation_attempts must be positive")
        if not 1 <= max_parallel_sections <= 2:
            raise ValueError("max_parallel_sections must be one or two")
        if not 80 <= articulation_words_per_minute <= 220:
            raise ValueError("articulation_words_per_minute must be between 80 and 220")
        self.adapter = adapter
        self.prompts = prompts
        self.blueprints = blueprints
        self.max_validation_attempts = max_validation_attempts
        self.max_parallel_sections = max_parallel_sections
        self.articulation_words_per_minute = articulation_words_per_minute
        self.workspace = workspace
        self.progress_callback = progress_callback

    def _validated_generate(
        self,
        *,
        stage: str,
        system_prompt: str,
        prompt: str,
        seed: int,
        validator: Callable[[dict[str, Any]], ValidatedT],
        raw_attempts: list[RawGenerationAttempt],
    ) -> ValidatedT:
        repair = ""
        for attempt in range(1, self.max_validation_attempts + 1):
            result = self.adapter.generate(
                ScriptGenerationRequest(
                    prompt=prompt + repair,
                    system_prompt=system_prompt,
                    max_output_tokens=1_200,
                    seed=seed + attempt - 1,
                )
            )
            try:
                value = validator(_json_object(result.text))
            except (ContentSafetyError, TypeError, ValueError, ValidationError) as error:
                raw_attempts.append(
                    RawGenerationAttempt(
                        stage=stage,
                        attempt=attempt,
                        text=result.text,
                        elapsed_seconds=result.elapsed_seconds,
                        request_seed=seed + attempt - 1,
                        max_output_tokens=1_200,
                        telemetry=result.telemetry,
                        validation_error=str(error)[:2_000],
                    )
                )
                if self.workspace is not None:
                    self.workspace.save_attempt(raw_attempts[-1])
                repair = (
                    "\n\nYour previous response was invalid. Return a corrected JSON object. "
                    f"Validation error: {str(error)[:800]}"
                )
                continue
            raw_attempts.append(
                RawGenerationAttempt(
                    stage=stage,
                    attempt=attempt,
                    text=result.text,
                    elapsed_seconds=result.elapsed_seconds,
                    request_seed=seed + attempt - 1,
                    max_output_tokens=1_200,
                    telemetry=result.telemetry,
                )
            )
            if self.workspace is not None:
                self.workspace.save_attempt(raw_attempts[-1])
            return value
        raise GenerationError(
            f"{stage} remained invalid after {self.max_validation_attempts} attempts"
        )

    def generate(
        self,
        *,
        prompt: str,
        duration_seconds: int,
        run_id: UUID,
        created_at: datetime | None = None,
        seed: int = 42,
        meditation_kind: MeditationKind | None = None,
        writing_method: WritingMethod = "plan_first",
        posture: Literal["sitting", "lying_down"] | None = None,
    ) -> MeditationGenerationResult:
        """Create validated artifacts; raw model strings never bypass validation."""

        if not 60 <= duration_seconds <= 1_800:
            raise GenerationError("duration must be between 60 and 1,800 seconds")
        if not prompt.strip():
            raise GenerationError("prompt cannot be empty")
        if writing_method not in {
            "plan_first",
            "blueprint_first_sparse",
            "three_candidate_structural_selection",
            "recipe_whole_session",
        }:
            raise GenerationError(f"unsupported writing method {writing_method!r}")
        if writing_method == "recipe_whole_session":
            from whoopy.meditation.whole_session import generate_whole_session

            return MeditationGenerationResult(
                **generate_whole_session(
                    self,
                    prompt=prompt,
                    duration_seconds=duration_seconds,
                    run_id=run_id,
                    created_at=created_at,
                    seed=seed,
                    meditation_kind=meditation_kind,
                    posture=posture,
                )
            )
        raw_attempts: list[RawGenerationAttempt] = []
        if self.workspace is not None:
            from whoopy.meditation.workspace import GenerationManifest

            self.workspace.prepare(
                GenerationManifest(
                    run_id=run_id,
                    prompt=prompt.strip(),
                    duration_seconds=duration_seconds,
                    seed=seed,
                    plan_prompt_id=self.prompts.plan.prompt_id,
                    plan_prompt_version=self.prompts.plan.version,
                    section_prompt_id=self.prompts.section.prompt_id,
                    section_prompt_version=self.prompts.section.version,
                    writing_method=writing_method,
                    adapter=self.adapter.metadata,
                )
            )
            plan = self.workspace.load_plan()
        else:
            plan = None
        if plan is None:
            if self.progress_callback is not None:
                self.progress_callback("planning")
            plan_prompt = (
                f"User request: {prompt.strip()}\n"
                f"Requested duration: {duration_seconds} seconds.\n"
                f"Reviewed meditation kind: {meditation_kind or 'infer from request'}.\n"
                "Create the structural JSON plan now."
            )
            if writing_method == "blueprint_first_sparse":
                proposed = _blueprint_proposal(
                    prompt=prompt,
                    meditation_kind=meditation_kind,
                    blueprints=self.blueprints,
                )
            elif writing_method == "three_candidate_structural_selection":
                candidates = [
                    self._validated_generate(
                        stage=f"plan-candidate-{index + 1}",
                        system_prompt=self.prompts.plan.text,
                        prompt=plan_prompt,
                        seed=seed + index * 1_000,
                        validator=lambda value: _validate_plan_for_request(
                            value,
                            prompt=prompt,
                            meditation_kind=meditation_kind,
                        ),
                        raw_attempts=raw_attempts,
                    )
                    for index in range(3)
                ]
                proposed = max(
                    candidates,
                    key=lambda candidate: _structural_candidate_score(candidate, prompt=prompt),
                )
            else:
                proposed = self._validated_generate(
                    stage="plan",
                    system_prompt=self.prompts.plan.text,
                    prompt=plan_prompt,
                    seed=seed,
                    validator=lambda value: _validate_plan_for_request(
                        value,
                        prompt=prompt,
                        meditation_kind=meditation_kind,
                    ),
                    raw_attempts=raw_attempts,
                )
            focused_proposal = _compact_plan_for_duration(
                proposed,
                prompt=prompt,
                duration_seconds=duration_seconds,
            )
            plan = _allocate_plan(
                focused_proposal,
                duration_seconds,
                articulation_words_per_minute=self.articulation_words_per_minute,
                blueprints=self.blueprints,
                prompt=prompt,
                meditation_kind=meditation_kind,
            )
            if self.workspace is not None:
                self.workspace.save_plan(plan)

        if writing_method == "blueprint_first_sparse" and duration_seconds >= 180:
            # Sparse narration should be allowed to land well below the
            # nominal speech target. Deterministic quiet absorbs the
            # difference after drafting. Keep a bounded 20%-85% range around
            # the nominal spoken target: forcing 65% rejected useful sparse
            # prose, while allowing 135% let narration consume the reviewed
            # practice quiet. Apply this to resumed plans too.
            plan = plan.model_copy(
                update={
                    "sections": [
                        section.model_copy(
                            update={
                                "minimum_words": max(
                                    8,
                                    round(
                                        section.target_speech_seconds
                                        * plan.words_per_minute
                                        / 60
                                        * 0.20
                                    ),
                                ),
                                "maximum_words": max(
                                    22,
                                    round(
                                        section.target_speech_seconds
                                        * plan.words_per_minute
                                        / 60
                                        * 0.85
                                    ),
                                ),
                            }
                        )
                        for section in plan.sections
                    ]
                }
            )
            if self.workspace is not None:
                self.workspace.save_plan(plan)

        if self.progress_callback is not None:
            self.progress_callback("drafting")

        def draft(
            planned: PlannedSection,
            editorial_feedback: str = "",
            editorial_round: int = 0,
        ) -> DraftedSection:
            if self.workspace is not None and not editorial_feedback:
                checkpoint = self.workspace.load_section(planned)
                if checkpoint is not None:
                    return checkpoint
            section_prompt = (
                f"Meditation title: {plan.title}\n"
                f"Overall intention: {plan.intention}\n"
                f"Section ID: {planned.id}\n"
                f"Section purpose: {planned.purpose}\n"
                f"Primary technique: {planned.technique}\n"
                f"Technique instructions: {TECHNIQUE_GUIDANCE[planned.technique]}\n"
                f"Word range: {planned.minimum_words}-{planned.maximum_words} words.\n"
                "The complete text, not each sentence, must fit this word range. "
                "Stop once the range is satisfied.\n"
                f"{editorial_feedback}"
                "Write only this section as the required JSON object."
            )

            def validate(value: dict[str, Any]) -> DraftedSection:
                proposed_draft = ProposedDraft.model_validate(value)
                if proposed_draft.section_id != planned.id:
                    raise ValueError(
                        f"section_id must be {planned.id!r}, got {proposed_draft.section_id!r}"
                    )
                reviewed_text = apply_reviewed_technique_contract(
                    technique=planned.technique,
                    text=proposed_draft.text,
                )
                validate_meditation_text(reviewed_text)
                sentences = split_sentences(reviewed_text)
                if not sentences:
                    raise ValueError("section must contain at least one complete sentence")
                fitted_text = _fit_complete_sentences(
                    reviewed_text,
                    purpose=(f"{planned.purpose} {TECHNIQUE_GUIDANCE[planned.technique]}"),
                    minimum_words=planned.minimum_words,
                    maximum_words=planned.maximum_words,
                    maximum_sentence_words=MAX_SENTENCE_WORDS,
                )
                count = len(WORD_PATTERN.findall(fitted_text))
                return DraftedSection(
                    section_id=planned.id,
                    text=fitted_text,
                    word_count=count,
                )

            stage = (
                (
                    f"section:{planned.id}:editorial-repair"
                    if editorial_round == 1
                    else f"section:{planned.id}:editorial-repair-{editorial_round}"
                )
                if editorial_feedback
                else f"section:{planned.id}"
            )
            attempt_start = len(raw_attempts)
            try:
                section = self._validated_generate(
                    stage=stage,
                    system_prompt=self.prompts.section.text,
                    prompt=section_prompt,
                    seed=seed + 100 + plan.sections.index(planned) * 10 + editorial_round * 1_000,
                    validator=validate,
                    raw_attempts=raw_attempts,
                )
            except GenerationError:
                # Mandatory arrival/ending beats may be recovered when the
                # model's only structural mistake is a comma-spliced sentence.
                # The full production validator runs again, so unsafe,
                # off-technique, malformed, or out-of-budget prose fails closed.
                if planned.beat not in {"arrival", "ending"}:
                    raise
                section = None
                for attempt in reversed(raw_attempts[attempt_start:]):
                    try:
                        value = _json_object(attempt.text)
                        proposed = ProposedDraft.model_validate(value)
                        repaired_text = _repair_overlong_sentence_boundaries(proposed.text)
                        if repaired_text == proposed.text:
                            continue
                        section = validate(
                            proposed.model_copy(update={"text": repaired_text}).model_dump()
                        )
                    except (ContentSafetyError, TypeError, ValueError, ValidationError):
                        continue
                    break
                if section is None:
                    raise
            assert section is not None
            if self.workspace is not None:
                self.workspace.save_section(section)
            return section

        if self.max_parallel_sections == 1:
            sections = [draft(section) for section in plan.sections]
        else:
            with ThreadPoolExecutor(max_workers=self.max_parallel_sections) as executor:
                sections = list(executor.map(draft, plan.sections))

        def polish(current: list[DraftedSection]) -> list[DraftedSection]:
            result = apply_reviewed_style_contract(current)
            if self.workspace is not None:
                for section in result:
                    self.workspace.save_section(section)
            return result

        sections = polish(sections)
        editorial = review_script(plan=plan, sections=sections)
        ending_failure_codes = {
            "incorrect_sleep_ending",
            "incorrect_daytime_ending",
        }
        for editorial_round in range(1, 3):
            if editorial.passed:
                break
            if editorial_round > 1 and all(
                issue.code in ending_failure_codes for issue in editorial.issues
            ):
                # The reviewed deterministic ending contract is safer and
                # cheaper than asking the model the same ending question a
                # second time.
                break
            failed = editorial.failed_section_ids
            feedback = {
                section_id: "Editorial repair required: "
                + "; ".join(
                    issue.message for issue in editorial.issues if section_id in issue.section_ids
                )
                + " Return a genuinely different section.\n"
                for section_id in failed
            }
            sections = [
                draft(planned, feedback[planned.id], editorial_round)
                if planned.id in failed
                else section
                for planned, section in zip(plan.sections, sections, strict=True)
            ]
            sections = polish(sections)
            editorial = review_script(plan=plan, sections=sections)
        if not editorial.passed:
            if any(issue.code in ending_failure_codes for issue in editorial.issues):
                planned_ending = plan.sections[-1]
                reviewed_text = apply_reviewed_ending_contract(
                    plan=plan,
                    text=sections[-1].text,
                    prior_texts=[section.text for section in sections[:-1]],
                )
                reviewed_text = _fit_complete_sentences(
                    reviewed_text,
                    purpose=(
                        f"{planned_ending.purpose} {TECHNIQUE_GUIDANCE[planned_ending.technique]}"
                    ),
                    minimum_words=planned_ending.minimum_words,
                    maximum_words=planned_ending.maximum_words,
                    keep_first_sentence=True,
                )
                sections[-1] = DraftedSection(
                    section_id=planned_ending.id,
                    text=reviewed_text,
                    word_count=len(WORD_PATTERN.findall(reviewed_text)),
                )
                if self.workspace is not None:
                    self.workspace.save_section(sections[-1])
                editorial = review_script(plan=plan, sections=sections)
            hard_failures = {
                "unsafe_claim",
                "incorrect_sleep_ending",
                "incorrect_daytime_ending",
                "technique_mismatch",
                "vague_reassurance",
                "over_talking",
            }
            blocking = [issue.code for issue in editorial.issues if issue.code in hard_failures]
            if blocking:
                raise GenerationError(
                    "whole-script editorial repair failed: " + ", ".join(blocking)
                )

        def compile_script(current_plan: MeditationPlan) -> str:
            script_parts: list[str] = [f"# {current_plan.title}"]
            for planned, section in zip(current_plan.sections, sections, strict=True):
                pace = "very_slow" if current_plan.meditation_kind == "sleep" else "slow"
                script_parts.extend(
                    [
                        f"[delivery: {planned.delivery}]",
                        f"[pace: {pace}]",
                        pace_section(section.text, section_pause_ms=planned.pause_after_ms),
                    ]
                )
            return "\n\n".join(script_parts) + "\n"

        timestamp = created_at or datetime.now(UTC)
        preliminary_script = compile_script(plan)
        preliminary_timeline = build_script_timeline(
            run_id=run_id,
            script=preliminary_script,
            created_at=timestamp,
            source="generated_prompt",
        )
        spoken_words = sum(section.word_count for section in sections)
        spoken_seconds = spoken_words / plan.words_per_minute * 60
        timed_plan = _reallocate_section_pauses_for_actual_prose(
            plan,
            preliminary_timeline,
            spoken_seconds=spoken_seconds,
        )
        script, timed_plan = _fit_script_to_requested_duration(
            compile_script(timed_plan),
            timed_plan,
            spoken_seconds=spoken_seconds,
            requested_seconds=duration_seconds,
        )
        timeline = build_script_timeline(
            run_id=run_id,
            script=script,
            created_at=timestamp,
            source="generated_prompt",
        )
        silence_seconds = (
            sum(segment.duration_ms for segment in timeline.segments if segment.type == "SILENCE")
            / 1_000
        )
        estimated = spoken_seconds + silence_seconds
        final_plan = timed_plan.model_copy(
            update={"silence_budget_seconds": round(silence_seconds)}
        )
        tolerance = max(3, duration_seconds * 0.05)
        if abs(estimated - duration_seconds) > tolerance:
            raise GenerationError(
                f"validated script estimate is {estimated:.1f}s; requested "
                f"{duration_seconds}s with ±{tolerance:.1f}s tolerance"
            )
        result = MeditationGenerationResult(
            plan=final_plan,
            sections=sections,
            script=script,
            timeline=timeline,
            estimated_duration_seconds=estimated,
            editorial=editorial,
            raw_attempts=raw_attempts,
            writing_method=writing_method,
        )
        if self.workspace is not None:
            self.workspace.save_plan(final_plan)
            self.workspace.save_outputs(script, timeline)
        return result
