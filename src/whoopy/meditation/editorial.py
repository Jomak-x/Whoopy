"""Whole-script editorial checks for Meditation Engine V2."""

from __future__ import annotations

import re
from collections import Counter

from pydantic import BaseModel, ConfigDict, Field

from whoopy.meditation.models import DraftedSection, MeditationPlan, PlannedSection
from whoopy.meditation.pacing import split_sentences
from whoopy.meditation.safety import ContentSafetyError, validate_meditation_text

WORD_PATTERN = re.compile(r"\b[\w'-]+\b")
VAGUE_REASSURANCE = re.compile(
    r"\b(?:embrace the calm|find your peace|everything is enough|this moment is yours|"
    r"you are exactly where you need to be|all is well)\b",
    re.I,
)
WAKING_LANGUAGE = re.compile(
    r"\b(?:open your eyes|return to the room|become alert|wake(?: up)?|awaken|"
    r"sit up|stand up|rise from (?:the )?(?:bed|chair))\b",
    re.I,
)
REORIENTATION_LANGUAGE = re.compile(
    r"\b(?:room|sound|support|contact|movement|move|step|eyes|surroundings|environment)\b",
    re.I,
)
OVERUSED_INVITATIONAL_OPENING = re.compile(
    r"^(?:you might\s+|if it feels [^,.;!?]{1,36},\s*)",
    re.I,
)
IMPERATIVE_OPENING = re.compile(
    r"^(?:allow|bring|choose|feel|hear|invite|let|notice|observe|rest|return|sense)\b",
    re.I,
)
FORCED_RELAXATION_LANGUAGE = re.compile(
    r"\b(?:let go of|release|remove|dissolve|clear away) (?:any |the )?tension\b",
    re.I,
)


class EditorialIssue(BaseModel):
    """One inspectable failure attached to the smallest repairable unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    message: str
    section_ids: list[str] = Field(min_length=1)


class EditorialReport(BaseModel):
    """No hidden score: each issue and failed section remains explicit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    issues: list[EditorialIssue]

    @property
    def failed_section_ids(self) -> set[str]:
        return {section_id for issue in self.issues for section_id in issue.section_ids}


def _lower_initial(text: str) -> str:
    return text[:1].lower() + text[1:]


def _vary_invitation(sentence: str, *, variant: int) -> str:
    remainder = OVERUSED_INVITATIONAL_OPENING.sub("", sentence, count=1).strip()
    if not remainder:
        return sentence
    direct = remainder[:1].upper() + remainder[1:]
    lowered = _lower_initial(remainder)
    templates = (
        direct,
        f"At your own pace, {lowered}",
        f"When you are ready, {lowered}",
        f"For now, {lowered}",
        f"As an option, {lowered}",
        f"Without forcing anything, {lowered}",
    )
    return templates[variant % len(templates)]


def _vary_repeated_imperative(sentence: str) -> str:
    """Reorder an existing qualifier without adding or deleting meaning."""

    if not IMPERATIVE_OPENING.match(sentence):
        return sentence
    match = re.match(
        r"^(?P<instruction>[^,]+),\s*"
        r"(?P<qualifier>(?:as|if|when|while|without)\b.+?)(?P<ending>[.!?])?$",
        sentence,
        re.I,
    )
    if match is None:
        return sentence
    qualifier = match.group("qualifier")
    instruction = match.group("instruction")
    return (
        f"{qualifier[:1].upper() + qualifier[1:]}, "
        f"{_lower_initial(instruction)}{match.group('ending') or '.'}"
    )


def apply_reviewed_style_contract(sections: list[DraftedSection]) -> list[DraftedSection]:
    """Remove model loops and vary mechanical invitations without inventing content.

    The local model's meaning is retained: exact later duplicates are omitted,
    while only a small set of overused opt-in lead-ins is rewritten. The
    replacements remain invitational at the script level and use varied,
    deterministic syntax so retries, resumes, and evaluations are stable.
    """

    seen_sentences: set[str] = set()
    opening_counts: Counter[str] = Counter()
    replacements: Counter[str] = Counter()
    polished: list[DraftedSection] = []
    for section in sections:
        kept: list[str] = []
        for sentence in split_sentences(section.text):
            normalized = " ".join(WORD_PATTERN.findall(sentence.casefold()))
            if normalized in seen_sentences:
                continue
            seen_sentences.add(normalized)
            opening = _opening(sentence)
            if opening_counts[opening] >= 2:
                if OVERUSED_INVITATIONAL_OPENING.match(sentence):
                    sentence = _vary_invitation(sentence, variant=replacements[opening])
                    replacements[opening] += 1
                else:
                    varied = _vary_repeated_imperative(sentence)
                    if varied != sentence:
                        sentence = varied
                        replacements[opening] += 1
            final_opening = _opening(sentence)
            if (
                final_opening != opening
                and opening_counts[final_opening] >= 2
                and OVERUSED_INVITATIONAL_OPENING.match(sentence)
            ):
                # Reordering an imperative's existing opt-in qualifier can
                # expose a different repeated opening. Resolve that collision
                # with the same reviewed invitation variants, skipping the
                # direct form that would recreate the original imperative.
                variant = max(1, replacements[final_opening] + 1)
                sentence = _vary_invitation(sentence, variant=variant)
                replacements[final_opening] += 1
                final_opening = _opening(sentence)
            opening_counts[final_opening] += 1
            kept.append(sentence)
        text = " ".join(kept)
        if not text:
            # A section made entirely of duplicates is invalid and must remain
            # visible to the ordinary editorial repair rather than disappear.
            text = section.text
        polished.append(
            DraftedSection(
                section_id=section.section_id,
                text=text,
                word_count=len(WORD_PATTERN.findall(text)),
            )
        )
    return polished


def _opening(sentence: str) -> str:
    return " ".join(word.casefold() for word in WORD_PATTERN.findall(sentence)[:3])


def review_script(
    *,
    plan: MeditationPlan,
    sections: list[DraftedSection],
) -> EditorialReport:
    """Review coherence signals only visible across the completed script."""

    planned_by_id: dict[str, PlannedSection] = {section.id: section for section in plan.sections}
    issues: list[EditorialIssue] = []
    sentence_owners: dict[str, list[str]] = {}
    openings: list[tuple[str, str]] = []
    for section in sections:
        try:
            validate_meditation_text(section.text)
        except ContentSafetyError as error:
            issues.append(
                EditorialIssue(
                    code="unsafe_claim",
                    message=str(error),
                    section_ids=[section.section_id],
                )
            )
        if VAGUE_REASSURANCE.search(section.text):
            issues.append(
                EditorialIssue(
                    code="vague_reassurance",
                    message="Replace vague reassurance with a concrete invitation.",
                    section_ids=[section.section_id],
                )
            )
        planned = planned_by_id[section.section_id]
        if planned.technique == "body_scan" and FORCED_RELAXATION_LANGUAGE.search(section.text):
            issues.append(
                EditorialIssue(
                    code="technique_mismatch",
                    message=(
                        "Body scans notice tension or ease without instructing the listener "
                        "to remove tension."
                    ),
                    section_ids=[section.section_id],
                )
            )
        if not planned.minimum_words <= section.word_count <= planned.maximum_words:
            issues.append(
                EditorialIssue(
                    code="over_talking",
                    message="Narration is outside its deterministic speech budget.",
                    section_ids=[section.section_id],
                )
            )
        for sentence in split_sentences(section.text):
            normalized = " ".join(WORD_PATTERN.findall(sentence.casefold()))
            if len(normalized.split()) >= 5:
                sentence_owners.setdefault(normalized, []).append(section.section_id)
            opening = _opening(sentence)
            if opening:
                openings.append((opening, section.section_id))

    for normalized_sentence, owners in sentence_owners.items():
        unique = list(dict.fromkeys(owners))
        if len(owners) > 1:
            issues.append(
                EditorialIssue(
                    code="repeated_idea",
                    message=f"Repeated sentence: {normalized_sentence}",
                    section_ids=unique[1:] or unique,
                )
            )
    opening_counts = Counter(opening for opening, _owner in openings)
    repeated_opening_threshold = max(4, round(len(openings) * 0.18))
    for repeated_opening in sorted(
        opening for opening, count in opening_counts.items() if count >= repeated_opening_threshold
    ):
        repeated_opening_sections = list(
            dict.fromkeys(owner for opening, owner in openings if opening == repeated_opening)
        )
        issues.append(
            EditorialIssue(
                code="repeated_structure",
                message=(
                    f"Too many sentences begin with {repeated_opening!r}. Do not use that "
                    "opening in the repair; begin with a concrete varied verb or observation."
                ),
                section_ids=repeated_opening_sections[1:] or repeated_opening_sections,
            )
        )

    ending = sections[-1]
    if plan.ending_behavior == "sleep_taper" and WAKING_LANGUAGE.search(ending.text):
        issues.append(
            EditorialIssue(
                code="incorrect_sleep_ending",
                message="Sleep guidance must taper without waking reorientation.",
                section_ids=[ending.section_id],
            )
        )
    if plan.ending_behavior == "daytime_reorientation" and not REORIENTATION_LANGUAGE.search(
        ending.text
    ):
        issues.append(
            EditorialIssue(
                code="incorrect_daytime_ending",
                message="Daytime guidance must reorient through contact, sound, or movement.",
                section_ids=[ending.section_id],
            )
        )
    return EditorialReport(passed=not issues, issues=issues)


def apply_reviewed_ending_contract(
    *,
    plan: MeditationPlan,
    text: str,
    prior_texts: list[str] | None = None,
) -> str:
    """Return prose that deterministically satisfies the reviewed ending contract.

    This is a bounded fallback after an LLM has already received one targeted
    repair attempt.  It changes no timing or technique choice: it only places
    an original reviewed sentence first so later word-budget fitting cannot
    trim away the required daytime reorientation or sleep taper.
    """

    prior_sentences = {
        " ".join(WORD_PATTERN.findall(sentence.casefold()))
        for prior_text in prior_texts or []
        for sentence in split_sentences(prior_text)
    }
    sentences = [
        sentence
        for sentence in split_sentences(text)
        if " ".join(WORD_PATTERN.findall(sentence.casefold())) not in prior_sentences
    ]
    if plan.ending_behavior == "sleep_taper":
        sentences = [sentence for sentence in sentences if not WAKING_LANGUAGE.search(sentence)]
        contract = "Let the guidance fade while you remain resting, with nothing else to do."
    else:
        contract = "Feel the support beneath you, hear the room, and invite a small movement."
    return " ".join([contract, *sentences])


def apply_reviewed_technique_contract(*, technique: str, text: str) -> str:
    """Apply a narrow deterministic repair that preserves speakable meaning."""

    if technique == "body_scan":
        return FORCED_RELAXATION_LANGUAGE.sub("notice any tension", text)
    return text
