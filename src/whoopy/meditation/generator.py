"""Plan-first, schema-validated local meditation generation."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from whoopy.meditation.models import (
    DraftedSection,
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
from whoopy.meditation.safety import ContentSafetyError, validate_meditation_text
from whoopy.ports import ScriptGenerationRequest, ScriptGenerator
from whoopy.timeline import Timeline, build_script_timeline

WORD_PATTERN = re.compile(r"\b[\w'-]+\b")
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
    raw_attempts: list[RawGenerationAttempt]


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
) -> str:
    """Select complete, purpose-relevant sentences within the time budget.

    Small local models sometimes obey sentence-style instructions but ignore a
    tight total word range. Selecting at reviewed sentence boundaries is safer
    than accepting an overrun and more useful than blindly keeping an unrelated
    introduction.
    """

    purpose_words = _content_words(purpose)
    sentences = split_sentences(text)
    ranked = sorted(
        enumerate(sentences),
        key=lambda item: (
            len(purpose_words & _content_words(item[1])),
            -item[0],
        ),
        reverse=True,
    )
    selected: list[tuple[int, str]] = []
    count = 0
    for index, sentence in ranked:
        sentence_words = sentence_word_count(sentence)
        if count + sentence_words > maximum_words:
            continue
        selected.append((index, sentence))
        count += sentence_words
    selected.sort()
    if count < minimum_words:
        raise ValueError(
            f"complete sentences provide {count} usable words; expected "
            f"{minimum_words}-{maximum_words}"
        )
    return " ".join(sentence for _index, sentence in selected)


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


def _allocate_plan(
    proposed: ProposedPlan,
    duration_seconds: int,
    *,
    articulation_words_per_minute: int = 123,
) -> MeditationPlan:
    pauses = [round(section.pause_seconds * 1_000) for section in proposed.sections]
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
    for section, target_words, pause_ms in zip(
        proposed.sections, allocated_words, pauses, strict=True
    ):
        speech_seconds = max(8, round(target_words * 60 / articulation_words_per_minute))
        planned.append(
            PlannedSection(
                id=section.id,
                title=section.title,
                purpose=section.purpose,
                target_speech_seconds=speech_seconds,
                pause_after_ms=pause_ms,
                minimum_words=max(8, round(target_words * 0.65)),
                maximum_words=max(8, round(target_words * 1.35)),
            )
        )
    return MeditationPlan(
        title=proposed.title,
        intention=proposed.intention,
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
        max_validation_attempts: int = 3,
        max_parallel_sections: int = 1,
        articulation_words_per_minute: int = 123,
        workspace: GenerationWorkspace | None = None,
    ) -> None:
        if max_validation_attempts < 1:
            raise ValueError("max_validation_attempts must be positive")
        if not 1 <= max_parallel_sections <= 2:
            raise ValueError("max_parallel_sections must be one or two")
        if not 80 <= articulation_words_per_minute <= 220:
            raise ValueError("articulation_words_per_minute must be between 80 and 220")
        self.adapter = adapter
        self.prompts = prompts
        self.max_validation_attempts = max_validation_attempts
        self.max_parallel_sections = max_parallel_sections
        self.articulation_words_per_minute = articulation_words_per_minute
        self.workspace = workspace

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
    ) -> MeditationGenerationResult:
        """Create validated artifacts; raw model strings never bypass validation."""

        if not 60 <= duration_seconds <= 1_800:
            raise GenerationError("duration must be between 60 and 1,800 seconds")
        if not prompt.strip():
            raise GenerationError("prompt cannot be empty")
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
                    adapter=self.adapter.metadata,
                )
            )
            plan = self.workspace.load_plan()
        else:
            plan = None
        if plan is None:
            plan_prompt = (
                f"User request: {prompt.strip()}\n"
                f"Requested duration: {duration_seconds} seconds.\n"
                "Create the structural JSON plan now."
            )
            proposed = self._validated_generate(
                stage="plan",
                system_prompt=self.prompts.plan.text,
                prompt=plan_prompt,
                seed=seed,
                validator=ProposedPlan.model_validate,
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
            )
            if self.workspace is not None:
                self.workspace.save_plan(plan)

        def draft(planned: PlannedSection) -> DraftedSection:
            if self.workspace is not None:
                checkpoint = self.workspace.load_section(planned)
                if checkpoint is not None:
                    return checkpoint
            section_prompt = (
                f"Meditation title: {plan.title}\n"
                f"Overall intention: {plan.intention}\n"
                f"Section ID: {planned.id}\n"
                f"Section purpose: {planned.purpose}\n"
                f"Word range: {planned.minimum_words}-{planned.maximum_words} words.\n"
                "The complete text, not each sentence, must fit this word range. "
                "Stop once the range is satisfied.\n"
                "Write only this section as the required JSON object."
            )

            def validate(value: dict[str, Any]) -> DraftedSection:
                proposed_draft = ProposedDraft.model_validate(value)
                if proposed_draft.section_id != planned.id:
                    raise ValueError(
                        f"section_id must be {planned.id!r}, got {proposed_draft.section_id!r}"
                    )
                validate_meditation_text(proposed_draft.text)
                sentences = split_sentences(proposed_draft.text)
                if not sentences:
                    raise ValueError("section must contain at least one complete sentence")
                longest_sentence = max(sentence_word_count(sentence) for sentence in sentences)
                if longest_sentence > MAX_SENTENCE_WORDS:
                    raise ValueError(
                        f"section contains a {longest_sentence}-word sentence; "
                        f"maximum is {MAX_SENTENCE_WORDS}"
                    )
                fitted_text = _fit_complete_sentences(
                    proposed_draft.text,
                    purpose=planned.purpose,
                    minimum_words=planned.minimum_words,
                    maximum_words=planned.maximum_words,
                )
                count = len(WORD_PATTERN.findall(fitted_text))
                return DraftedSection(
                    section_id=planned.id,
                    text=fitted_text,
                    word_count=count,
                )

            section = self._validated_generate(
                stage=f"section:{planned.id}",
                system_prompt=self.prompts.section.text,
                prompt=section_prompt,
                seed=seed + 100 + plan.sections.index(planned) * 10,
                validator=validate,
                raw_attempts=raw_attempts,
            )
            if self.workspace is not None:
                self.workspace.save_section(section)
            return section

        if self.max_parallel_sections == 1:
            sections = [draft(section) for section in plan.sections]
        else:
            with ThreadPoolExecutor(max_workers=self.max_parallel_sections) as executor:
                sections = list(executor.map(draft, plan.sections))

        script_parts: list[str] = [f"# {plan.title}"]
        for planned, section in zip(plan.sections, sections, strict=True):
            script_parts.append(pace_section(section.text, section_pause_ms=planned.pause_after_ms))
        script = "\n\n".join(script_parts) + "\n"
        timestamp = created_at or datetime.now(UTC)
        timeline = build_script_timeline(
            run_id=run_id,
            script=script,
            created_at=timestamp,
            source="generated_prompt",
        )
        spoken_words = sum(section.word_count for section in sections)
        silence_seconds = (
            sum(segment.duration_ms for segment in timeline.segments if segment.type == "SILENCE")
            / 1_000
        )
        estimated = spoken_words / plan.words_per_minute * 60 + silence_seconds
        tolerance = max(20, duration_seconds * 0.25)
        if abs(estimated - duration_seconds) > tolerance:
            raise GenerationError(
                f"validated script estimate is {estimated:.1f}s; requested "
                f"{duration_seconds}s with ±{tolerance:.1f}s tolerance"
            )
        result = MeditationGenerationResult(
            plan=plan,
            sections=sections,
            script=script,
            timeline=timeline,
            estimated_duration_seconds=estimated,
            raw_attempts=raw_attempts,
        )
        if self.workspace is not None:
            self.workspace.save_outputs(script, timeline)
        return result
