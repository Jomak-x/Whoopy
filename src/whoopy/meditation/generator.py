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
from whoopy.meditation.prompts import PromptBundle
from whoopy.meditation.safety import ContentSafetyError, validate_meditation_text
from whoopy.ports import ScriptGenerationRequest, ScriptGenerator
from whoopy.timeline import Timeline, build_script_timeline

WORD_PATTERN = re.compile(r"\b[\w'-]+\b")
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


def _allocate_plan(proposed: ProposedPlan, duration_seconds: int) -> MeditationPlan:
    pauses = [round(section.pause_seconds * 1_000) for section in proposed.sections]
    pause_total = sum(pauses)
    minimum_speech = 8 * len(proposed.sections)
    if pause_total + minimum_speech > duration_seconds:
        scale = max(1_000, (duration_seconds - minimum_speech) * 1_000 // len(pauses))
        pauses = [min(pause, scale) for pause in pauses]
        pause_total = sum(pauses)
    speech_budget = max(minimum_speech, duration_seconds - round(pause_total / 1_000))
    weight_total = sum(section.weight for section in proposed.sections)
    allocated = [
        max(8, round(speech_budget * section.weight / weight_total))
        for section in proposed.sections
    ]
    allocated[-1] += speech_budget - sum(allocated)
    words_per_minute = 165
    planned: list[PlannedSection] = []
    for section, speech_seconds, pause_ms in zip(proposed.sections, allocated, pauses, strict=True):
        target_words = speech_seconds * words_per_minute / 60
        planned.append(
            PlannedSection(
                id=section.id,
                title=section.title,
                purpose=section.purpose,
                target_speech_seconds=speech_seconds,
                pause_after_ms=pause_ms,
                minimum_words=max(8, round(target_words * 0.72)),
                maximum_words=max(8, round(target_words * 1.18)),
            )
        )
    return MeditationPlan(
        title=proposed.title,
        intention=proposed.intention,
        requested_duration_seconds=duration_seconds,
        words_per_minute=words_per_minute,
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
        workspace: GenerationWorkspace | None = None,
    ) -> None:
        if max_validation_attempts < 1:
            raise ValueError("max_validation_attempts must be positive")
        if not 1 <= max_parallel_sections <= 2:
            raise ValueError("max_parallel_sections must be one or two")
        self.adapter = adapter
        self.prompts = prompts
        self.max_validation_attempts = max_validation_attempts
        self.max_parallel_sections = max_parallel_sections
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
            plan = _allocate_plan(proposed, duration_seconds)
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
                "Write only this section as the required JSON object."
            )

            def validate(value: dict[str, Any]) -> DraftedSection:
                proposed_draft = ProposedDraft.model_validate(value)
                if proposed_draft.section_id != planned.id:
                    raise ValueError(
                        f"section_id must be {planned.id!r}, got {proposed_draft.section_id!r}"
                    )
                validate_meditation_text(proposed_draft.text)
                count = len(WORD_PATTERN.findall(proposed_draft.text))
                if not planned.minimum_words <= count <= planned.maximum_words:
                    raise ValueError(
                        f"section has {count} words; expected "
                        f"{planned.minimum_words}-{planned.maximum_words}"
                    )
                return DraftedSection(
                    section_id=planned.id,
                    text=proposed_draft.text,
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
            script_parts.extend([section.text, f"[pause: {planned.pause_after_ms}ms]"])
        script = "\n\n".join(script_parts) + "\n"
        timestamp = created_at or datetime.now(UTC)
        timeline = build_script_timeline(
            run_id=run_id,
            script=script,
            created_at=timestamp,
        )
        spoken_words = sum(section.word_count for section in sections)
        silence_seconds = sum(section.pause_after_ms for section in plan.sections) / 1_000
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
