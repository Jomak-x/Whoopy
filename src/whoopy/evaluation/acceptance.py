"""Honest Local V1 corpus acceptance from durable Lab evidence.

Automatic production checks and human judgments remain separate.  This module
never invents a rating, treats missing evidence as a pass, or reduces unrelated
quality dimensions into a hidden combined score.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from statistics import median
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from whoopy.evaluation.models import EvaluationSet, ExperimentReport, HumanRating

HumanDimension = Literal["content", "voice"]
REQUIRED_HUMAN_DIMENSIONS: tuple[HumanDimension, HumanDimension] = ("content", "voice")
SAFETY_METRIC_NAME = "safety_validation_passed"


class CorpusEvidenceError(ValueError):
    """Raised when durable Lab evidence cannot be read safely."""


class CaseAcceptanceEvidence(BaseModel):
    """Inspectable acceptance evidence for one reviewed prompt."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    successful_trial_count: int = Field(ge=0)
    safety_pass_count: int = Field(ge=0)
    safety_failure_count: int = Field(ge=0)
    content_scores: list[int] = Field(default_factory=list)
    voice_scores: list[int] = Field(default_factory=list)
    missing_human_dimensions: list[HumanDimension] = Field(default_factory=list)
    automatic_passed: bool
    human_passed: bool
    passed: bool


class CorpusAcceptanceSummary(BaseModel):
    """Machine-readable Local V1 gate without a synthetic aggregate score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str
    evaluation_version: int
    expected_case_count: int = Field(ge=1)
    covered_case_count: int = Field(ge=0)
    content_median: float | None = Field(default=None, ge=1, le=5)
    voice_median: float | None = Field(default=None, ge=1, le=5)
    lowest_required_human_score: int | None = Field(default=None, ge=1, le=5)
    baseline_content_preference_count: int = Field(ge=0)
    challenger_content_preference_count: int = Field(ge=0)
    tied_content_preference_count: int = Field(ge=0)
    complete_baseline_content_comparison: bool
    content_improves_over_baseline: bool
    no_safety_failures: bool
    complete_human_coverage: bool
    accepted: bool
    blockers: list[str] = Field(default_factory=list)
    cases: list[CaseAcceptanceEvidence]


def assess_corpus_acceptance(
    evaluation_set: EvaluationSet,
    evidence: Iterable[tuple[ExperimentReport, Iterable[HumanRating]]],
) -> CorpusAcceptanceSummary:
    """Evaluate the ten-prompt gate from production reports and blind ratings."""

    case_ids = [case.id for case in evaluation_set.cases]
    expected = set(case_ids)
    successful: dict[str, int] = {case_id: 0 for case_id in case_ids}
    safety_passes: dict[str, int] = {case_id: 0 for case_id in case_ids}
    safety_failures: dict[str, int] = {case_id: 0 for case_id in case_ids}
    scores: dict[str, dict[str, list[int]]] = {
        case_id: {dimension: [] for dimension in REQUIRED_HUMAN_DIMENSIONS} for case_id in case_ids
    }
    baseline_content_comparisons: dict[str, int] = {case_id: 0 for case_id in case_ids}
    baseline_content_preferences = 0
    challenger_content_preferences = 0
    tied_content_preferences = 0

    for report, stored_ratings in evidence:
        definition = report.definition
        if (
            definition.evaluation_id != evaluation_set.evaluation_id
            or definition.evaluation_version != evaluation_set.version
        ):
            continue
        assignments = {item.assignment_id: item for item in report.blind_assignments}
        report_ratings = {item.rating_id: item for item in report.ratings}
        for rating in stored_ratings:
            report_ratings[rating.rating_id] = rating
        for trial in report.trials:
            if trial.case_id not in expected:
                continue
            metric = next(
                (item for item in trial.metrics if item.name == SAFETY_METRIC_NAME),
                None,
            )
            if metric is not None and metric.value != 1:
                safety_failures[trial.case_id] += 1
            if not trial.success:
                continue
            successful[trial.case_id] += 1
            if metric is not None and metric.value == 1:
                safety_passes[trial.case_id] += 1
        for rating in report_ratings.values():
            if rating.dimension not in REQUIRED_HUMAN_DIMENSIONS:
                continue
            assignment = assignments.get(rating.assignment_id)
            if assignment is None or assignment.case_id not in expected:
                continue
            scores[assignment.case_id][rating.dimension].append(rating.score)
            if rating.dimension != "content" or definition.experiment_type not in {
                "llm",
                "writing_method",
            }:
                continue
            candidates_by_side = assignment.sealed_candidate_by_side
            if definition.baseline_candidate_id not in candidates_by_side.values():
                continue
            if rating.preferred_side is None:
                continue
            baseline_content_comparisons[assignment.case_id] += 1
            if rating.preferred_side == "tie":
                tied_content_preferences += 1
                continue
            preferred_candidate = candidates_by_side[rating.preferred_side]
            if preferred_candidate == definition.baseline_candidate_id:
                baseline_content_preferences += 1
            else:
                challenger_content_preferences += 1

    cases: list[CaseAcceptanceEvidence] = []
    all_content: list[int] = []
    all_voice: list[int] = []
    for case_id in case_ids:
        content = scores[case_id]["content"]
        voice = scores[case_id]["voice"]
        all_content.extend(content)
        all_voice.extend(voice)
        missing: list[HumanDimension] = [
            dimension for dimension in REQUIRED_HUMAN_DIMENSIONS if not scores[case_id][dimension]
        ]
        automatic_passed = (
            successful[case_id] > 0 and safety_passes[case_id] > 0 and safety_failures[case_id] == 0
        )
        required_scores = [*content, *voice]
        human_passed = not missing and min(required_scores, default=0) >= 3
        cases.append(
            CaseAcceptanceEvidence(
                case_id=case_id,
                successful_trial_count=successful[case_id],
                safety_pass_count=safety_passes[case_id],
                safety_failure_count=safety_failures[case_id],
                content_scores=content,
                voice_scores=voice,
                missing_human_dimensions=missing,
                automatic_passed=automatic_passed,
                human_passed=human_passed,
                passed=automatic_passed and human_passed,
            )
        )

    content_median = float(median(all_content)) if all_content else None
    voice_median = float(median(all_voice)) if all_voice else None
    all_required_scores = [*all_content, *all_voice]
    lowest_score = min(all_required_scores) if all_required_scores else None
    complete_human_coverage = all(not item.missing_human_dimensions for item in cases)
    complete_baseline_content_comparison = all(
        baseline_content_comparisons[case_id] > 0 for case_id in case_ids
    )
    content_improves_over_baseline = (
        complete_baseline_content_comparison
        and challenger_content_preferences > baseline_content_preferences
    )
    # Absence of evidence is not evidence of absence: this becomes true only
    # when every reviewed case has an explicit pass and none records a failure.
    no_safety_failures = all(
        item.safety_pass_count > 0 and item.safety_failure_count == 0 for item in cases
    )
    covered_case_count = sum(item.automatic_passed for item in cases)

    blockers: list[str] = []
    missing_automatic = [item.case_id for item in cases if not item.automatic_passed]
    if missing_automatic:
        blockers.append(
            "Missing successful production safety evidence for: " + ", ".join(missing_automatic)
        )
    failed_safety = [item.case_id for item in cases if item.safety_failure_count]
    if failed_safety:
        blockers.append("Safety failures recorded for: " + ", ".join(failed_safety))
    missing_human = [item.case_id for item in cases if item.missing_human_dimensions]
    if missing_human:
        blockers.append(
            "Missing blind content and/or voice ratings for: " + ", ".join(missing_human)
        )
    missing_baseline_comparison = [
        case_id for case_id in case_ids if baseline_content_comparisons[case_id] == 0
    ]
    if missing_baseline_comparison:
        blockers.append(
            "Missing blind content preference against a declared LLM/writing-method baseline for: "
            + ", ".join(missing_baseline_comparison)
        )
    elif not content_improves_over_baseline:
        blockers.append(
            "Blind content preference does not improve over baseline: "
            f"challenger {challenger_content_preferences}, baseline "
            f"{baseline_content_preferences}, ties {tied_content_preferences}."
        )
    if content_median is not None and content_median < 4:
        blockers.append(f"Content median is {content_median:g}; at least 4 is required.")
    if voice_median is not None and voice_median < 4:
        blockers.append(f"Voice median is {voice_median:g}; at least 4 is required.")
    if lowest_score is not None and lowest_score < 3:
        blockers.append(
            f"A required human rating is {lowest_score}; every content/voice rating must be 3+."
        )

    accepted = (
        covered_case_count == len(case_ids)
        and no_safety_failures
        and complete_human_coverage
        and content_improves_over_baseline
        and content_median is not None
        and content_median >= 4
        and voice_median is not None
        and voice_median >= 4
        and lowest_score is not None
        and lowest_score >= 3
    )
    return CorpusAcceptanceSummary(
        evaluation_id=evaluation_set.evaluation_id,
        evaluation_version=evaluation_set.version,
        expected_case_count=len(case_ids),
        covered_case_count=covered_case_count,
        content_median=content_median,
        voice_median=voice_median,
        lowest_required_human_score=lowest_score,
        baseline_content_preference_count=baseline_content_preferences,
        challenger_content_preference_count=challenger_content_preferences,
        tied_content_preference_count=tied_content_preferences,
        complete_baseline_content_comparison=complete_baseline_content_comparison,
        content_improves_over_baseline=content_improves_over_baseline,
        no_safety_failures=no_safety_failures,
        complete_human_coverage=complete_human_coverage,
        accepted=accepted,
        blockers=blockers,
        cases=cases,
    )


def load_lab_acceptance_evidence(
    lab_root: Path,
) -> list[tuple[ExperimentReport, list[HumanRating]]]:
    """Load every immutable report and its separately stored ratings."""

    evidence: list[tuple[ExperimentReport, list[HumanRating]]] = []
    for report_path in sorted((lab_root / "experiments").glob("*/v*/report.json")):
        try:
            report = ExperimentReport.model_validate_json(report_path.read_text("utf-8"))
            ratings = [
                HumanRating.model_validate_json(path.read_text("utf-8"))
                for path in sorted(report_path.parent.joinpath("ratings").glob("*.json"))
            ]
        except (OSError, ValidationError) as error:
            message = f"Could not load Lab evidence {report_path}: {error}"
            raise CorpusEvidenceError(message) from error
        evidence.append((report, ratings))
    return evidence
