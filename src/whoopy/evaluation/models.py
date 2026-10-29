"""Typed contracts for repeatable production-backed evaluation.

The Phase 3.5 script bake-off remains readable for migration purposes.  The
Local V1 contracts below model the complete Lab: one controlled variable per
experiment, durable production artifact references, blind review, explicit
metrics, and auditable Studio-default decisions.
"""

from __future__ import annotations

import re
from itertools import combinations
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import yaml
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from whoopy.ports import AdapterMetadata

WritingMethod = Literal[
    "plan_first",
    "blueprint_first_sparse",
    "three_candidate_structural_selection",
    "recipe_whole_session",
]


class EvaluationCase(BaseModel):
    """One reviewed request in the versioned test set."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    category: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    prompt: str = Field(min_length=1, max_length=2_000)
    duration_seconds: int = Field(ge=60, le=600)
    blueprint_id: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_-]+@[1-9][0-9]*$")
    reviewed_script_path: str | None = None
    reviewed_timeline_path: str | None = None


class EvaluationSet(BaseModel):
    """Immutable collection that makes comparisons use identical requests."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 1
    evaluation_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    cases: list[EvaluationCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self) -> EvaluationSet:
        ids = [case.id for case in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("evaluation case IDs must be unique")
        return self


class BakeoffCaseResult(BaseModel):
    """Automatic measurements for one model/case pair; no hidden total score."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    category: str
    profile: Literal["lite", "standard", "high"]
    writing_method: WritingMethod = "plan_first"
    success: bool
    elapsed_seconds: float = Field(ge=0)
    peak_process_tree_mb: float = Field(ge=0)
    model_artifact_bytes: int = Field(gt=0)
    section_count: int | None = Field(default=None, ge=1)
    validation_retries: int = Field(ge=0)
    estimated_duration_seconds: float | None = Field(default=None, gt=0)
    timing_error_percent: float | None = Field(default=None, ge=0)
    repeated_trigram_ratio: float | None = Field(default=None, ge=0, le=1)
    invitational_phrase_count: int | None = Field(default=None, ge=0)
    safety_validation_passed: bool
    failure: str | None = None

    @model_validator(mode="after")
    def consistent_success(self) -> BakeoffCaseResult:
        if self.success == (self.failure is not None):
            raise ValueError("success requires no failure; failure requires a message")
        return self


class BakeoffReport(BaseModel):
    """Machine-readable evidence plus explicit fields reserved for humans."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    evaluation_id: str
    evaluation_version: int
    created_at: AwareDatetime
    seed: int
    writing_method: WritingMethod = "plan_first"
    platform: str
    candidates: dict[str, AdapterMetadata]
    results: list[BakeoffCaseResult] = Field(min_length=1)
    human_review_status: Literal["pending", "complete"] = "pending"
    human_review_artifact: str | None = None
    decision: str = "No default changes until blind listening review is complete."


class EvaluationSetError(ValueError):
    """Raised when the versioned evaluation fixture is malformed."""


def load_evaluation_set(path: Path) -> EvaluationSet:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return EvaluationSet.model_validate(payload)
    except (OSError, ValidationError, yaml.YAMLError) as error:
        raise EvaluationSetError(f"Could not load evaluation set {path}: {error}") from error


ExperimentType = Literal["llm", "writing_method", "voice", "delivery", "pacing"]
ModelSelectionRole = Literal[
    "portable_default",
    "low_resource_fallback",
    "higher_quality",
]
MetricGoal = Literal["higher", "lower", "informational"]


class VoiceConfiguration(BaseModel):
    """One complete, named voice setup; its internals change as one Lab variable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    configuration_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    model_id: str = Field(min_length=1, max_length=200)
    voice_id: str = Field(min_length=1, max_length=200)
    voice_mode: Literal["preset", "reference", "designed"] = "preset"
    delivery: Literal[
        "neutral",
        "warm",
        "calm",
        "soothing",
        "grounded",
        "compassionate",
        "sleepy",
        "encouraging",
    ] = "calm"
    intensity: Literal["subtle", "moderate"] = "subtle"
    language: str = Field(default="English", min_length=2, max_length=64)
    instruction: str | None = Field(default=None, min_length=1, max_length=300)


class EvaluationCondition(BaseModel):
    """Complete production inputs for a trial, independent of adapter internals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    llm_model_id: str = Field(min_length=1, max_length=200)
    writing_method: WritingMethod
    voice: VoiceConfiguration
    pacing_profile_id: str = Field(min_length=1, max_length=128)


class ExperimentCandidate(BaseModel):
    """A human-friendly candidate with a fully inspectable condition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    candidate_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=120)
    condition: EvaluationCondition


class ExperimentDefinition(BaseModel):
    """Versioned comparison definition that permits exactly one logical variable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    experiment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,95}$")
    version: int = Field(ge=1)
    experiment_type: ExperimentType
    evaluation_id: str = Field(min_length=1)
    evaluation_version: int = Field(ge=1)
    seed: int = Field(ge=0)
    candidates: list[ExperimentCandidate] = Field(min_length=2, max_length=20)
    baseline_candidate_id: str

    @model_validator(mode="after")
    def validate_controlled_comparison(self) -> ExperimentDefinition:
        ids = [candidate.candidate_id for candidate in self.candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("experiment candidate IDs must be unique")
        if self.baseline_candidate_id not in ids:
            raise ValueError("baseline_candidate_id must identify a candidate")

        controlled_field = {
            "llm": "llm_model_id",
            "writing_method": "writing_method",
            "voice": "voice",
            "delivery": "voice.delivery_and_intensity",
            "pacing": "pacing_profile_id",
        }[self.experiment_type]
        first = self.candidates[0].condition.model_dump(mode="json")
        values: set[str] = set()
        for candidate in self.candidates:
            current = candidate.condition.model_dump(mode="json")
            if self.experiment_type == "delivery":
                fixed_first = dict(first)
                fixed_current = dict(current)
                first_voice = dict(fixed_first.pop("voice"))
                current_voice = dict(fixed_current.pop("voice"))
                first_voice.pop("delivery")
                first_voice.pop("intensity")
                current_controls = (
                    current_voice.pop("delivery"),
                    current_voice.pop("intensity"),
                )
                if fixed_current != fixed_first or current_voice != first_voice:
                    raise ValueError(
                        "delivery experiments may only change voice.delivery and "
                        f"voice.intensity; candidate {candidate.candidate_id} changes a "
                        "fixed model, identity, mode, language, LLM, method, or pacing input"
                    )
                values.add(repr(current_controls))
                continue
            changed = {key for key in first if current[key] != first[key]}
            if changed - {controlled_field}:
                unexpected = sorted(changed - {controlled_field})
                raise ValueError(
                    f"{self.experiment_type} experiments may only change {controlled_field}; "
                    f"candidate {candidate.candidate_id} also changes {unexpected}"
                )
            values.add(repr(current[controlled_field]))
        if len(values) != len(self.candidates):
            raise ValueError(f"every candidate must have a distinct {controlled_field}")
        return self


class ProductionTrialRequest(BaseModel):
    """Request passed to the real production pipeline, never a Lab-only generator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str
    experiment_version: int = Field(ge=1)
    trial_id: UUID
    case: EvaluationCase
    candidate_id: str
    condition: EvaluationCondition
    seed: int = Field(ge=0)
    experiment_type: Literal["llm", "writing_method", "voice", "delivery", "pacing"] | None = None
    controlled_source_run_id: UUID | None = None


class ArtifactReference(BaseModel):
    """Immutable pointer to an artifact emitted by a production run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,127}$")
    role: Literal["plan", "script", "timeline", "audio", "waveform", "qc", "manifest", "log"]
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    media_type: str = Field(min_length=1, max_length=120)


class MetricValue(BaseModel):
    """One transparent automatic measurement; combined scores are forbidden."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{1,63}$")
    value: float
    unit: str = Field(min_length=1, max_length=40)
    goal: MetricGoal

    @model_validator(mode="after")
    def reject_aggregate_score(self) -> MetricValue:
        forbidden = {"score", "total_score", "combined_score", "overall_score"}
        if self.name in forbidden:
            raise ValueError(
                "automatic metrics must remain separate; combined scores are forbidden"
            )
        return self


class ControlledInputFingerprints(BaseModel):
    """Hashes used to prove the supposedly fixed inputs really stayed fixed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    blueprint_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generation_schema_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    spoken_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Added after the first Local V1 evidence receipts.  Old reports remain
    # readable, while every newly executed controlled comparison requires it.
    speech_controls_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    silence_timeline_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    # The ordered sequence of every SPEECH/SILENCE/BREATH segment, including
    # immutable segment attributes but excluding the explicitly allowlisted
    # pacing values.  Optional only so historical evidence remains readable.
    timeline_topology_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pacing_profile_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class CanonicalSourceProvenance(BaseModel):
    """Exact canonical timeline from which a controlled candidate was derived."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    source_run_id: UUID
    source_revision_id: str = Field(pattern=r"^(original|r[0-9]{4}-[a-z0-9]{10})$")
    artifact_role: Literal["timeline"] = "timeline"
    artifact_path: str = Field(min_length=1)
    artifact_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    topology_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ProductionTrialResult(BaseModel):
    """Evidence returned by one invocation of the production pipeline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    trial_id: UUID
    source_run_id: UUID
    controlled_source_run_id: UUID | None = None
    candidate_id: str
    case_id: str
    success: bool
    actual_condition: EvaluationCondition
    started_at: AwareDatetime
    completed_at: AwareDatetime
    artifacts: list[ArtifactReference] = Field(default_factory=list)
    metrics: list[MetricValue] = Field(default_factory=list)
    adapter_metadata: dict[str, AdapterMetadata] = Field(default_factory=dict)
    hardware: dict[str, Any] = Field(default_factory=dict)
    controlled_inputs: ControlledInputFingerprints | None = None
    canonical_source: CanonicalSourceProvenance | None = None
    failure: str | None = Field(default=None, max_length=2_000)

    @model_validator(mode="after")
    def validate_result(self) -> ProductionTrialResult:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.success == (self.failure is not None):
            raise ValueError("successful trials have no failure; failed trials require one")
        successful_roles = {item.role for item in self.artifacts}
        if self.success and not {"manifest", "audio"}.issubset(successful_roles):
            raise ValueError(
                "successful production trials require run manifest and playable audio artifacts"
            )
        if self.success and self.controlled_inputs is None:
            raise ValueError("successful production trials require controlled-input fingerprints")
        if (
            self.canonical_source is not None
            and self.canonical_source.source_run_id != self.controlled_source_run_id
        ):
            raise ValueError("canonical-source provenance must identify controlled_source_run_id")
        if self.success and (not self.metrics or not self.adapter_metadata or not self.hardware):
            raise ValueError(
                "successful production trials require metrics, adapter metadata, and hardware"
            )
        names = [metric.name for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("metric names must be unique within a trial")
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("artifact IDs must be unique within a trial")
        return self


class BlindAssignment(BaseModel):
    """Randomized A/B presentation; candidate identities stay sealed until reveal."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: UUID
    case_id: str
    left_trial_id: UUID
    right_trial_id: UUID
    sealed_candidate_by_side: dict[Literal["left", "right"], str]

    @model_validator(mode="after")
    def distinct_sides(self) -> BlindAssignment:
        if self.left_trial_id == self.right_trial_id:
            raise ValueError("blind comparison requires two different trials")
        if set(self.sealed_candidate_by_side) != {"left", "right"}:
            raise ValueError("blind assignment must seal both sides")
        return self


class BlindPresentation(BaseModel):
    """Reviewer-safe A/B projection with no model or candidate identity."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: UUID
    case_id: str
    left_media_token: UUID
    right_media_token: UUID


class HumanRating(BaseModel):
    """One 1-5 judgment attached to a blind presentation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rating_id: UUID
    assignment_id: UUID
    dimension: Literal["content", "voice", "pacing", "naturalness", "safety", "preference"]
    score: int = Field(ge=1, le=5)
    preferred_side: Literal["left", "right", "tie"] | None = None
    notes: str = Field(default="", max_length=4_000)
    created_at: AwareDatetime


class RegressionMetric(BaseModel):
    """A named current-vs-accepted-baseline delta with no aggregate verdict."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    candidate_id: str
    metric_name: str
    baseline_value: float
    current_value: float
    delta: float
    goal: MetricGoal
    direction: Literal["improved", "regressed", "unchanged", "informational"]


class ExperimentReport(BaseModel):
    """Complete evidence bundle for one experiment revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    definition: ExperimentDefinition
    created_at: AwareDatetime
    platform: str = Field(min_length=1)
    trials: list[ProductionTrialResult] = Field(min_length=1)
    blind_assignments: list[BlindAssignment] = Field(default_factory=list)
    ratings: list[HumanRating] = Field(default_factory=list)
    regressions: list[RegressionMetric] = Field(default_factory=list)
    comparison_status: Literal["complete", "incomplete"]
    incomplete_candidates_by_case: dict[str, list[str]]

    @model_validator(mode="before")
    @classmethod
    def infer_explicit_completeness_for_legacy_payloads(cls, value: Any) -> Any:
        """Migrate older reports while making every new serialization explicit."""

        if not isinstance(value, dict) or "comparison_status" in value:
            return value
        definition = value.get("definition")
        trials = value.get("trials") or []
        if isinstance(definition, ExperimentDefinition):
            candidate_ids = {candidate.candidate_id for candidate in definition.candidates}
        else:
            candidate_ids = {
                str(candidate["candidate_id"])
                for candidate in (definition or {}).get("candidates", [])
            }
        case_ids: list[str] = []
        successful: dict[str, set[str]] = {}
        for trial in trials:
            if isinstance(trial, ProductionTrialResult):
                case_id = str(trial.case_id)
                candidate_id = str(trial.candidate_id)
                success = bool(trial.success)
            else:
                case_id = str(trial["case_id"])
                candidate_id = str(trial["candidate_id"])
                success = bool(trial["success"])
            if case_id not in case_ids:
                case_ids.append(case_id)
            if success:
                successful.setdefault(case_id, set()).add(candidate_id)
        incomplete = {
            case_id: sorted(candidate_ids - successful.get(case_id, set()))
            for case_id in case_ids
            if candidate_ids - successful.get(case_id, set())
        }
        migrated = dict(value)
        migrated["comparison_status"] = "incomplete" if incomplete else "complete"
        migrated["incomplete_candidates_by_case"] = incomplete
        return migrated

    @model_validator(mode="after")
    def references_are_internal(self) -> ExperimentReport:
        trial_ids = {trial.trial_id for trial in self.trials}
        if len(trial_ids) != len(self.trials):
            raise ValueError("production trial IDs must be unique within a report")
        trial_keys = {(trial.case_id, trial.candidate_id) for trial in self.trials}
        if len(trial_keys) != len(self.trials):
            raise ValueError("each case/candidate may appear only once within a report")
        expected_candidates = {candidate.candidate_id for candidate in self.definition.candidates}
        if any(trial.candidate_id not in expected_candidates for trial in self.trials):
            raise ValueError("production trials must reference definition candidates")
        if any(
            assignment.left_trial_id not in trial_ids or assignment.right_trial_id not in trial_ids
            for assignment in self.blind_assignments
        ):
            raise ValueError("blind assignments must reference report trials")
        assignment_ids = {assignment.assignment_id for assignment in self.blind_assignments}
        if len(assignment_ids) != len(self.blind_assignments):
            raise ValueError("blind assignment IDs must be unique within a report")
        trials_by_id = {trial.trial_id: trial for trial in self.trials}
        assignment_keys: set[tuple[str, frozenset[str]]] = set()
        for assignment in self.blind_assignments:
            left = trials_by_id[assignment.left_trial_id]
            right = trials_by_id[assignment.right_trial_id]
            if not left.success or not right.success:
                raise ValueError("blind assignments may reference only successful trials")
            if left.case_id != assignment.case_id or right.case_id != assignment.case_id:
                raise ValueError("blind assignment case must match both referenced trials")
            expected_seal = {"left": left.candidate_id, "right": right.candidate_id}
            if assignment.sealed_candidate_by_side != expected_seal:
                raise ValueError("blind assignment seal must match its referenced trials")
            pair_key = (assignment.case_id, frozenset(expected_seal.values()))
            if pair_key in assignment_keys:
                raise ValueError("each candidate pair may be assigned only once per case")
            assignment_keys.add(pair_key)
        if any(rating.assignment_id not in assignment_ids for rating in self.ratings):
            raise ValueError("ratings must reference report blind assignments")
        rating_ids = {rating.rating_id for rating in self.ratings}
        if len(rating_ids) != len(self.ratings):
            raise ValueError("rating IDs must be unique within a report")
        rating_keys = {(rating.assignment_id, rating.dimension) for rating in self.ratings}
        if len(rating_keys) != len(self.ratings):
            raise ValueError("each blind assignment may have only one rating per dimension")
        successful: dict[str, set[str]] = {}
        case_ids: list[str] = []
        for trial in self.trials:
            if trial.case_id not in case_ids:
                case_ids.append(trial.case_id)
            if trial.success:
                successful.setdefault(trial.case_id, set()).add(trial.candidate_id)
        expected_incomplete = {
            case_id: sorted(expected_candidates - successful.get(case_id, set()))
            for case_id in case_ids
            if expected_candidates - successful.get(case_id, set())
        }
        expected_status = "incomplete" if expected_incomplete else "complete"
        if self.comparison_status != expected_status:
            raise ValueError("comparison_status does not match successful candidate evidence")
        if self.incomplete_candidates_by_case != expected_incomplete:
            raise ValueError(
                "incomplete_candidates_by_case does not match successful candidate evidence"
            )
        return self


class PromotionDecision(BaseModel):
    """Versioned evidence record for accepting or rejecting a Studio default."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # V1 records had no model-selection role.  They remain readable for audit
    # and migration, but cannot satisfy the Local V1 model-role release gate.
    schema_version: Literal[1, 2, 3] = 3
    decision_id: UUID
    created_at: AwareDatetime
    experiment_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,95}$")
    experiment_version: int = Field(ge=1)
    outcome: Literal["promote", "reject"]
    studio_target: Literal["llm", "writing_method", "voice", "delivery", "pacing"]
    selection_role: ModelSelectionRole | None = None
    candidate_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    previous_value: str = Field(min_length=1)
    selected_value: str = Field(min_length=1)
    rationale: str = Field(min_length=1, max_length=4_000)
    report_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    rating_ids: list[UUID] = Field(default_factory=list)
    rating_sha256_by_id: dict[str, str] | None = None

    @model_validator(mode="after")
    def validate_selection_role(self) -> PromotionDecision:
        if self.schema_version == 1 and self.selection_role is not None:
            raise ValueError("legacy promotion decisions cannot declare a model selection role")
        if self.outcome == "reject" and self.selection_role is not None:
            raise ValueError("rejected candidates cannot be assigned a model selection role")
        if self.selection_role is not None and self.studio_target != "llm":
            raise ValueError("model selection roles are only valid for LLM decisions")
        if (
            self.schema_version in {2, 3}
            and self.outcome == "promote"
            and self.studio_target == "llm"
            and self.selection_role is None
        ):
            raise ValueError("new LLM promotions require a model selection role")
        if len(self.rating_ids) != len(set(self.rating_ids)):
            raise ValueError("promotion decision rating IDs must be unique")
        if self.schema_version < 3 and self.rating_sha256_by_id is not None:
            raise ValueError("legacy promotion decisions cannot declare rating content hashes")
        if self.schema_version == 3:
            hashes = self.rating_sha256_by_id or {}
            expected = {str(rating_id) for rating_id in self.rating_ids}
            if self.outcome == "promote" and not expected:
                raise ValueError("promotions require durable supporting ratings")
            if set(hashes) != expected:
                raise ValueError("rating content hashes must match every supporting rating ID")
            if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in hashes.values()):
                raise ValueError("rating content hashes must be lowercase SHA-256 digests")
        return self


class QueueEntry(BaseModel):
    """Sequential execution state for one expensive Lab experiment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    experiment_id: str
    experiment_version: int = Field(ge=1)
    status: Literal["queued", "running", "completed", "failed", "cancelled"] = "queued"
    enqueued_at: AwareDatetime
    started_at: AwareDatetime | None = None
    completed_at: AwareDatetime | None = None
    failure: str | None = None

    @model_validator(mode="after")
    def validate_state_timestamps(self) -> QueueEntry:
        if self.status == "queued" and (
            self.started_at is not None or self.completed_at is not None or self.failure is not None
        ):
            raise ValueError("queued entries cannot have execution state")
        if self.status == "running" and (
            self.started_at is None or self.completed_at is not None or self.failure is not None
        ):
            raise ValueError("running entries require only started_at")
        if self.status in {"completed", "cancelled"} and (
            self.completed_at is None or self.failure is not None
        ):
            raise ValueError("completed and cancelled entries require completion without failure")
        if self.status == "failed" and (self.completed_at is None or not self.failure):
            raise ValueError("failed entries require completion and a failure message")
        return self


def candidate_pairs(definition: ExperimentDefinition) -> list[tuple[str, str]]:
    """Return stable candidate pairs used to create blind A/B assignments."""

    return [
        (left.candidate_id, right.candidate_id)
        for left, right in combinations(definition.candidates, 2)
    ]


def load_experiment_definition(path: Path) -> ExperimentDefinition:
    """Load and strictly validate one versioned controlled comparison."""

    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        return ExperimentDefinition.model_validate(payload)
    except (OSError, ValidationError, yaml.YAMLError) as error:
        raise EvaluationSetError(f"Could not load experiment definition {path}: {error}") from error
