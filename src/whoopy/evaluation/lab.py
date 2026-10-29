"""Production-backed Local V1 Evaluation Lab orchestration.

The Lab owns comparison design and evidence.  It never synthesizes substitute
content: a caller must supply a production executor that creates ordinary
Whoopy runs and returns their immutable artifacts.
"""

from __future__ import annotations

import hashlib
import json
import random
import threading
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from statistics import median
from typing import Literal, Protocol, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from whoopy.evaluation.models import (
    ArtifactReference,
    BlindAssignment,
    BlindPresentation,
    EvaluationSet,
    ExperimentDefinition,
    ExperimentReport,
    HumanRating,
    MetricGoal,
    ModelSelectionRole,
    ProductionTrialRequest,
    ProductionTrialResult,
    PromotionDecision,
    QueueEntry,
    RegressionMetric,
    candidate_pairs,
)

_REQUIRED_PROMOTION_DIMENSIONS: dict[
    Literal["llm", "writing_method", "voice", "delivery", "pacing"],
    tuple[Literal["content", "voice", "pacing", "naturalness", "safety", "preference"], ...],
] = {
    "llm": ("content", "voice"),
    "writing_method": ("content", "voice"),
    "voice": ("voice",),
    "delivery": ("voice",),
    "pacing": ("pacing",),
}

_PREFERENCE_DIMENSION: dict[
    Literal["llm", "writing_method", "voice", "delivery", "pacing"],
    Literal["content", "voice", "pacing"],
] = {
    "llm": "content",
    "writing_method": "content",
    "voice": "voice",
    "delivery": "voice",
    "pacing": "pacing",
}


class BlindReviewStatus(TypedDict):
    required_dimensions: list[str]
    preference_dimension: str
    expected_case_count: int
    reviewed_case_count: int
    missing_case_ids: list[str]
    missing_dimensions_by_assignment: dict[str, list[str]]
    missing_preference_assignment_ids: list[str]
    complete_current_pairs: bool
    complete_evaluation_set: bool
    promotion_ready: bool


class ProductionExperimentExecutor(Protocol):
    """Port implemented by the normal run pipeline, adapters, and artifact store."""

    def execute(self, request: ProductionTrialRequest) -> ProductionTrialResult:
        """Execute one ordinary production run and return its durable evidence."""


class EvaluationExecutionError(RuntimeError):
    """Raised when production evidence does not match the controlled request."""


class EvaluationExecutionHalted(RuntimeError):
    """Carries durable partial results when a user pauses or cancels a batch."""

    def __init__(self, trials: Iterable[ProductionTrialResult]) -> None:
        super().__init__("experiment execution halted by user request")
        self.trials = tuple(trials)


class SequentialExperimentQueue:
    """Thread-safe FIFO contract that permits at most one heavyweight experiment."""

    def __init__(self) -> None:
        self._entries: list[QueueEntry] = []
        self._lock = threading.Lock()

    def entries(self) -> tuple[QueueEntry, ...]:
        with self._lock:
            return tuple(self._entries)

    def enqueue(self, definition: ExperimentDefinition) -> QueueEntry:
        with self._lock:
            key = (definition.experiment_id, definition.version)
            if any(
                (entry.experiment_id, entry.experiment_version) == key
                and entry.status in {"queued", "running"}
                for entry in self._entries
            ):
                raise EvaluationExecutionError("experiment revision is already queued or running")
            entry = QueueEntry(
                experiment_id=definition.experiment_id,
                experiment_version=definition.version,
                enqueued_at=datetime.now(UTC),
            )
            self._entries.append(entry)
            return entry

    def start_next(self) -> QueueEntry | None:
        with self._lock:
            if any(entry.status == "running" for entry in self._entries):
                return None
            for index, entry in enumerate(self._entries):
                if entry.status == "queued":
                    running = entry.model_copy(
                        update={"status": "running", "started_at": datetime.now(UTC)}
                    )
                    self._entries[index] = running
                    return running
            return None

    def finish(
        self,
        *,
        experiment_id: str,
        experiment_version: int,
        failure: str | None = None,
    ) -> QueueEntry:
        with self._lock:
            for index, entry in enumerate(self._entries):
                if (
                    entry.experiment_id == experiment_id
                    and entry.experiment_version == experiment_version
                    and entry.status == "running"
                ):
                    finished = entry.model_copy(
                        update={
                            "status": "failed" if failure else "completed",
                            "completed_at": datetime.now(UTC),
                            "failure": failure,
                        }
                    )
                    self._entries[index] = finished
                    return finished
            raise EvaluationExecutionError("only the running experiment can be finished")

    def cancel(self, *, experiment_id: str, experiment_version: int) -> QueueEntry:
        with self._lock:
            for index, entry in enumerate(self._entries):
                if (
                    entry.experiment_id == experiment_id
                    and entry.experiment_version == experiment_version
                    and entry.status == "queued"
                ):
                    cancelled = entry.model_copy(
                        update={"status": "cancelled", "completed_at": datetime.now(UTC)}
                    )
                    self._entries[index] = cancelled
                    return cancelled
            raise EvaluationExecutionError("only a queued experiment can be cancelled")


class EvaluationLab:
    """Run controlled cases sequentially and assemble blind review evidence."""

    def __init__(
        self,
        *,
        evaluation_set: EvaluationSet,
        platform: str,
    ) -> None:
        self.evaluation_set = evaluation_set
        self.platform = platform

    def run(
        self,
        definition: ExperimentDefinition,
        executor: ProductionExperimentExecutor,
        *,
        case_ids: Iterable[str] | None = None,
        existing_trials: Iterable[ProductionTrialResult] = (),
        resumable_runs: dict[UUID, UUID] | None = None,
        should_halt: Callable[[], bool] | None = None,
        trial_completed: Callable[[tuple[ProductionTrialResult, ...]], None] | None = None,
    ) -> ExperimentReport:
        """Run selected cases candidate-by-candidate through the production port.

        Supplying one ``case_id`` is the inexpensive single-case trial used
        before committing to a batch.  Calls are intentionally synchronous so
        heavyweight model ownership can never overlap inside one experiment.
        """

        if (
            definition.evaluation_id != self.evaluation_set.evaluation_id
            or definition.evaluation_version != self.evaluation_set.version
        ):
            raise EvaluationExecutionError("experiment references a different evaluation set")
        requested_ids = None if case_ids is None else list(case_ids)
        if requested_ids is not None and len(requested_ids) != len(set(requested_ids)):
            raise EvaluationExecutionError("case_ids must not contain duplicates")
        available = {case.id: case for case in self.evaluation_set.cases}
        if requested_ids is None:
            cases = self.evaluation_set.cases
        else:
            missing = sorted(set(requested_ids) - set(available))
            if missing:
                raise EvaluationExecutionError(f"unknown evaluation case IDs: {missing}")
            if not requested_ids:
                raise EvaluationExecutionError("at least one evaluation case is required")
            cases = [available[case_id] for case_id in requested_ids]

        trials = list(existing_trials)
        existing_by_key = {(trial.case_id, trial.candidate_id): trial for trial in trials}
        resumable = resumable_runs or {}
        for case in cases:
            controlled_source_run_id: UUID | None = None
            candidates = sorted(
                definition.candidates,
                key=lambda candidate: candidate.candidate_id != definition.baseline_candidate_id,
            )
            for candidate in candidates:
                if should_halt is not None and should_halt():
                    raise EvaluationExecutionHalted(trials)
                trial_id = uuid5(
                    NAMESPACE_URL,
                    f"whoopy:{definition.experiment_id}:{definition.version}:"
                    f"{case.id}:{candidate.candidate_id}:{definition.seed}",
                )
                request = ProductionTrialRequest(
                    experiment_id=definition.experiment_id,
                    experiment_version=definition.version,
                    trial_id=trial_id,
                    case=case,
                    candidate_id=candidate.candidate_id,
                    condition=candidate.condition,
                    seed=definition.seed,
                    experiment_type=definition.experiment_type,
                    controlled_source_run_id=(
                        controlled_source_run_id
                        if definition.experiment_type in {"voice", "delivery", "pacing"}
                        else None
                    ),
                )
                existing = existing_by_key.get((case.id, candidate.candidate_id))
                if existing is not None and trial_id not in resumable:
                    if (
                        candidate.candidate_id == definition.baseline_candidate_id
                        and existing.success
                    ):
                        controlled_source_run_id = existing.source_run_id
                    continue
                if trial_id in resumable:
                    resume = getattr(executor, "resume", None)
                    if not callable(resume):
                        raise EvaluationExecutionError(
                            "production executor cannot resume the interrupted Lab trial"
                        )
                    result = resume(request, resumable[trial_id])
                else:
                    result = executor.execute(request)
                self._validate_result(request, result)
                if existing is None:
                    trials.append(result)
                else:
                    trials[trials.index(existing)] = result
                existing_by_key[(case.id, candidate.candidate_id)] = result
                resumable.pop(trial_id, None)
                if trial_completed is not None:
                    trial_completed(tuple(trials))
                if should_halt is not None and should_halt():
                    raise EvaluationExecutionHalted(trials)
                if candidate.candidate_id == definition.baseline_candidate_id and result.success:
                    controlled_source_run_id = result.source_run_id
                elif (
                    candidate.candidate_id == definition.baseline_candidate_id
                    and definition.experiment_type in {"voice", "delivery", "pacing"}
                ):
                    # A controlled comparison cannot borrow content from a
                    # challenger when its declared baseline failed.
                    break

        self._validate_fixed_inputs(definition, trials)

        expected_candidates = {candidate.candidate_id for candidate in definition.candidates}
        successful: dict[str, set[str]] = {}
        for trial in trials:
            if trial.success:
                successful.setdefault(trial.case_id, set()).add(trial.candidate_id)
        incomplete = {
            case.id: sorted(expected_candidates - successful.get(case.id, set()))
            for case in cases
            if expected_candidates - successful.get(case.id, set())
        }
        return ExperimentReport(
            definition=definition,
            created_at=datetime.now(UTC),
            platform=self.platform,
            trials=trials,
            blind_assignments=self._blind_assignments(definition, trials),
            comparison_status="incomplete" if incomplete else "complete",
            incomplete_candidates_by_case=incomplete,
        )

    @staticmethod
    def _validate_result(
        request: ProductionTrialRequest,
        result: ProductionTrialResult,
    ) -> None:
        mismatches: list[str] = []
        if result.trial_id != request.trial_id:
            mismatches.append("trial_id")
        if result.case_id != request.case.id:
            mismatches.append("case_id")
        if result.candidate_id != request.candidate_id:
            mismatches.append("candidate_id")
        if result.actual_condition != request.condition:
            mismatches.append("actual_condition")
        if result.controlled_source_run_id != request.controlled_source_run_id:
            mismatches.append("controlled_source_run_id")
        if (
            result.success
            and request.controlled_source_run_id is not None
            and result.canonical_source is None
        ):
            mismatches.append("canonical_source")
        if mismatches:
            raise EvaluationExecutionError(
                "production result does not match the controlled request: " + ", ".join(mismatches)
            )

    @staticmethod
    def _validate_fixed_inputs(
        definition: ExperimentDefinition,
        trials: list[ProductionTrialResult],
    ) -> None:
        fields = {
            "llm": (
                "prompt_sha256",
                "blueprint_sha256",
                "generation_schema_sha256",
                "pacing_profile_sha256",
            ),
            "writing_method": (
                "prompt_sha256",
                "blueprint_sha256",
                "generation_schema_sha256",
                "pacing_profile_sha256",
            ),
            "voice": (
                "spoken_text_sha256",
                "speech_controls_sha256",
                "silence_timeline_sha256",
                "timeline_topology_sha256",
                "pacing_profile_sha256",
            ),
            "delivery": (
                "spoken_text_sha256",
                "silence_timeline_sha256",
                "timeline_topology_sha256",
                "pacing_profile_sha256",
            ),
            "pacing": (
                "spoken_text_sha256",
                "speech_controls_sha256",
                "timeline_topology_sha256",
            ),
        }[definition.experiment_type]
        successful_by_case: dict[str, list[ProductionTrialResult]] = {}
        for trial in trials:
            if trial.success:
                successful_by_case.setdefault(trial.case_id, []).append(trial)
        for case_id, case_trials in successful_by_case.items():
            fingerprints = [trial.controlled_inputs for trial in case_trials]
            if any(fingerprint is None for fingerprint in fingerprints):
                raise EvaluationExecutionError("successful trial omitted controlled-input evidence")
            for field in fields:
                if field in {
                    "speech_controls_sha256",
                    "timeline_topology_sha256",
                    "pacing_profile_sha256",
                } and any(
                    fingerprint is not None and getattr(fingerprint, field) is None
                    for fingerprint in fingerprints
                ):
                    raise EvaluationExecutionError(
                        f"controlled comparison omitted {field} evidence"
                    )
                values = {
                    getattr(fingerprint, field)
                    for fingerprint in fingerprints
                    if fingerprint is not None
                }
                if len(values) > 1:
                    raise EvaluationExecutionError(
                        f"{definition.experiment_type} experiment changed fixed input "
                        f"{field} for case {case_id}"
                    )

    @staticmethod
    def _blind_assignments(
        definition: ExperimentDefinition,
        trials: list[ProductionTrialResult],
    ) -> list[BlindAssignment]:
        successful = {
            (trial.case_id, trial.candidate_id): trial for trial in trials if trial.success
        }
        case_ids = list(dict.fromkeys(trial.case_id for trial in trials))
        assignments: list[BlindAssignment] = []
        for case_id in case_ids:
            for first_id, second_id in candidate_pairs(definition):
                first = successful.get((case_id, first_id))
                second = successful.get((case_id, second_id))
                if first is None or second is None:
                    continue
                seed_bytes = hashlib.sha256(
                    f"{definition.seed}:{case_id}:{first_id}:{second_id}".encode()
                ).digest()
                randomized = random.Random(seed_bytes)
                left, right = (first, second) if randomized.randrange(2) == 0 else (second, first)
                assignments.append(
                    BlindAssignment(
                        assignment_id=uuid5(
                            NAMESPACE_URL,
                            f"whoopy:blind:{definition.experiment_id}:{definition.version}:"
                            f"{case_id}:{first_id}:{second_id}:{definition.seed}",
                        ),
                        case_id=case_id,
                        left_trial_id=left.trial_id,
                        right_trial_id=right.trial_id,
                        sealed_candidate_by_side={
                            "left": left.candidate_id,
                            "right": right.candidate_id,
                        },
                    )
                )
        return assignments


def add_rating(report: ExperimentReport, rating: HumanRating) -> ExperimentReport:
    """Return a new report revision containing one non-duplicate blind rating."""

    if rating.assignment_id not in {
        assignment.assignment_id for assignment in report.blind_assignments
    }:
        raise EvaluationExecutionError("rating references an unknown blind assignment")
    if any(existing.rating_id == rating.rating_id for existing in report.ratings):
        raise EvaluationExecutionError("rating_id already exists")
    if any(
        existing.assignment_id == rating.assignment_id and existing.dimension == rating.dimension
        for existing in report.ratings
    ):
        raise EvaluationExecutionError(
            f"a {rating.dimension} rating already exists for this blind assignment"
        )
    return report.model_copy(update={"ratings": [*report.ratings, rating]})


def blind_review_status(
    report: ExperimentReport,
    *,
    expected_case_ids: Iterable[str] | None = None,
) -> BlindReviewStatus:
    """Describe the explicit human evidence still needed before promotion.

    Review completion is deliberately dimension-aware.  A single generic score
    must not reveal identities or turn a one-case trial into a Studio default.
    """

    required_dimensions = _REQUIRED_PROMOTION_DIMENSIONS[report.definition.experiment_type]
    preference_dimension = _PREFERENCE_DIMENSION[report.definition.experiment_type]
    expected = list(
        dict.fromkeys(
            expected_case_ids
            if expected_case_ids is not None
            else (assignment.case_id for assignment in report.blind_assignments)
        )
    )
    assignment_case_ids = {assignment.case_id for assignment in report.blind_assignments}
    missing_case_ids = [case_id for case_id in expected if case_id not in assignment_case_ids]
    ratings_by_assignment: dict[UUID, list[HumanRating]] = {}
    for rating in report.ratings:
        ratings_by_assignment.setdefault(rating.assignment_id, []).append(rating)

    missing_dimensions_by_assignment: dict[str, list[str]] = {}
    missing_preference_assignment_ids: list[str] = []
    for assignment in report.blind_assignments:
        ratings = ratings_by_assignment.get(assignment.assignment_id, [])
        saved_dimensions = {rating.dimension for rating in ratings}
        missing_dimensions = [
            str(dimension) for dimension in required_dimensions if dimension not in saved_dimensions
        ]
        if missing_dimensions:
            missing_dimensions_by_assignment[str(assignment.assignment_id)] = missing_dimensions
        preferred_rating = next(
            (rating for rating in ratings if rating.dimension == preference_dimension),
            None,
        )
        if preferred_rating is None or preferred_rating.preferred_side is None:
            missing_preference_assignment_ids.append(str(assignment.assignment_id))

    complete_current_pairs = bool(report.blind_assignments) and not (
        missing_dimensions_by_assignment or missing_preference_assignment_ids
    )
    complete_evaluation_set = not missing_case_ids and bool(expected)
    return {
        "required_dimensions": list(required_dimensions),
        "preference_dimension": preference_dimension,
        "expected_case_count": len(expected),
        "reviewed_case_count": len(assignment_case_ids.intersection(expected)),
        "missing_case_ids": missing_case_ids,
        "missing_dimensions_by_assignment": missing_dimensions_by_assignment,
        "missing_preference_assignment_ids": missing_preference_assignment_ids,
        "complete_current_pairs": complete_current_pairs,
        "complete_evaluation_set": complete_evaluation_set,
        "promotion_ready": complete_current_pairs and complete_evaluation_set,
    }


def blind_presentations(report: ExperimentReport) -> list[BlindPresentation]:
    """Return randomized playable pairs without revealing candidate metadata."""

    presentations: list[BlindPresentation] = []
    for assignment in report.blind_assignments:
        presentations.append(
            BlindPresentation(
                assignment_id=assignment.assignment_id,
                case_id=assignment.case_id,
                left_media_token=uuid5(assignment.assignment_id, "left"),
                right_media_token=uuid5(assignment.assignment_id, "right"),
            )
        )
    return presentations


def resolve_blind_audio(report: ExperimentReport, media_token: UUID) -> ArtifactReference:
    """Resolve an opaque reviewer token without exposing the sealed candidate map."""

    trials = {trial.trial_id: trial for trial in report.trials}
    for assignment in report.blind_assignments:
        for side, trial_id in (
            ("left", assignment.left_trial_id),
            ("right", assignment.right_trial_id),
        ):
            if uuid5(assignment.assignment_id, side) == media_token:
                trial = trials[trial_id]
                return next(artifact for artifact in trial.artifacts if artifact.role == "audio")
    raise EvaluationExecutionError("unknown blind media token")


def compare_against_baseline(
    report: ExperimentReport,
    baseline: ExperimentReport,
    *,
    candidate_id: str,
    baseline_candidate_id: str,
) -> list[RegressionMetric]:
    """Compare matched named metrics without reducing them to a total score."""

    if (
        report.definition.evaluation_id != baseline.definition.evaluation_id
        or report.definition.evaluation_version != baseline.definition.evaluation_version
    ):
        raise EvaluationExecutionError(
            "regression reports must use the same versioned evaluation set"
        )
    if report.definition.experiment_type != baseline.definition.experiment_type:
        raise EvaluationExecutionError("regression reports must compare the same experiment type")
    if report.platform != baseline.platform:
        raise EvaluationExecutionError("regression reports must use the same hardware platform")
    if candidate_id not in {candidate.candidate_id for candidate in report.definition.candidates}:
        raise EvaluationExecutionError("regression candidate is not in the current report")
    if baseline_candidate_id not in {
        candidate.candidate_id for candidate in baseline.definition.candidates
    }:
        raise EvaluationExecutionError("baseline candidate is not in the baseline report")

    baseline_values = {
        (trial.case_id, metric.name): metric
        for trial in baseline.trials
        if trial.candidate_id == baseline_candidate_id and trial.success
        for metric in trial.metrics
    }
    comparisons: list[RegressionMetric] = []
    for trial in report.trials:
        if trial.candidate_id != candidate_id or not trial.success:
            continue
        for metric in trial.metrics:
            old = baseline_values.get((trial.case_id, metric.name))
            if old is None or old.unit != metric.unit or old.goal != metric.goal:
                continue
            delta = metric.value - old.value
            comparisons.append(
                RegressionMetric(
                    case_id=trial.case_id,
                    candidate_id=candidate_id,
                    metric_name=metric.name,
                    baseline_value=old.value,
                    current_value=metric.value,
                    delta=delta,
                    goal=metric.goal,
                    direction=_direction(delta, metric.goal),
                )
            )
    return comparisons


def _direction(
    delta: float,
    goal: MetricGoal,
) -> Literal["improved", "regressed", "unchanged", "informational"]:
    if goal == "informational":
        return "informational"
    if delta == 0:
        return "unchanged"
    improved = delta > 0 if goal == "higher" else delta < 0
    return "improved" if improved else "regressed"


def create_promotion_decision(
    report: ExperimentReport,
    *,
    outcome: Literal["promote", "reject"],
    candidate_id: str,
    previous_value: str,
    selected_value: str,
    rationale: str,
    report_sha256: str,
    selection_role: ModelSelectionRole | None = None,
    expected_case_ids: Iterable[str] | None = None,
    supported_low_resource_llm_ids: Iterable[str] = (),
) -> PromotionDecision:
    """Create an immutable promotion/rejection record tied to reviewed evidence."""

    selected_candidate = next(
        (item for item in report.definition.candidates if item.candidate_id == candidate_id),
        None,
    )
    if selected_candidate is None:
        raise EvaluationExecutionError("decision candidate is not part of the experiment")
    if outcome == "promote" and report.comparison_status != "complete":
        raise EvaluationExecutionError(
            "promotion requires a complete comparison with every candidate successful"
        )
    if outcome == "promote":
        review = blind_review_status(report, expected_case_ids=expected_case_ids)
        if not review["complete_evaluation_set"]:
            missing = ", ".join(review["missing_case_ids"])
            raise EvaluationExecutionError(
                "promotion requires successful blind comparisons for the complete versioned "
                f"evaluation set; missing: {missing}"
            )
        if not review["complete_current_pairs"]:
            dimensions = ", ".join(review["required_dimensions"])
            raise EvaluationExecutionError(
                "promotion requires every blind pair to have saved "
                f"{dimensions} ratings and an explicit "
                f"{review['preference_dimension']} preference"
            )
        required = set(review["required_dimensions"])
        supporting_scores = [
            rating.score for rating in report.ratings if rating.dimension in required
        ]
        if not supporting_scores or min(supporting_scores) < 3 or median(supporting_scores) < 4:
            raise EvaluationExecutionError(
                "promotion requires every required human rating to be 3+ and the median to be 4+"
            )
        is_low_resource_fallback = (
            report.definition.experiment_type == "llm" and selection_role == "low_resource_fallback"
        )
        if is_low_resource_fallback:
            supported_ids = {value.casefold() for value in supported_low_resource_llm_ids}
            if selected_candidate.condition.llm_model_id.casefold() not in supported_ids:
                raise EvaluationExecutionError(
                    "low_resource_fallback must select a model exposed by the production "
                    "Lite profile"
                )
        preference_dimension = review["preference_dimension"]
        preferences = {
            rating.assignment_id: rating
            for rating in report.ratings
            if rating.dimension == preference_dimension
        }
        candidate_support: dict[str, list[int]] = {}
        for assignment in report.blind_assignments:
            candidates = assignment.sealed_candidate_by_side
            if candidate_id not in candidates.values():
                continue
            opponent = next(value for value in candidates.values() if value != candidate_id)
            rating = preferences[assignment.assignment_id]
            outcome_value = 0
            if rating.preferred_side != "tie":
                assert rating.preferred_side is not None
                preferred = candidates[rating.preferred_side]
                outcome_value = 1 if preferred == candidate_id else -1
            candidate_support.setdefault(opponent, []).append(outcome_value)
        unsupported = sorted(
            opponent
            for opponent, values in candidate_support.items()
            if sum(value == 1 for value in values) <= sum(value == -1 for value in values)
        )
        expected_opponents = {
            candidate.candidate_id
            for candidate in report.definition.candidates
            if candidate.candidate_id != candidate_id
        }
        # A portable or higher-quality default is a quality winner.  A Lite
        # fallback has a different product purpose: it must remain acceptable
        # by the same complete blind review and score floors, but requiring it
        # to defeat the portable default would make the two distinct required
        # roles contradictory.  Its lower-resource identity is instead
        # fail-closed against the production executor's supported Lite map.
        if not is_low_resource_fallback and (
            set(candidate_support) != expected_opponents or unsupported
        ):
            detail = ", ".join(unsupported or sorted(expected_opponents - set(candidate_support)))
            raise EvaluationExecutionError(
                "promotion requires the selected candidate to win more blind preferences than "
                f"it loses against every alternative; unsupported against: {detail}"
            )
    if outcome not in {"promote", "reject"}:
        raise EvaluationExecutionError("decision outcome must be promote or reject")
    if outcome == "promote" and report.definition.experiment_type == "llm":
        if selection_role is None:
            raise EvaluationExecutionError(
                "LLM promotions must identify portable_default, low_resource_fallback, "
                "or higher_quality"
            )
    elif selection_role is not None:
        raise EvaluationExecutionError("model selection roles are only valid for promoted LLMs")
    rating_hashes = {
        str(rating.rating_id): hashlib.sha256(
            json.dumps(
                rating.model_dump(mode="json"),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        for rating in report.ratings
    }
    return PromotionDecision(
        decision_id=uuid4(),
        created_at=datetime.now(UTC),
        experiment_id=report.definition.experiment_id,
        experiment_version=report.definition.version,
        outcome=outcome,
        studio_target=report.definition.experiment_type,
        selection_role=selection_role,
        candidate_id=candidate_id,
        previous_value=previous_value,
        selected_value=selected_value,
        rationale=rationale,
        report_sha256=report_sha256,
        rating_ids=[rating.rating_id for rating in report.ratings],
        rating_sha256_by_id=rating_hashes,
    )
