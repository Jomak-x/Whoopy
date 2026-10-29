"""Whole-session drafting with structural rejection and one holistic revision.

No model sentence is selected, deleted or rewritten here. Timing belongs to the
recipe compiler; prose that cannot fit safely is returned to the writer once.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, get_args
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic_core import ErrorType

from whoopy.meditation.editorial import EditorialReport
from whoopy.meditation.models import (
    DraftedSection,
    MeditationKind,
    MeditationPlan,
    PlannedSection,
    RawGenerationAttempt,
    SectionId,
    SpokenText,
)
from whoopy.meditation.pacing import sentence_word_count, split_sentences
from whoopy.meditation.recipes import (
    ParagraphSlot,
    Posture,
    RoutedSession,
    allocate_slots,
    route_session,
)
from whoopy.meditation.safety import validate_meditation_text
from whoopy.ports import ScriptGenerationRequest
from whoopy.timeline import SilenceSegment, SpeechSegment, Timeline, build_script_timeline

if TYPE_CHECKING:
    from whoopy.meditation.generator import LocalMeditationGenerator


RESPONSE_SCHEMA_ALGORITHM_VERSION = 1
TIMING_COMPILER_ALGORITHM_VERSION = 3


class WholeSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    section_id: SectionId
    paragraphs: list[SpokenText] = Field(min_length=1, max_length=100)


class WholeSessionDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sections: list[WholeSection] = Field(min_length=3, max_length=12)


def _public_validation_error(error: ValueError | TypeError) -> str:
    if isinstance(error, ValidationError):
        details = []
        for issue in error.errors(include_url=False, include_context=False, include_input=False):
            location = (
                ".".join(
                    str(part)
                    if isinstance(part, int) or part in {"sections", "section_id", "paragraphs"}
                    else "<field>"
                    for part in issue["loc"]
                )
                or "root"
            )
            kind = issue["type"] if issue["type"] in get_args(ErrorType) else "validation_error"
            details.append(f"{location}: {kind}")
        return "; ".join(details)
    return "TypeError" if isinstance(error, TypeError) else "ValueError"


MARKDOWN_EMPHASIS = re.compile(r"(?P<marker>\*{1,3}|_{1,3})(?=\S).+?(?<=\S)(?P=marker)")

BREATH = re.compile(r"\b(?:breath\w*|inhale|exhale|inhalation|exhalation)\b", re.I)
HOLD = re.compile(r"\bhold(?:ing)?\b.{0,20}\b(?:breath|inhale|exhale|count|second)\w*\b", re.I)
TIMED_BREATH = re.compile(
    r"\b(?:inhale|exhale|breathe in|breathe out)\b.{0,35}"
    r"\b(?:one|two|three|four|five|six|\d+)\b|"
    r"\b(?:count|counting)\b.{0,25}\b(?:breath|inhale|exhale)\w*\b",
    re.I,
)
SAFETY_ASSURANCE = re.compile(
    r"\b(?:you are|you're|you\u2019re|you will be) "
    r"(?:always |completely |perfectly |entirely )?safe\b|\bnothing can harm you\b",
    re.I,
)
PERSISTENCE = re.compile(
    r"\b(?:push through|ignore)\b.{0,30}\b(?:discomfort|dizz\w*|pain|breathless\w*|panic\w*)\b|"
    r"\b(?:continue|keep going|persist)\b.{0,15}\b(?:despite|even if|even when)\b"
    r".{0,35}\b(?:discomfort|uncomfortable|dizz\w*|pain|breathless\w*|panic\w*)\b",
    re.I,
)
CONTINUE_CUE = re.compile(
    r"\b(?:listen\w*|feel\w*|notic\w*|remain|rest\w*|stay|return\w*|"
    r"allow\w*|question|support|contact|sound\w*|anchor|sense|sensing)\b",
    re.I,
)
WAKING_ACTION = (
    r"open (?:your|the) eyes|wake(?: up)?|awaken|become alert|sit up|stand up|"
    r"return to (?:the|your) (?:day|room|surroundings)"
)
WAKING = re.compile(rf"\b(?:{WAKING_ACTION})\b", re.I)
MOVEMENT_OBJECT = (
    r"(?:(?:your|the|a|an)\s+)?(?:hands?|feet|fingers?|toes?|arms?|legs?|head|body|shoulders?)"
)
NEGATABLE_ACTION = (
    rf"move(?:\s+{MOVEMENT_OBJECT}(?:\s+(?:or|and)\s+{MOVEMENT_OBJECT})*)?"
    rf"|{WAKING_ACTION}"
)
NEGATED_WAKING = re.compile(
    r"\b(?:no need to|(?:do not|don't|don\u2019t) (?:need|have) to|"
    r"do not|don't|don\u2019t|need not|needn't|needn\u2019t|never|without needing to)\s+"
    rf"(?:{NEGATABLE_ACTION})"
    rf"(?:\s+(?:or|and)\s+(?:to\s+)?(?:{NEGATABLE_ACTION}))*\b",
    re.I,
)
LYING_CONTRADICTION = re.compile(
    r"\b(?:sit (?:upright|tall|up)|straighten your spine|feet (?:flat )?on (?:the )?floor|"
    r"palms? on (?:your )?(?:knees|thighs)|(?:in|on|against) (?:the|your|a) chair)\b",
    re.I,
)
SITTING_CONTRADICTION = re.compile(
    r"\b(?:lie|lay|lying)\s+(?:down|in bed)|\b(?:bed|mattress|pillow)\b",
    re.I,
)


def _has_waking_instruction(text: str) -> bool:
    # Ignore only explicit negated action spans. A later positive, contrastive
    # or separate-clause instruction must still be checked independently.
    negated = [match.span() for match in NEGATED_WAKING.finditer(text)]
    return any(
        not any(start <= match.start() and match.end() <= end for start, end in negated)
        for match in WAKING.finditer(text)
    )


def _strict_json(text: str) -> object:
    # Some instruct models emit exactly one Markdown envelope despite the JSON
    # request. Remove only that envelope; prose and raw-attempt provenance stay
    # intact. Commentary, nested/multiple fences and partial fences still fail.
    stripped = text.strip()
    envelope = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", stripped)
    if envelope is not None:
        stripped = envelope.group(1)
        if "```" in stripped:
            raise ValueError("multiple or nested JSON fences are not permitted")

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field {key!r}")
            result[key] = value
        return result

    return json.loads(stripped, object_pairs_hook=unique_object)


def _prepared_for_quiet(section_id: str, kind: MeditationKind, text: str) -> bool:
    sentences = split_sentences(text)
    is_reflection = section_id == "reflection" or (
        section_id == "practice" and kind == "reflection"
    )
    if CONTINUE_CUE.search(sentences[-1]) or (is_reflection and "?" in sentences[-1]):
        return True
    if section_id == "comfort":
        # Supported posture plus permission to adjust is a complete embodied
        # practice; its final sentence need not repeat the support instruction.
        return bool(
            re.search(r"\b(?:support\w*|contact|chair|seat|bed|mattress|floor)\b", text, re.I)
            and re.search(
                r"\b(?:adjust\w*|shift\w*|mov\w*|change (?:your )?position)\b", text, re.I
            )
        )
    return False


def recipe_quiet_limits(
    timeline: Timeline, plan: MeditationPlan, sections: list[DraftedSection]
) -> dict[str, int]:
    """Recover paragraph quiet bounds from the durable authored section contracts."""

    section_limits = {
        section.id: (
            22_000
            if plan.ending_behavior == "sleep_taper" and index >= len(plan.sections) - 2
            else 16_000
        )
        for index, section in enumerate(plan.sections)
    }
    paragraph_limits: dict[tuple[str, ...], int] = {}
    for section in sections:
        for paragraph in section.text.split("\n\n"):
            compiled = build_script_timeline(
                run_id=timeline.run_id,
                script=paragraph,
                created_at=timeline.created_at,
                source="generated_prompt",
            )
            chunks = tuple(
                segment.text for segment in compiled.segments if isinstance(segment, SpeechSegment)
            )
            text = " ".join(chunks)
            cap = section_limits[section.section_id]
            if not _prepared_for_quiet(section.section_id, plan.meditation_kind, text):
                cap = min(7_999, cap)
            paragraph_limits[chunks] = min(paragraph_limits.get(chunks, cap), cap)
    limits: dict[str, int] = {}
    preceding_chunks: list[str] = []
    for segment in timeline.segments:
        if isinstance(segment, SpeechSegment):
            preceding_chunks.append(segment.text)
        elif isinstance(segment, SilenceSegment):
            chunks = tuple(preceding_chunks)
            if chunks not in paragraph_limits:
                raise ValueError(
                    f"Cannot recover quiet limit for {segment.id}: preceding speech "
                    "does not match an authored paragraph"
                )
            limits[segment.id] = paragraph_limits[chunks]
            preceding_chunks.clear()
        else:
            preceding_chunks.clear()
    return limits


def _allocate_whole_quiets(
    draft: WholeSessionDraft,
    session: RoutedSession,
    slots: list[list[ParagraphSlot]],
    words_per_minute: int,
) -> list[list[int]]:
    """Fit exact session time by moving only bounded, prepared quiet.

    Lower word targets guide the writer, rather than requiring filler. A brief
    paragraph can borrow quiet capacity from the rest of the complete session.
    Sparse prose is still rejected when the total cannot fit the same caps.
    """

    durations: list[int] = []
    speeches: list[int] = []
    caps: list[int] = []
    word_counts: list[int] = []
    unprepared: list[str] = []
    for section, section_slots in zip(draft.sections, slots, strict=True):
        for index, (text, slot) in enumerate(zip(section.paragraphs, section_slots, strict=True)):
            durations.append(slot.duration_ms)
            words = sentence_word_count(text)
            word_counts.append(words)
            speeches.append(round(words * 60_000 / words_per_minute))
            prepared = _prepared_for_quiet(section.section_id, session.kind, text)
            caps.append(slot.maximum_quiet_ms if prepared else min(7_999, slot.maximum_quiet_ms))
            if not prepared:
                unprepared.append(f"{section.section_id} paragraph {index + 1}")
    required = sum(durations) - sum(speeches)
    minimum = len(caps) * 1_000
    maximum = sum(caps)
    if not minimum <= required <= maximum:
        actual_words = sum(word_counts)
        target_words = sum(
            round((slot.minimum_words + slot.maximum_words) / 2)
            for section_slots in slots
            for slot in section_slots
        )
        if required > maximum:
            needed_words = math.ceil((sum(durations) - maximum) * words_per_minute / 60_000)
            direction = (
                f"Too little spoken guidance: {actual_words} words. With these paragraph "
                f"endings, at least {needed_words} spoken words are needed "
                f"(about {max(0, needed_words - actual_words)} additional words). "
                "Expand the complete session with meaningful observation and return guidance "
                "for the existing practice; do not add filler or new techniques. "
            )
            if unprepared:
                direction += (
                    "These endings reduce quiet capacity to less than 8 seconds each: "
                    + ", ".join(unprepared)
                    + ". End them with a concrete invitation to continue the familiar practice. "
                )
        else:
            allowed_words = math.floor((sum(durations) - minimum) * words_per_minute / 60_000)
            direction = (
                f"Too much spoken guidance: {actual_words} words. At most {allowed_words} "
                f"spoken words fit the minimum quiet (about "
                f"{max(0, actual_words - allowed_words)} fewer words). "
                "Shorten the complete session while preserving the practice arc and "
                "required content. "
            )
        raise ValueError(
            f"whole-session quiet budget requires {required / 1000:.1f}s; "
            f"bounded prepared capacity is {minimum / 1000:.1f}-{maximum / 1000:.1f}s. "
            + direction
            + f"Aim for the contract's total target of {target_words} words, respecting "
            "every paragraph maximum."
        )
    quiets = [
        min(cap, max(1_000, duration - speech))
        for cap, duration, speech in zip(caps, durations, speeches, strict=True)
    ]
    delta = required - sum(quiets)
    while delta:
        active = (
            [index for index, quiet in enumerate(quiets) if quiet < caps[index]]
            if delta > 0
            else [index for index, quiet in enumerate(quiets) if quiet > 1_000]
        )
        # The global capacity check proves at least one interval can change.
        share = max(1, abs(delta) // len(active))
        for index in active:
            room = caps[index] - quiets[index] if delta > 0 else quiets[index] - 1_000
            adjustment = min(room, share, abs(delta))
            signed = adjustment if delta > 0 else -adjustment
            quiets[index] += signed
            delta -= signed
            if not delta:
                break
    result = []
    offset = 0
    for section_slots in slots:
        result.append(quiets[offset : offset + len(section_slots)])
        offset += len(section_slots)
    return result


def _validate_session(
    draft: WholeSessionDraft,
    session: RoutedSession,
    slots: list[list[ParagraphSlot]],
    words_per_minute: int,
) -> list[str]:
    """Return every independent gate failure so one revision sees the whole problem."""

    errors: list[str] = []
    expected = [beat.id for beat in session.recipe.beats]
    if [section.section_id for section in draft.sections] != expected:
        return [f"Beat IDs must occur exactly once, in this order: {expected}"]
    allocated_quiets: list[list[int]] | None = None
    if all(
        len(section.paragraphs) == len(section_slots)
        for section, section_slots in zip(draft.sections, slots, strict=True)
    ):
        try:
            allocated_quiets = _allocate_whole_quiets(draft, session, slots, words_per_minute)
        except ValueError as error:
            errors.append(str(error))
    all_sentences: set[str] = set()
    for section_index, (beat, section, section_slots) in enumerate(
        zip(session.recipe.beats, draft.sections, slots, strict=True)
    ):
        if len(section.paragraphs) != len(section_slots):
            errors.append(f"{beat.id}: expected {len(section_slots)} paragraphs")
            continue
        questions = sum(text.count("?") for text in section.paragraphs)
        is_reflection = beat.id == "reflection" or (
            beat.id == "practice" and session.kind == "reflection"
        )
        if questions != (1 if is_reflection else 0):
            errors.append(
                f"{beat.id}: {'ask exactly one question' if is_reflection else 'add no question'}"
            )
        for index, (text, slot) in enumerate(zip(section.paragraphs, section_slots, strict=True)):
            label = f"{beat.id} paragraph {index + 1}"
            if any(not line.strip() for line in text.splitlines()):
                errors.append(f"{label}: paragraph strings must not contain blank separator lines")
            try:
                validate_meditation_text(text)
            except ValueError as error:
                errors.append(f"{label}: {error}")
            words = sentence_word_count(text)
            if not 4 <= words <= slot.maximum_words:
                errors.append(f"{label}: {words} words; requires 4-{slot.maximum_words}")
            if MARKDOWN_EMPHASIS.search(text):
                errors.append(
                    f"{label}: Markdown emphasis is not spoken prose; rewrite in plain text "
                    "without asterisks or underscore emphasis, keeping complete sentences"
                )
            elif text[-1] not in '.!?"\u201d\u2019':
                errors.append(f"{label}: finish the final sentence")
            if BREATH.search(text) and (
                session.breath_excluded or session.anchor != "natural_breath"
            ):
                errors.append(
                    f"{label}: use the selected {session.anchor} anchor without breath guidance"
                )
            if SAFETY_ASSURANCE.search(text):
                errors.append(f"{label}: do not assert the listener is safe")
            if PERSISTENCE.search(text):
                errors.append(f"{label}: never require persistence through discomfort")
            if HOLD.search(text) or TIMED_BREATH.search(text):
                errors.append(f"{label}: unreviewed breath timing or holding")
            contradictory = (
                LYING_CONTRADICTION if session.posture == "lying_down" else SITTING_CONTRADICTION
            )
            if contradictory.search(text):
                errors.append(f"{label}: posture contradicts {session.posture}")
            sentences = split_sentences(text)
            if allocated_quiets is not None:
                quiet_ms = allocated_quiets[section_index][index]
                if quiet_ms >= 8_000 and not _prepared_for_quiet(beat.id, session.kind, text):
                    errors.append(
                        f"{label}: prepare the following quiet with a concrete practice invitation"
                    )
            for sentence in sentences:
                normalized = " ".join(re.findall(r"\b[\w'-]+\b", sentence.casefold()))
                if normalized in all_sentences:
                    errors.append(
                        f"{label}: repeated sentence; develop the practice without copying"
                    )
                all_sentences.add(normalized)
    opening = " ".join(draft.sections[0].paragraphs)
    if not re.search(
        r"\b(?:adjust\w*|shift\w*|mov\w*|change (?:your |the )?position)\b", opening, re.I
    ):
        errors.append("comfort: offer permission to adjust or move")
    orientation = " ".join(draft.sections[1].paragraphs)
    if not re.search(r"\b(?:sound\w*|hear\w*|listen\w*)\b", orientation, re.I):
        errors.append("orientation: establish surrounding sound before inward practice")
    if not re.search(
        r"\b(?:support|contact|weight|surface|seat|chair|bed|floor|mattress)\b|"
        r"\b(?:body|hands?|feet|back|legs?|arms?)\s+touch(?:es|ing)?\b",
        orientation,
        re.I,
    ):
        errors.append("orientation: establish bodily support or contact")
    ending = " ".join(draft.sections[-1].paragraphs)
    if session.recipe.ending == "sleep_taper":
        if _has_waking_instruction(ending):
            errors.append("taper: waking reorientation contradicts the sleep ending")
        if not re.search(r"\b(?:rest\w*|sleep\w*|quiet\w*)\b", ending, re.I):
            errors.append("taper: permit quiet rest without a new task")
    else:
        if not re.search(
            r"\b(?:hear(?:ing)?|listen(?:ing)?)\b|"
            r"\b(?:notice|noticing|check in with|attend to|return to|become aware of)\b"
            r"[^.!?;\n]{0,60}\bsounds?\b",
            ending,
            re.I,
        ) or not re.search(
            r"\b(?:move|moving|stretch(?:ing)?|lift(?:ing)?|shift(?:ing)?|"
            r"wiggl(?:e|ing)|flex(?:ing)?|curl(?:ing)?)\s+"
            r"(?:(?:your|the|a|an|one|both)\s+)?"
            r"(?:fingers?|toes?|hands?|arms?|feet|foot|legs?|body|weight|position|posture)\b|"
            r"\bstretch(?:ing)?(?:\s+(?:a little|gently|slightly))?"
            r"(?=\s*(?:[.!?,;]|$|if you (?:wish|like|want)\b))",
            ending,
            re.I,
        ):
            errors.append("return: include surrounding sound and an optional small movement")
        if re.search(
            r"\b(?:fall asleep|go to sleep|drift off|"
            r"(?:drift|fall) (?:back )?(?:to |into )sleep)\b",
            ending,
            re.I,
        ):
            errors.append("return: a sleep instruction contradicts the daytime ending")
    return errors


def _make_plan(
    session: RoutedSession,
    slots: list[list[ParagraphSlot]],
    prompt: str,
    duration_seconds: int,
    words_per_minute: int,
) -> MeditationPlan:
    sections = []
    for index, (beat, section_slots) in enumerate(zip(session.recipe.beats, slots, strict=True)):
        minimum = sum(slot.minimum_words for slot in section_slots)
        maximum = sum(slot.maximum_words for slot in section_slots)
        sections.append(
            PlannedSection(
                id=beat.id,
                title=beat.id.replace("-", " ").title(),
                purpose=beat.purpose,
                technique=beat.technique,
                beat="arrival"
                if index == 0
                else ("ending" if index == len(slots) - 1 else "practice"),
                delivery="sleepy" if beat.technique == "sleep_transition" else "calm",
                target_speech_seconds=max(
                    8, round((minimum + maximum) / 2 * 60 / words_per_minute)
                ),
                pause_after_ms=1_200,
                minimum_words=max(8, minimum),
                maximum_words=max(8, maximum),
            )
        )
    return MeditationPlan(
        title="Good Night" if session.kind == "sleep" else "A Steady Moment",
        intention=prompt.strip()[:240],
        requested_outcome=prompt.strip()[:240],
        meditation_kind=session.kind,
        blueprint_id=session.recipe.versioned_id,
        blueprint_version=session.recipe.version,
        breath_mode="external_anchor"
        if session.anchor != "natural_breath"
        else "natural_awareness",
        ending_behavior=session.recipe.ending,
        posture=session.posture,
        anchor=session.anchor,
        requested_duration_seconds=duration_seconds,
        words_per_minute=words_per_minute,
        sections=sections,
    )


def _required_content(section_id: str, session: RoutedSession) -> list[str]:
    requirements = {
        "comfort": [f"Supported {session.posture} posture", "Permission to adjust or move"],
        "orientation": ["Actual surrounding sound", "A concrete point of bodily contact"],
        "arrival": [f"Ordinary experience of the {session.anchor} anchor", "No forced change"],
        "anchor": [f"One {session.anchor} anchor", "Wandering and a simple return to that anchor"],
        "body": [
            "A clear ordered sequence of body areas",
            "Neutral sensation without forcing change",
        ],
        "practice": [session.main_practice, f"Continuity with the earlier {session.anchor} anchor"],
        "reflection": ["One bounded ordinary moment from the day", "Exactly one genuine question"],
        "response": ["Space for the listener's own response", "No new question or supplied answer"],
        "release": ["Release the earlier reflection", f"Return to familiar {session.anchor}"],
        "integration": [
            "Greater independence with the familiar practice",
            "Fewer new instructions",
        ],
        "taper": ["Progressively fewer demands", "Quiet rest with no waking reorientation"],
        "return": [
            "Actual surrounding sounds",
            "An optional small movement before the day resumes",
        ],
    }
    return requirements[section_id]


def _response_schema(
    session: RoutedSession,
    slots: list[list[ParagraphSlot]],
) -> dict[str, Any]:
    """Constrain structure while leaving every spoken string to the writer.

    llama.cpp b10142 supports tuple arrays via prefixItems. Do not add an items
    entry to that array: its converter prioritizes items over prefixItems.
    """

    sections = [
        {
            "type": "object",
            "properties": {
                "section_id": {"type": "string", "const": beat.id},
                "paragraphs": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": len(section_slots),
                    "maxItems": len(section_slots),
                },
            },
            "required": ["section_id", "paragraphs"],
            "additionalProperties": False,
        }
        for beat, section_slots in zip(session.recipe.beats, slots, strict=True)
    ]
    return {
        "type": "object",
        "properties": {
            "sections": {
                "type": "array",
                "prefixItems": sections,
                "minItems": len(sections),
                "maxItems": len(sections),
            },
        },
        "required": ["sections"],
        "additionalProperties": False,
    }


def generate_whole_session(
    generator: LocalMeditationGenerator,
    *,
    prompt: str,
    duration_seconds: int,
    run_id: UUID,
    created_at: datetime | None,
    seed: int,
    meditation_kind: MeditationKind | None,
    posture: Posture | None,
) -> dict[str, Any]:
    """Return ordinary generation artifacts while retaining every original paragraph."""

    from whoopy.meditation.generator import GenerationError
    from whoopy.meditation.workspace import GenerationManifest

    document = generator.prompts.whole_session
    if document is None:
        raise GenerationError(
            "recipe_whole_session requires whole_session_system.md in the prompt bundle"
        )
    try:
        session = route_session(
            prompt,
            duration_seconds,
            posture=posture,
            meditation_kind=meditation_kind,
            library=generator.recipes,
        )
    except ValueError as error:
        raise GenerationError(str(error)) from error
    slots = allocate_slots(session, duration_seconds, generator.articulation_words_per_minute)
    plan = _make_plan(
        session, slots, prompt, duration_seconds, generator.articulation_words_per_minute
    )
    response_schema = _response_schema(session, slots)
    schema_hash = hashlib.sha256(
        json.dumps(response_schema, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    recipe_hash = hashlib.sha256(session.recipe.model_dump_json().encode()).hexdigest()
    prompt_hash = hashlib.sha256(document.text.encode()).hexdigest()
    contract_hash = hashlib.sha256(
        json.dumps(
            {
                "session": session.model_dump(),
                "words_per_minute": plan.words_per_minute,
                "slots": [[slot.model_dump() for slot in section_slots] for section_slots in slots],
                "response_schema_algorithm_version": RESPONSE_SCHEMA_ALGORITHM_VERSION,
                "timing_compiler_algorithm_version": TIMING_COMPILER_ALGORITHM_VERSION,
                "response_schema_sha256": schema_hash,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    plan = plan.model_copy(
        update={
            "recipe_sha256": recipe_hash,
            "writing_prompt_id": document.prompt_id,
            "writing_prompt_version": document.version,
            "writing_prompt_sha256": prompt_hash,
            "timing_compiler_algorithm_version": TIMING_COMPILER_ALGORITHM_VERSION,
            "response_schema_algorithm_version": RESPONSE_SCHEMA_ALGORITHM_VERSION,
            "response_schema_sha256": schema_hash,
        }
    )
    if generator.workspace is not None:
        generator.workspace.prepare(
            GenerationManifest(
                run_id=run_id,
                prompt=prompt.strip(),
                duration_seconds=duration_seconds,
                seed=seed,
                plan_prompt_id=f"{session.recipe.versioned_id}:{session.posture}:{session.anchor}:"
                f"{session.kind}:{plan.words_per_minute}",
                plan_prompt_version=session.recipe.version,
                section_prompt_id=document.prompt_id,
                section_prompt_version=document.version,
                writing_method="recipe_whole_session",
                adapter=generator.adapter.metadata,
                recipe_sha256=recipe_hash,
                whole_session_prompt_sha256=prompt_hash,
                whole_session_contract_sha256=contract_hash,
                whole_session_response_schema_sha256=schema_hash,
                whole_session_schema_algorithm_version=RESPONSE_SCHEMA_ALGORITHM_VERSION,
                whole_session_timing_algorithm_version=TIMING_COMPILER_ALGORITHM_VERSION,
            )
        )
    contract = {
        # This optimistic floor assumes every paragraph prepares its bounded quiet.
        # Actual draft validation still uses each paragraph's effective quiet cap.
        "total_minimum_words": max(
            4 * sum(len(section_slots) for section_slots in slots),
            math.ceil(
                sum(
                    slot.duration_ms - slot.maximum_quiet_ms
                    for section_slots in slots
                    for slot in section_slots
                )
                * plan.words_per_minute
                / 60_000
            ),
        ),
        "words_per_minute": plan.words_per_minute,
        "word_budget_guidance": (
            "Aim near total_target_words. total_minimum_words is only the duration-derived "
            "floor when every paragraph prepares its maximum bounded quiet; it is not "
            "the writing target. Individual paragraphs may be brief if the whole session "
            "still fits. Develop meaningful guidance within the existing practice, not filler."
        ),
        "total_target_words": sum(
            round((slot.minimum_words + slot.maximum_words) / 2)
            for section_slots in slots
            for slot in section_slots
        ),
        "total_maximum_words": sum(
            slot.maximum_words for section_slots in slots for slot in section_slots
        ),
        "original_user_need": prompt.strip(),
        "recipe": session.recipe.versioned_id,
        "duration_seconds": duration_seconds,
        "posture": session.posture,
        "ending": session.recipe.ending,
        "anchor": session.anchor,
        "breath_focus": (
            "excluded"
            if session.breath_excluded
            else "optional natural only"
            if session.anchor == "natural_breath"
            else "external anchor only; no breath attention, instruction or mentions"
        ),
        "main_practice": session.main_practice,
        "beats_in_order": [
            {
                "section_id": beat.id,
                "purpose": beat.purpose,
                "required_content": _required_content(beat.id, session),
                "question_count": int(
                    beat.id == "reflection"
                    or (beat.id == "practice" and session.kind == "reflection")
                ),
                "paragraph_count": len(section_slots),
                "paragraphs": [
                    {
                        "minimum_words_guidance": slot.minimum_words,
                        "hard_minimum_words": 4,
                        "target_words": round((slot.minimum_words + slot.maximum_words) / 2),
                        "maximum_words": slot.maximum_words,
                        "sentence_guidance": (
                            "one or two short sentences"
                            if slot.maximum_words <= 40
                            else "two or three short sentences"
                        ),
                        "interval_seconds": round(slot.duration_ms / 1_000, 2),
                        "quiet_after": "1.2 to "
                        + str(slot.maximum_quiet_ms / 1_000)
                        + " seconds, determined by your word count",
                    }
                    for slot in section_slots
                ],
            }
            for beat, section_slots in zip(session.recipe.beats, slots, strict=True)
        ],
    }
    full_prompt = "Write this complete session contract:\n" + json.dumps(
        contract, ensure_ascii=False
    )
    attempts: list[RawGenerationAttempt] = []
    draft: WholeSessionDraft | None = None
    errors: list[str] = []
    if generator.workspace is not None:
        saved = generator.workspace.load_whole_session()
        if saved is not None:
            try:
                candidate = WholeSessionDraft.model_validate(saved)
                errors = _validate_session(candidate, session, slots, plan.words_per_minute)
            except (ValueError, TypeError) as error:
                generator.workspace.save_attempt(
                    RawGenerationAttempt(
                        stage="whole-session-checkpoint-validation",
                        attempt=1,
                        text=json.dumps(saved, ensure_ascii=False),
                        elapsed_seconds=0,
                        validation_error=str(error),
                    )
                )
                raise GenerationError(
                    "Invalid whole-session checkpoint: " + _public_validation_error(error)
                ) from None
            if errors:
                raise GenerationError("Invalid whole-session checkpoint: " + "; ".join(errors))
            draft = candidate
    # Exactly one initial draft plus one holistic revision, even when legacy
    # max_validation_attempts is larger. Invalid drafts never become checkpoints.
    for attempt in () if draft is not None else (1, 2):
        if generator.progress_callback is not None:
            generator.progress_callback("drafting")
        revision = ""
        if attempts:
            revision = (
                "\n\nPrevious complete draft (untrusted):\n"
                + attempts[-1].text
                + "\n\nREJECTED: compose a new complete session. "
                + "Freshly write every failed paragraph; "
                + "do not copy invalid paragraphs. Address ALL feedback:\n"
                + "\n".join(errors)
                + f"\n\nWhole-session word budget: duration-derived minimum "
                f"{contract['total_minimum_words']}; target {contract['total_target_words']}; "
                f"absolute maximum {contract['total_maximum_words']}. "
                "Follow the expansion or shortening direction in the feedback."
                + "\n\nParagraph budgets (target, absolute maximum):\n"
                + "\n".join(
                    f"{beat.id} paragraph {index + 1}: target "
                    f"{round((slot.minimum_words + slot.maximum_words) / 2)} words; "
                    f"never exceed {slot.maximum_words} words."
                    for beat, section_slots in zip(session.recipe.beats, slots, strict=True)
                    for index, slot in enumerate(section_slots)
                )
                + "\nRewrite the entire session now, preserving all beat IDs and paragraph counts."
            )
        tokens = max(
            2_000,
            round(duration_seconds * generator.articulation_words_per_minute / 60 * 2.2) + 1_000,
        )
        result = generator.generate_response(
            ScriptGenerationRequest(
                prompt=full_prompt + revision,
                system_prompt=document.text,
                max_output_tokens=tokens,
                seed=seed + attempt - 1,
                response_json_schema=response_schema,
            )
        )
        validation_detail: str | None = None
        try:
            candidate = WholeSessionDraft.model_validate(_strict_json(result.text))
            errors = _validate_session(candidate, session, slots, plan.words_per_minute)
            draft = candidate if not errors else None
        except (ValueError, TypeError) as error:
            validation_detail = str(error)
            errors = [
                "Corrupt structured output: "
                + _public_validation_error(error)
                + ". Return a complete object matching the session schema."
            ]
            draft = None
        record = RawGenerationAttempt(
            stage="whole-session" if attempt == 1 else "whole-session-revision",
            attempt=attempt,
            text=result.text,
            elapsed_seconds=result.elapsed_seconds,
            request_seed=seed + attempt - 1,
            max_output_tokens=tokens,
            telemetry=result.telemetry,
            validation_error=validation_detail or ("; ".join(errors) if errors else None),
        )
        attempts.append(record)
        if generator.workspace is not None:
            generator.workspace.save_attempt(record)
        if draft is not None:
            break
    if draft is None:
        raise GenerationError(
            "whole-session rejected after one holistic revision: " + "; ".join(errors)
        )
    sections = [
        DraftedSection(
            section_id=section.section_id,
            text="\n\n".join(section.paragraphs),
            word_count=sum(sentence_word_count(text) for text in section.paragraphs),
        )
        for section in draft.sections
    ]
    parts = [f"# {plan.title}"]
    silence_ms = 0
    allocated_quiets = _allocate_whole_quiets(draft, session, slots, plan.words_per_minute)
    for section, section_quiets, planned in zip(
        draft.sections, allocated_quiets, plan.sections, strict=True
    ):
        parts.extend([f"[delivery: {planned.delivery}]", "[pace: slow]"])
        for text, pause in zip(section.paragraphs, section_quiets, strict=True):
            silence_ms += pause
            parts.extend([text, f"[pause: {pause}ms]"])
    script = "\n\n".join(parts) + "\n"
    timeline = build_script_timeline(
        run_id=run_id,
        script=script,
        created_at=created_at or datetime.now(UTC),
        source="generated_prompt",
    )
    plan = plan.model_copy(update={"silence_budget_seconds": round(silence_ms / 1_000)})
    spoken_seconds = (
        sum(
            round(sentence_word_count(text) * 60_000 / plan.words_per_minute)
            for section in draft.sections
            for text in section.paragraphs
        )
        / 1_000
    )
    if generator.workspace is not None:
        generator.workspace.save_whole_session(draft.model_dump())
        generator.workspace.save_plan(plan)
        for drafted_section in sections:
            generator.workspace.save_section(drafted_section)
        generator.workspace.save_outputs(script, timeline)
    return {
        "plan": plan,
        "sections": sections,
        "script": script,
        "timeline": timeline,
        "estimated_duration_seconds": spoken_seconds + silence_ms / 1_000,
        "editorial": EditorialReport(passed=True, issues=[]),
        "raw_attempts": attempts,
        "writing_method": "recipe_whole_session",
    }
