"""Fail-closed consolidation of every Local V1 acceptance evidence family."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from whoopy.evaluation.models import ExperimentReport, HumanRating, PromotionDecision
from whoopy.model_packs import classify_model_eligibility, load_model_pack_registry
from whoopy.release.acceptance import (
    INTERRUPTION_STAGES,
    AcceptanceReport,
    AcceptanceSuite,
    InterruptionStage,
)
from whoopy.release.evidence import CrossPlatformReleaseEvidence, aggregate_platform_reports
from whoopy.release.network_isolation import valid_macos_network_isolation_evidence

GateOutcome = Literal["passed", "failed", "missing", "stale", "unverified"]
RELEASE_TARGETS = (
    "windows-x86_64",
    "darwin-arm64",
    "darwin-x86_64",
    "linux-x86_64",
    "linux-arm64",
)
STUDIO_PACKS = ("kokoro", "fish-speech-1.4", "moss-local-5b", "moss-8b")
PORTABLE_STUDIO_PACK = "kokoro"
LAB_COMPARISONS = ("llm", "writing_method", "voice", "pacing")
STUDIO_LIFECYCLE_STEPS = (
    "health",
    "generate",
    "playback",
    "edit",
    "export",
    "resume",
    "delete",
)


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    modified_at: datetime


class AcceptanceGateStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    gate_id: str
    title: str
    required: bool = True
    outcome: GateOutcome
    detail: str
    blockers: tuple[str, ...] = ()
    sources: tuple[EvidenceReference, ...] = ()
    facts: dict[str, str | int | float | bool | None | list[str]] = Field(default_factory=dict)


class ModelReadinessStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str
    state: str
    compatible_on_current_machine: bool
    ready: bool
    studio_selectable: bool
    machine_resolution: Literal["playable", "proven_incompatibility", "unresolved"]
    remedy: str | None
    failed_checks: tuple[str, ...]


class LocalV1AcceptanceStatus(BaseModel):
    """One report where release readiness is derived, never operator-authored."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generated_at: datetime
    product_version: str
    declared_ci_targets: tuple[str, ...]
    independently_observed_platforms: dict[str, Literal["passed", "failed", "not_run"]]
    model_readiness: tuple[ModelReadinessStatus, ...]
    gates: tuple[AcceptanceGateStatus, ...]
    blockers: tuple[str, ...]
    local_v1_release_ready: bool

    @model_validator(mode="after")
    def _derived_release_status(self) -> LocalV1AcceptanceStatus:
        expected = all(not gate.required or gate.outcome == "passed" for gate in self.gates)
        expected = expected and all(
            self.independently_observed_platforms.get(target) == "passed"
            for target in self.declared_ci_targets
        )
        if self.local_v1_release_ready != expected:
            raise ValueError("local_v1_release_ready must be derived from every required gate")
        return self


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reference(path: Path) -> EvidenceReference:
    resolved = path.resolve()
    return EvidenceReference(
        path=resolved,
        sha256=_sha256(resolved),
        modified_at=datetime.fromtimestamp(resolved.stat().st_mtime, UTC),
    )


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("evidence root must be an object")
    return value


def _missing(gate_id: str, title: str, path: Path) -> AcceptanceGateStatus:
    return AcceptanceGateStatus(
        gate_id=gate_id,
        title=title,
        outcome="missing",
        detail="Required durable evidence is missing.",
        blockers=(f"Generate and preserve {path}.",),
    )


def assess_corpus(path: Path) -> AcceptanceGateStatus:
    title = "Ten-prompt content, safety, and human-rating corpus"
    if not path.is_file():
        return _missing("corpus", title, path)
    try:
        value = _json(path)
        expected = int(value["expected_case_count"])
        covered = int(value["covered_case_count"])
        passed = (
            value.get("accepted") is True
            and expected == 10
            and covered == expected
            and value.get("no_safety_failures") is True
            and value.get("complete_human_coverage") is True
            and value.get("complete_baseline_content_comparison") is True
            and value.get("content_improves_over_baseline") is True
            and float(value["content_median"]) >= 4
            and float(value["voice_median"]) >= 4
            and int(value["lowest_required_human_score"]) >= 3
        )
        blockers = tuple(str(item) for item in value.get("blockers", []))
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        return AcceptanceGateStatus(
            gate_id="corpus",
            title=title,
            outcome="failed",
            detail=f"Corpus evidence is invalid: {error}",
            blockers=("Regenerate the corpus acceptance receipt from immutable Lab evidence.",),
            sources=(_reference(path),),
        )
    return AcceptanceGateStatus(
        gate_id="corpus",
        title=title,
        outcome="passed" if passed else "failed",
        detail=(
            "All ten cases passed automatic safety and required blind human thresholds."
            if passed
            else "The corpus receipt does not satisfy every automatic and human threshold."
        ),
        blockers=() if passed else blockers or ("Corpus acceptance is false.",),
        sources=(_reference(path),),
        facts={"expected_cases": expected, "covered_cases": covered},
    )


def assess_soak(path: Path) -> AcceptanceGateStatus:
    title = "Twenty-run primary-machine finished-export soak"
    if not path.is_file():
        return _missing("soak_exports", title, path)
    try:
        value = _json(path)
        cases = value["cases"]
        counts = value["duration_counts"]
        passed = (
            value.get("evidence_scope") == "primary_machine_soak_finished_exports"
            and value.get("passed") is True
            and not value.get("errors")
            and isinstance(cases, list)
            and len(cases) == 20
            and all(case.get("passed") is True for case in cases)
            and all(
                int(counts.get(str(minutes), counts.get(minutes, 0))) == 4
                for minutes in (1, 3, 5, 10, 20)
            )
            and all(
                set(case.get("files", {})) == {"wav", "flac", "aac", "opus"}
                and all(item.get("decode_verified") is True for item in case["files"].values())
                for case in cases
            )
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        return AcceptanceGateStatus(
            gate_id="soak_exports",
            title=title,
            outcome="failed",
            detail=f"Soak evidence is invalid: {error}",
            blockers=("Regenerate the export soak receipt from the authoritative runs.",),
            sources=(_reference(path),),
        )
    return AcceptanceGateStatus(
        gate_id="soak_exports",
        title=title,
        outcome="passed" if passed else "failed",
        detail=(
            "Four valid WAV/FLAC/AAC/Opus runs passed at every required duration."
            if passed
            else "The soak receipt does not prove 20 complete finished-export runs."
        ),
        blockers=() if passed else ("Re-run and verify the complete 20-run export soak.",),
        sources=(_reference(path),),
        facts={"case_count": len(cases) if isinstance(cases, list) else 0},
    )


def assess_interruptions(path: Path) -> AcceptanceGateStatus:
    title = "Forced interruption and checkpoint recovery"
    if not path.is_file():
        return _missing("forced_interruption", title, path)
    try:
        report = AcceptanceReport.model_validate_json(path.read_text("utf-8"))
        by_stage = {case.stage: case for case in report.cases if case.kind == "forced_interruption"}

        def recovered(stage: InterruptionStage) -> bool:
            case = by_stage.get(stage)
            return bool(
                case is not None
                and case.passed
                and case.interruption_observed is True
                and case.stage_marker is not None
                and case.stage_marker.stage == stage
                and case.recovery is not None
                and case.recovery.return_code == 0
            )

        passed = set(by_stage) == set(INTERRUPTION_STAGES) and all(
            recovered(stage) for stage in INTERRUPTION_STAGES
        )
    except (OSError, ValidationError, ValueError) as error:
        return AcceptanceGateStatus(
            gate_id="forced_interruption",
            title=title,
            outcome="failed",
            detail=f"Interruption evidence is invalid: {error}",
            blockers=("Run all six forced-interruption cases and their recoveries.",),
            sources=(_reference(path),),
        )
    return AcceptanceGateStatus(
        gate_id="forced_interruption",
        title=title,
        outcome="passed" if passed else "failed",
        detail=(
            "Every required stage was interrupted and recovered with return code zero."
            if passed
            else "One or more required interruption stages lacks complete recovery evidence."
        ),
        blockers=() if passed else ("Complete planning through QC interruption recovery.",),
        sources=(_reference(path),),
        facts={"covered_stages": sorted(str(stage) for stage in by_stage if stage is not None)},
    )


def assess_studio_lifecycle(path: Path) -> AcceptanceGateStatus:
    title = "No-CLI Studio lifecycle"
    if not path.is_file():
        return _missing("studio_lifecycle", title, path)
    try:
        value = _json(path)
        steps = {
            str(item.get("step")): item.get("passed") is True
            for item in value.get("steps", [])
            if isinstance(item, dict)
        }
        passed = (
            value.get("evidence_scope") == "localhost_studio_lifecycle_not_cross_platform_release"
            and value.get("status") == "passed"
            and all(steps.get(step) is True for step in STUDIO_LIFECYCLE_STEPS)
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return AcceptanceGateStatus(
            gate_id="studio_lifecycle",
            title=title,
            outcome="failed",
            detail=f"Studio lifecycle evidence is invalid: {error}",
            blockers=("Re-run the localhost Studio lifecycle harness.",),
            sources=(_reference(path),),
        )
    missing = [step for step in STUDIO_LIFECYCLE_STEPS if steps.get(step) is not True]
    return AcceptanceGateStatus(
        gate_id="studio_lifecycle",
        title=title,
        outcome="passed" if passed else "failed",
        detail=(
            "Studio generated, played, edited, exported, resumed, and recoverably deleted "
            "meditations."
            if passed
            else "The Studio receipt is failed or missing required point-and-click lifecycle steps."
        ),
        blockers=()
        if passed
        else tuple([f"Missing passing Studio step: {step}" for step in missing]),
        sources=(_reference(path),),
        facts={"passed_steps": sorted(step for step, okay in steps.items() if okay)},
    )


def assess_models(
    registry_path: Path,
    models_root: Path,
    *,
    verify_digests: bool,
) -> tuple[AcceptanceGateStatus, tuple[ModelReadinessStatus, ...]]:
    """Inspect every Studio model; skipped digests can never yield a pass."""

    title = "Every Studio-selectable voice model produces current real output"
    try:
        registry = load_model_pack_registry(registry_path, models_root=models_root)
        inspected = {
            status.pack_id: status
            for status in registry.list_statuses(verify_digests=verify_digests)
        }
        statuses = [inspected[pack_id] for pack_id in STUDIO_PACKS]
    except (OSError, KeyError, ValueError) as error:
        gate = AcceptanceGateStatus(
            gate_id="model_readiness",
            title=title,
            outcome="failed",
            detail=f"Model readiness could not be inspected: {error}",
            blockers=("Repair the model registry before release assessment.",),
            sources=((_reference(registry_path),) if registry_path.is_file() else ()),
        )
        return gate, ()

    models_list: list[ModelReadinessStatus] = []
    for status in statuses:
        spec = registry.get(status.pack_id)
        eligibility = classify_model_eligibility(
            status.pack_id,
            status.state,
            status.checks,
            supported_platforms=spec.supported_platforms,
        )
        models_list.append(
            ModelReadinessStatus(
                pack_id=status.pack_id,
                state=status.state.value,
                compatible_on_current_machine=(
                    eligibility.machine_resolution != "proven_incompatibility"
                ),
                ready=eligibility.machine_resolution == "playable",
                studio_selectable=eligibility.studio_selectable,
                machine_resolution=eligibility.machine_resolution,
                remedy=eligibility.remedy,
                failed_checks=tuple(check.message for check in status.checks if not check.passed),
            )
        )
    models = tuple(models_list)
    portable_ready = next(item for item in models if item.pack_id == PORTABLE_STUDIO_PACK).ready
    dispositions_resolved = all(item.machine_resolution != "unresolved" for item in models)
    passed = verify_digests and portable_ready and dispositions_resolved
    blockers = []
    if not verify_digests:
        blockers.append("Full checkpoint digest verification was not requested for this report.")
    if not portable_ready:
        portable = next(item for item in models if item.pack_id == PORTABLE_STUDIO_PACK)
        blockers.append(
            f"Portable Studio voice {portable.pack_id} is {portable.state}: {portable.remedy}"
        )
    blockers.extend(
        f"{item.pack_id} has no current playable output or proven incompatibility: {item.remedy}"
        for item in models
        if item.machine_resolution == "unresolved" and item.pack_id != PORTABLE_STUDIO_PACK
    )
    outcome: GateOutcome = "passed" if passed else "unverified" if not verify_digests else "failed"
    return (
        AcceptanceGateStatus(
            gate_id="model_readiness",
            title=title,
            outcome=outcome,
            detail=(
                "Every selectable Studio voice is currently playable; unavailable optional "
                "voices have current, actionable incompatibility evidence."
                if passed
                else (
                    "Studio model readiness is incomplete; static or historical records do not "
                    "count."
                )
            ),
            blockers=tuple(blockers),
            sources=((_reference(registry_path),) if registry_path.is_file() else ()),
            facts={
                "digest_verification_performed": verify_digests,
                "ready_packs": [item.pack_id for item in models if item.ready],
                "studio_selectable_packs": [
                    item.pack_id for item in models if item.studio_selectable
                ],
                "proven_incompatible_packs": [
                    item.pack_id
                    for item in models
                    if item.machine_resolution == "proven_incompatibility"
                ],
                "unresolved_packs": [
                    item.pack_id for item in models if item.machine_resolution == "unresolved"
                ],
            },
        ),
        models,
    )


def assess_lab(lab_root: Path) -> tuple[AcceptanceGateStatus, AcceptanceGateStatus]:
    """Separate production comparison coverage from human promotion decisions."""

    reports: list[tuple[Path, ExperimentReport]] = []
    invalid: list[str] = []
    for path in sorted((lab_root / "experiments").glob("*/v*/report.json")):
        try:
            reports.append((path, ExperimentReport.model_validate_json(path.read_text("utf-8"))))
        except (OSError, ValidationError) as error:
            invalid.append(f"{path}: {error}")
    # Immutable revisions are audit history, not a mutable "current" result.  The
    # highest valid report revision for each experiment is authoritative for the
    # current comparison gate.  Otherwise a superseded report whose source run was
    # legitimately repaired would poison every future release forever.  We never
    # fall back when the latest report is stale: that revision is checked below and
    # fails closed.
    current_reports_by_experiment: dict[str, tuple[Path, ExperimentReport]] = {}
    for report_path, report in reports:
        existing_report = current_reports_by_experiment.get(report.definition.experiment_id)
        if (
            existing_report is None
            or report.definition.version > existing_report[1].definition.version
        ):
            current_reports_by_experiment[report.definition.experiment_id] = (
                report_path,
                report,
            )
    current_reports = list(current_reports_by_experiment.values())

    complete_types: set[str] = set()
    runs_root = lab_root.parent.resolve()

    def artifacts_are_valid(report_path: Path, report: ExperimentReport) -> bool:
        artifacts_valid = True
        for trial in report.trials:
            if not trial.success:
                continue
            for artifact in trial.artifacts:
                artifact_path = Path(artifact.path)
                try:
                    resolved_artifact = artifact_path.resolve(strict=True)
                    resolved_artifact.relative_to(runs_root)
                    if (
                        not resolved_artifact.is_file()
                        or _sha256(resolved_artifact) != artifact.sha256
                    ):
                        raise ValueError("content hash does not match")
                except (OSError, ValueError) as error:
                    artifacts_valid = False
                    invalid.append(
                        f"{report_path}: artifact {artifact.artifact_id} is missing, outside the "
                        f"managed runs root, or stale ({error})."
                    )
        return artifacts_valid

    validated_report_revisions: set[tuple[str, int]] = set()
    for report_path, report in current_reports:
        artifacts_valid = artifacts_are_valid(report_path, report)
        validated_report_revisions.add((report.definition.experiment_id, report.definition.version))
        if (
            report.comparison_status == "complete"
            and report.blind_assignments
            and artifacts_valid
            and all(
                trial.success
                and trial.controlled_inputs is not None
                and {artifact.role for artifact in trial.artifacts} >= {"manifest", "audio"}
                for trial in report.trials
            )
        ):
            complete_types.add(report.definition.experiment_type)
    missing_types = sorted(set(LAB_COMPARISONS) - complete_types)
    comparison_passed = not missing_types and not invalid
    report_sources = tuple(_reference(path) for path, _report in reports)
    comparison_gate = AcceptanceGateStatus(
        gate_id="lab_comparisons",
        title="Production-backed controlled Lab comparisons",
        outcome="passed" if comparison_passed else "failed" if reports else "missing",
        detail=(
            "LLM, writing-method, voice, and pacing comparisons have complete production artifacts."
            if comparison_passed
            else "The Lab lacks one or more complete controlled production comparisons."
        ),
        blockers=tuple(
            [f"Missing complete Lab comparison: {kind}" for kind in missing_types] + invalid
        ),
        sources=report_sources,
        facts={"complete_comparison_types": sorted(complete_types), "report_count": len(reports)},
    )

    ratings: list[HumanRating] = []
    ratings_by_revision: dict[tuple[str, int], dict[str, HumanRating]] = {}
    rating_paths: list[Path] = []
    for path in sorted((lab_root / "experiments").glob("*/v*/ratings/*.json")):
        try:
            rating = HumanRating.model_validate_json(path.read_text("utf-8"))
            experiment_id = path.parents[2].name
            version_text = path.parents[1].name
            if not version_text.startswith("v"):
                raise ValueError("rating is outside a versioned experiment directory")
            key = (experiment_id, int(version_text[1:]))
            existing = ratings_by_revision.setdefault(key, {}).get(str(rating.rating_id))
            if existing is not None and existing != rating:
                raise ValueError("conflicting duplicate rating ID")
            ratings_by_revision[key][str(rating.rating_id)] = rating
            ratings.append(rating)
            rating_paths.append(path)
        except (OSError, ValidationError, ValueError) as error:
            invalid.append(f"{path}: {error}")
    reports_by_revision: dict[tuple[str, int], ExperimentReport] = {}
    for _path, report in reports:
        key = (report.definition.experiment_id, report.definition.version)
        reports_by_revision[key] = report
        revision_ratings = ratings_by_revision.setdefault(key, {})
        for rating in report.ratings:
            existing = revision_ratings.get(str(rating.rating_id))
            if existing is not None and existing != rating:
                invalid.append(
                    f"Report {report.definition.experiment_id} v{report.definition.version} "
                    f"conflicts with separately stored rating {rating.rating_id}."
                )
            revision_ratings[str(rating.rating_id)] = rating
        ratings.extend(report.ratings)
    decisions: list[PromotionDecision] = []
    decision_paths: list[Path] = []
    for path in sorted((lab_root / "decisions").glob("*.json")):
        try:
            decisions.append(PromotionDecision.model_validate_json(path.read_text("utf-8")))
            decision_paths.append(path)
        except (OSError, ValidationError) as error:
            invalid.append(f"{path}: {error}")
    for decision in decisions:
        key = (decision.experiment_id, decision.experiment_version)
        report_path = (
            lab_root
            / "experiments"
            / decision.experiment_id
            / f"v{decision.experiment_version}"
            / "report.json"
        )
        if not report_path.is_file() or _sha256(report_path) != decision.report_sha256:
            invalid.append(f"Decision {decision.decision_id} report hash is missing or stale.")
        supporting = ratings_by_revision.get(key, {})
        decision_rating_ids = {str(rating_id) for rating_id in decision.rating_ids}
        if (
            decision.outcome == "promote" and not decision_rating_ids
        ) or not decision_rating_ids.issubset(supporting):
            invalid.append(
                f"Decision {decision.decision_id} lacks its durable supporting rating IDs."
            )
        supporting_report = reports_by_revision.get(key)
        if supporting_report is None:
            invalid.append(f"Decision {decision.decision_id} has no valid experiment report.")
        else:
            if key not in validated_report_revisions:
                supporting_path = (
                    lab_root
                    / "experiments"
                    / supporting_report.definition.experiment_id
                    / f"v{supporting_report.definition.version}"
                    / "report.json"
                )
                artifacts_are_valid(supporting_path, supporting_report)
                validated_report_revisions.add(key)
            if (
                decision.studio_target != supporting_report.definition.experiment_type
                or decision.candidate_id
                not in {
                    candidate.candidate_id for candidate in supporting_report.definition.candidates
                }
            ):
                invalid.append(
                    f"Decision {decision.decision_id} does not match its report definition."
                )
        if decision.outcome == "promote" and decision.schema_version < 3:
            invalid.append(
                f"Decision {decision.decision_id} predates rating-content binding and cannot "
                "satisfy the current release gate."
            )
        hashes = decision.rating_sha256_by_id or {}
        for rating_id in decision_rating_ids:
            supporting_rating = supporting.get(rating_id)
            if supporting_rating is None:
                continue
            actual = hashlib.sha256(
                json.dumps(
                    supporting_rating.model_dump(mode="json"),
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            if hashes.get(rating_id) != actual:
                invalid.append(
                    f"Decision {decision.decision_id} supporting rating {rating_id} is stale."
                )
    promoted = {decision.studio_target for decision in decisions if decision.outcome == "promote"}
    required_promotions = set(LAB_COMPARISONS)
    missing_promotions = sorted(required_promotions - promoted)
    role_blockers, model_role_candidates = _model_selection_role_evidence(decisions)
    decision_passed = bool(ratings) and not missing_promotions and not role_blockers and not invalid

    decision_gate = AcceptanceGateStatus(
        gate_id="lab_human_decisions",
        title="Blind Lab ratings and versioned Studio-default decisions",
        outcome=(
            "passed"
            if decision_passed
            else "missing"
            if not ratings and not decisions and not invalid
            else "failed"
        ),
        detail=(
            "Human ratings support saved default decisions for content, voice, and pacing stages."
            if decision_passed
            else "Automated comparisons alone do not select Local V1 defaults."
        ),
        blockers=tuple(
            (["No durable blind Lab ratings exist."] if not ratings else [])
            + [f"No promoted Studio decision for: {target}" for target in missing_promotions]
            + role_blockers
            + invalid
        ),
        sources=tuple(_reference(path) for path in [*rating_paths, *decision_paths]),
        facts={
            "rating_count": len(ratings),
            "decision_count": len(decisions),
            "promoted_targets": sorted(promoted),
            "promoted_model_roles": sorted(model_role_candidates),
            "portable_default_candidates": sorted(
                model_role_candidates.get("portable_default", set())
            ),
            "low_resource_fallback_candidates": sorted(
                model_role_candidates.get("low_resource_fallback", set())
            ),
            "higher_quality_candidates": sorted(model_role_candidates.get("higher_quality", set())),
        },
    )
    return comparison_gate, decision_gate


def _model_selection_role_evidence(
    decisions: list[PromotionDecision],
) -> tuple[list[str], dict[str, set[str]]]:
    """Require distinct, evidence-bearing portable and fallback model selections."""

    model_role_candidates: dict[str, set[str]] = {}
    for decision in decisions:
        if (
            decision.outcome == "promote"
            and decision.studio_target == "llm"
            and decision.selection_role is not None
        ):
            model_role_candidates.setdefault(decision.selection_role, set()).add(
                decision.candidate_id
            )
    required_model_roles = {"portable_default", "low_resource_fallback"}
    missing_model_roles = sorted(required_model_roles - set(model_role_candidates))
    required_roles_are_distinct = any(
        portable != fallback
        for portable in model_role_candidates.get("portable_default", set())
        for fallback in model_role_candidates.get("low_resource_fallback", set())
    )
    role_blockers = [
        f"No evidence-backed model decision for required role: {role}"
        for role in missing_model_roles
    ]
    if not missing_model_roles and not required_roles_are_distinct:
        role_blockers.append(
            "Portable default and low-resource fallback must select different model candidates."
        )
    return role_blockers, model_role_candidates


def _declared_package_data_files(project_root: Path) -> dict[Path, str]:
    project = tomllib.loads((project_root / "pyproject.toml").read_text("utf-8"))
    declarations = project.get("tool", {}).get("setuptools", {}).get("data-files", {})
    return {
        source: f"{destination}/{source.name}"
        for destination, patterns in declarations.items()
        for pattern in patterns
        for source in project_root.glob(pattern)
        if source.is_file()
    }


def _required_package_inputs(project_root: Path, data_files: dict[Path, str]) -> list[Path]:
    """Return every source that can change the installed Local V1 product.

    This deliberately mirrors the package-data and data-files declarations in
    ``pyproject.toml``.  Checking only Python modules and the built frontend can
    incorrectly bless a wheel after a model registry, prompt, acceptance suite,
    or isolated voice worker changed.
    """

    package = project_root / "src" / "whoopy"
    values = [project_root / "pyproject.toml", project_root / "uv.lock"]
    readme = project_root / "README.md"
    if readme.is_file():
        values.append(readme)
    for path in package.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(package)
        if (
            path.suffix == ".py"
            or relative.parts[:2] == ("webui", "static")
            or (relative.parts[:1] == ("resources",) and path.suffix in {".yaml", ".yml"})
        ):
            values.append(path)
    values.extend(data_files)
    return sorted(set(values))


def _wheel_member_for_source(
    project_root: Path, source: Path, version: str, data_files: dict[Path, str]
) -> str | None:
    """Map an installed product source to its exact wheel member."""

    try:
        relative = source.relative_to(project_root / "src" / "whoopy").as_posix()
    except ValueError:
        destination = data_files.get(source)
        return f"whoopy-{version}.data/data/{destination}" if destination is not None else None
    return f"whoopy/{relative}"


def _sdist_member_for_source(project_root: Path, source: Path, version: str) -> str | None:
    """Map a source distributed with Whoopy to its exact sdist member."""

    for root_name in ("src", "config", "scripts", "docs"):
        try:
            relative = source.relative_to(project_root / root_name).as_posix()
        except ValueError:
            continue
        return f"whoopy-{version}/{root_name}/{relative}"
    if source in {project_root / "README.md", project_root / "pyproject.toml"}:
        return f"whoopy-{version}/{source.name}"
    # uv.lock is an acquisition/build input, not intentionally part of the sdist.
    return None


def assess_package(project_root: Path, dist_directory: Path) -> AcceptanceGateStatus:
    """Require current-version wheel/sdist newer than and equal to package source."""

    title = "Current reproducible Local V1 package artifacts"
    pyproject = project_root / "pyproject.toml"
    try:
        version = str(tomllib.loads(pyproject.read_text("utf-8"))["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as error:
        return AcceptanceGateStatus(
            gate_id="package_freshness",
            title=title,
            outcome="failed",
            detail=f"Project version cannot be read: {error}",
            blockers=("Repair pyproject.toml and rebuild release artifacts.",),
        )
    wheels = sorted(dist_directory.glob(f"whoopy-{version}-*.whl"))
    sdists = sorted(dist_directory.glob(f"whoopy-{version}.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        return AcceptanceGateStatus(
            gate_id="package_freshness",
            title=title,
            outcome="missing",
            detail="Exactly one current-version wheel and source distribution are required.",
            blockers=(f"Build whoopy {version} wheel and sdist from the current tree.",),
            facts={"product_version": version},
        )
    wheel, sdist = wheels[0], sdists[0]
    data_files = _declared_package_data_files(project_root)
    inputs = _required_package_inputs(project_root, data_files)
    latest_input = max(path.stat().st_mtime_ns for path in inputs)
    stale_reasons: list[str] = []
    if wheel.stat().st_mtime_ns < latest_input or sdist.stat().st_mtime_ns < latest_input:
        stale_reasons.append("A package input is newer than the wheel or sdist.")
    with zipfile.ZipFile(wheel) as archive:
        wheel_names = {item.filename for item in archive.infolist() if not item.is_dir()}
        expected_wheel_sources = {
            packaged
            for source in inputs
            if (packaged := _wheel_member_for_source(project_root, source, version, data_files))
            is not None
        }
        wheel_metadata = {
            f"whoopy-{version}.dist-info/{name}"
            for name in ("METADATA", "WHEEL", "RECORD", "entry_points.txt", "top_level.txt")
        }
        if wheel_names - wheel_metadata != expected_wheel_sources:
            stale_reasons.append("Wheel source membership does not match the current source tree.")
        for source in inputs:
            packaged = _wheel_member_for_source(project_root, source, version, data_files)
            if packaged is None:
                continue
            if packaged not in wheel_names or archive.read(packaged) != source.read_bytes():
                stale_reasons.append(f"Wheel does not match current source: {source}")
                break
        metadata_names = [name for name in wheel_names if name.endswith(".dist-info/METADATA")]
        if len(metadata_names) != 1 or f"Version: {version}" not in archive.read(
            metadata_names[0]
        ).decode("utf-8"):
            stale_reasons.append("Wheel metadata version does not match pyproject.toml.")
    with tarfile.open(sdist, "r:gz") as archive:
        members = {item.name: item for item in archive.getmembers() if item.isfile()}
        sdist_metadata = {
            f"whoopy-{version}/src/whoopy.egg-info/{name}"
            for name in (
                "PKG-INFO",
                "SOURCES.txt",
                "dependency_links.txt",
                "entry_points.txt",
                "requires.txt",
                "top_level.txt",
            )
        } | {f"whoopy-{version}/PKG-INFO"}
        if not (project_root / "setup.cfg").exists():
            sdist_metadata.add(f"whoopy-{version}/setup.cfg")
        for name, member in members.items():
            if name in sdist_metadata:
                continue
            try:
                relative = Path(name).relative_to(f"whoopy-{version}")
            except ValueError:
                stale_reasons.append(f"Source distribution contains an unexpected member: {name}")
                break
            source = project_root / relative
            extracted = archive.extractfile(member)
            if (
                ".." in relative.parts
                or not source.is_file()
                or extracted is None
                or extracted.read() != source.read_bytes()
            ):
                stale_reasons.append(
                    f"Source distribution contains absent or changed source: {name}"
                )
                break
        for source in inputs:
            packaged = _sdist_member_for_source(project_root, source, version)
            if packaged is None:
                continue
            packaged_member = members.get(packaged)
            extracted = (
                archive.extractfile(packaged_member) if packaged_member is not None else None
            )
            if extracted is None or extracted.read() != source.read_bytes():
                stale_reasons.append(f"Source distribution does not match current source: {source}")
                break
    passed = not stale_reasons
    return AcceptanceGateStatus(
        gate_id="package_freshness",
        title=title,
        outcome="passed" if passed else "stale",
        detail=(
            "Current-version wheel and sdist match the packaged source tree."
            if passed
            else "Release artifacts do not match the current implementation."
        ),
        blockers=tuple(stale_reasons),
        sources=(_reference(wheel), _reference(sdist), _reference(pyproject)),
        facts={"product_version": version, "product_wheel_sha256": _sha256(wheel)},
    )


def assess_installed_bundles(
    paths: tuple[Path, ...],
    *,
    product_version: str,
    expected_product_wheel_sha256: str | None = None,
) -> AcceptanceGateStatus:
    """Require a fresh installed lifecycle under proven network isolation on every target."""

    title = "Fresh installed-bundle offline Studio lifecycle"
    if not paths:
        return AcceptanceGateStatus(
            gate_id="installed_bundle_lifecycle",
            title=title,
            outcome="missing",
            detail="No fresh installed-bundle lifecycle receipts were supplied.",
            blockers=("Collect one installed-bundle lifecycle receipt on every release target.",),
        )
    errors: list[str] = []
    targets: dict[str, dict[str, Any]] = {}
    sources: list[EvidenceReference] = []
    required_steps = set(STUDIO_LIFECYCLE_STEPS)

    def is_sha256(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    def valid_network_isolation(value: dict[str, Any], *, target: str) -> bool:
        attested = (
            value.get("operator_attested_network_disabled_during_test") is True
            and value.get("process_network_isolation_enforced") is False
            and value.get("network_isolation_evidence_kind") == "operator_attestation"
            and isinstance(value.get("network_isolation_note"), str)
            and bool(value["network_isolation_note"].strip())
        )
        if attested:
            return True
        evidence = value.get("network_isolation_evidence")
        return not (
            not target.startswith("darwin-")
            or value.get("operator_attested_network_disabled_during_test") is not False
            or value.get("process_network_isolation_enforced") is not True
            or value.get("network_isolation_evidence_kind") != "macos_sandbox_exec"
            or not isinstance(value.get("network_isolation_note"), str)
            or not value["network_isolation_note"].strip()
            or not valid_macos_network_isolation_evidence(evidence)
        )

    for path in paths:
        try:
            value = _json(path)
            sources.append(_reference(path))
            target = str(value["target"])
            if target not in RELEASE_TARGETS:
                raise ValueError(f"unsupported target {target!r}")
            if target in targets:
                raise ValueError(f"duplicate receipt for {target}")
            install = value["install_receipt"]
            lifecycle = value["studio_lifecycle_evidence"]
            interpreter = value["installed_interpreter"]
            if (
                not isinstance(install, dict)
                or not isinstance(lifecycle, dict)
                or not isinstance(interpreter, dict)
            ):
                raise ValueError(
                    "install, interpreter, and Studio lifecycle evidence must be objects"
                )
            lifecycle_identity = hashlib.sha256(
                json.dumps(lifecycle, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            install_receipt_identity = hashlib.sha256(
                (json.dumps(install, indent=2, sort_keys=True) + "\n").encode("utf-8")
            ).hexdigest()
            steps = {
                str(item.get("step")): item.get("passed") is True
                for item in lifecycle.get("steps", [])
                if isinstance(item, dict)
            }
            valid = (
                value.get("schema_version") == 1
                and value.get("evidence_scope")
                == "fresh_application_bundle_offline_after_acquisition"
                and value.get("product_version") == product_version
                and is_sha256(value.get("bundle_id"))
                and is_sha256(value.get("product_wheel_sha256"))
                and is_sha256(value.get("dependency_lock_sha256"))
                and value.get("packaging_passed") is True
                and value.get("offline_launch_passed") is True
                and value.get("offline_acceptance_passed") is True
                and valid_network_isolation(value, target=target)
                and value.get("studio_lifecycle_requested") is True
                and value.get("studio_lifecycle_exercised") is True
                and value.get("studio_lifecycle_passed") is True
                and value.get("voice_synthesis_tested") is True
                and value.get("api_started") is True
                and value.get("bundled_frontend_served") is True
                and value.get("installed_defaults_resolved_without_checkout") is True
                and value.get("bound_host") == "127.0.0.1"
                and value.get("independent_platform_evidence") is False
                and value.get("local_v1_release_ready") is False
                and value.get("studio_lifecycle_evidence_sha256") == lifecycle_identity
                and lifecycle.get("evidence_scope")
                == "localhost_studio_lifecycle_not_cross_platform_release"
                and lifecycle.get("status") == "passed"
                and lifecycle.get("local_v1_release_ready") is False
                and all(steps.get(step) is True for step in required_steps)
                and install.get("schema_version") == 2
                and value.get("install_receipt_sha256") == install_receipt_identity
                and install.get("bundle_id") == value.get("bundle_id")
                and install.get("bundle_target") == target
                and install.get("bundle_python") == value.get("python")
                and install.get("product_version") == product_version
                and install.get("product_wheel_sha256") == value.get("product_wheel_sha256")
                and install.get("bundle_integrity_verified") is True
                and install.get("bundle_authenticity_verified") is False
                and install.get("package_index_isolation_enforced") is True
                and install.get("process_network_isolation_enforced") is False
                and install.get("dependency_consistency_check_passed") is True
                and install.get("launcher_smoke_passed") is True
                and interpreter.get("python") == value.get("python")
                and interpreter.get("implementation") == "CPython"
                and interpreter.get("target") == target
            )
            if not valid:
                raise ValueError(
                    "receipt does not satisfy the installed offline lifecycle contract"
                )
            targets[target] = value
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
            errors.append(f"{path}: {error}")
    missing = sorted(set(RELEASE_TARGETS) - set(targets))
    errors.extend(f"Missing installed-bundle receipt for {target}." for target in missing)
    wheel_digests = {
        str(receipt["product_wheel_sha256"])
        for receipt in targets.values()
        if is_sha256(receipt.get("product_wheel_sha256"))
    }
    if len(wheel_digests) > 1:
        errors.append("Installed-bundle receipts do not share one product wheel digest.")
    if expected_product_wheel_sha256 is not None and wheel_digests != {
        expected_product_wheel_sha256
    }:
        errors.append("Installed-bundle receipts do not match the current packaged wheel digest.")
    return AcceptanceGateStatus(
        gate_id="installed_bundle_lifecycle",
        title=title,
        outcome="passed" if not errors else "failed",
        detail=(
            "Every target installed the release bundle and completed the offline Studio lifecycle."
            if not errors
            else "Installed-product offline lifecycle evidence is incomplete or invalid."
        ),
        blockers=tuple(errors),
        sources=tuple(sources),
        facts={
            "passed_targets": sorted(targets),
            "product_wheel_sha256": next(iter(wheel_digests), None),
        },
    )


def assess_platforms(
    *,
    suite: AcceptanceSuite,
    platform_reports: tuple[Path, ...] = (),
    cross_platform_evidence: Path | None = None,
    expected_product_wheel_sha256: str | None = None,
) -> tuple[
    AcceptanceGateStatus,
    AcceptanceGateStatus,
    dict[str, Literal["passed", "failed", "not_run"]],
]:
    """Accept only reports generated on their claimed machines with offline proof."""

    sources: tuple[EvidenceReference, ...] = ()
    reports: list[AcceptanceReport] = []
    try:
        if cross_platform_evidence is not None:
            if not cross_platform_evidence.is_file():
                raise ValueError(f"cross-platform evidence is missing: {cross_platform_evidence}")
            aggregate = CrossPlatformReleaseEvidence.model_validate_json(
                cross_platform_evidence.read_text("utf-8")
            )
            if aggregate.suite_id != suite.suite_id:
                raise ValueError("cross-platform evidence used a different acceptance suite")
            sources = (_reference(cross_platform_evidence),)
            observed = {entry.target.target_id: entry.status for entry in aggregate.platform_matrix}
            for imported in aggregate.imported_reports:
                source = imported.source_path
                if not source.is_file() or _sha256(source) != imported.sha256:
                    raise ValueError(
                        f"imported platform source is missing or changed: {imported.source_path}"
                    )
                report = AcceptanceReport.model_validate_json(source.read_text("utf-8"))
                reports.append(report)
            rebuilt = aggregate_platform_reports(
                suite=suite,
                report_paths=tuple(imported.source_path for imported in aggregate.imported_reports),
            )
            rebuilt_statuses = {
                entry.target.target_id: entry.status for entry in rebuilt.platform_matrix
            }
            if rebuilt_statuses != observed or {
                item.target_id: item.sha256 for item in rebuilt.imported_reports
            } != {item.target_id: item.sha256 for item in aggregate.imported_reports}:
                raise ValueError("cross-platform aggregate does not match its source reports")
            if rebuilt.product_wheel_sha256 != aggregate.product_wheel_sha256:
                raise ValueError(
                    "cross-platform aggregate product wheel does not match its source reports"
                )
            expected_ready = all(observed.get(target) == "passed" for target in RELEASE_TARGETS)
            if aggregate.local_v1_release_ready != expected_ready:
                raise ValueError("cross-platform ready flag is inconsistent with its matrix")
        else:
            aggregate = aggregate_platform_reports(suite=suite, report_paths=platform_reports)
            observed = {entry.target.target_id: entry.status for entry in aggregate.platform_matrix}
            reports = [
                AcceptanceReport.model_validate_json(path.read_text("utf-8"))
                for path in platform_reports
            ]
            sources = tuple(_reference(path) for path in platform_reports)
        if reports:
            if expected_product_wheel_sha256 is None:
                raise ValueError(
                    "current packaged product wheel digest is unavailable for platform binding"
                )
            if aggregate.product_wheel_sha256 != expected_product_wheel_sha256 or any(
                report.product_wheel_sha256 != expected_product_wheel_sha256 for report in reports
            ):
                raise ValueError(
                    "independent platform reports do not match the current packaged product wheel"
                )
    except (OSError, ValidationError, ValueError) as error:
        observed = {target: "not_run" for target in RELEASE_TARGETS}
        platform_gate = AcceptanceGateStatus(
            gate_id="independent_platform_acceptance",
            title="Independent Windows, macOS, and Linux acceptance",
            outcome="failed" if platform_reports or cross_platform_evidence else "missing",
            detail=f"Independent platform evidence is unavailable or invalid: {error}",
            blockers=("Collect one complete independent acceptance report on every target.",),
            sources=sources,
        )
        offline_gate = AcceptanceGateStatus(
            gate_id="offline_operation",
            title="OS-network-disabled post-acquisition operation",
            outcome="missing",
            detail="No valid independent platform set proves offline operation.",
            blockers=("Run acceptance with networking disabled outside Whoopy.",),
            sources=sources,
        )
        return platform_gate, offline_gate, observed

    missing = [target for target in RELEASE_TARGETS if observed.get(target) != "passed"]
    platform_passed = not missing and len(reports) == len(RELEASE_TARGETS)
    platform_gate = AcceptanceGateStatus(
        gate_id="independent_platform_acceptance",
        title="Independent Windows, macOS, and Linux acceptance",
        outcome="passed" if platform_passed else "failed" if reports else "missing",
        detail=(
            "All five supported targets supplied their own complete acceptance report."
            if platform_passed
            else "CI target declarations or portability smokes do not prove platform acceptance."
        ),
        blockers=tuple(f"No passing independent report for {target}." for target in missing),
        sources=sources,
        facts={
            "passed_targets": sorted(
                target for target, state in observed.items() if state == "passed"
            ),
            "product_wheel_sha256": aggregate.product_wheel_sha256,
        },
    )

    def valid_platform_offline(report: AcceptanceReport) -> bool:
        offline = report.offline
        operator_proof = (
            offline.network_isolation_evidence_kind == "operator_attestation"
            and not offline.process_network_isolation_enforced
        )
        macos_proof = (
            report.target.operating_system == "darwin"
            and offline.network_isolation_evidence_kind == "macos_sandbox_exec"
            and offline.process_network_isolation_enforced
            and valid_macos_network_isolation_evidence(offline.network_isolation_evidence)
        )
        return bool(
            offline.passed
            and offline.network_disabled_during_test
            and offline.network_isolation_note
            and (operator_proof or macos_proof)
        )

    offline_failed = [
        report.target.target_id for report in reports if not valid_platform_offline(report)
    ]
    offline_passed = platform_passed and not offline_failed
    offline_gate = AcceptanceGateStatus(
        gate_id="offline_operation",
        title="OS-network-disabled post-acquisition operation",
        outcome="passed" if offline_passed else "failed" if reports else "missing",
        detail=(
            "Every independent target report includes explicit OS-network-disabled evidence."
            if offline_passed
            else "Offline process configuration is not OS-level network-isolation proof."
        ),
        blockers=tuple(
            [f"Offline acceptance failed for {target}." for target in offline_failed]
            + (["Offline proof requires all five platform reports."] if not platform_passed else [])
        ),
        sources=sources,
    )
    return platform_gate, offline_gate, observed


def assess_ci_portability(path: Path | None) -> AcceptanceGateStatus:
    """Report CI coverage as informational and explicitly non-accepting."""

    title = "Declared CI portability targets (not release acceptance)"
    if path is None or not path.is_file():
        return AcceptanceGateStatus(
            gate_id="ci_portability",
            title=title,
            required=False,
            outcome="missing",
            detail="No CI portability summary was supplied; this cannot affect release readiness.",
        )
    try:
        value = _json(path)
        targets = value.get("targets", {})
        valid_scope = value.get("evidence_scope") == "ci_portability_summary_not_release_acceptance"
        passed = (
            valid_scope
            and value.get("passed") is True
            and set(targets) == set(RELEASE_TARGETS)
            and all(
                receipt.get("evidence_scope") == "ci_portability_not_release_acceptance"
                and receipt.get("passed") is True
                and receipt.get("os_network_isolation_proven") is False
                for receipt in targets.values()
            )
        )
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        return AcceptanceGateStatus(
            gate_id="ci_portability",
            title=title,
            required=False,
            outcome="failed",
            detail=f"CI portability summary is invalid: {error}",
            sources=(_reference(path),),
        )
    return AcceptanceGateStatus(
        gate_id="ci_portability",
        title=title,
        required=False,
        outcome="passed" if passed else "failed",
        detail=(
            "CI exercised declared targets, but grants no independent platform or offline "
            "acceptance."
            if passed
            else "CI portability coverage is incomplete and grants no release acceptance."
        ),
        sources=(_reference(path),),
        facts={"declared_targets": sorted(str(target) for target in targets)},
    )


def build_local_v1_acceptance_status(
    *,
    project_root: Path,
    corpus_report: Path,
    soak_report: Path,
    interruption_report: Path,
    studio_lifecycle_report: Path,
    model_registry: Path,
    models_root: Path,
    lab_root: Path,
    acceptance_suite: AcceptanceSuite,
    dist_directory: Path,
    installed_bundle_reports: tuple[Path, ...] = (),
    platform_reports: tuple[Path, ...] = (),
    cross_platform_evidence: Path | None = None,
    ci_portability_summary: Path | None = None,
    verify_model_digests: bool = False,
    generated_at: datetime | None = None,
) -> LocalV1AcceptanceStatus:
    """Combine all authoritative evidence without converting absence into success."""

    model_gate, models = assess_models(
        model_registry, models_root, verify_digests=verify_model_digests
    )
    lab_comparisons, lab_decisions = assess_lab(lab_root)
    version = str(
        tomllib.loads((project_root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
    )
    package_gate = assess_package(project_root, dist_directory)
    packaged_wheel_sha256 = package_gate.facts.get("product_wheel_sha256")
    installed_bundle_gate = assess_installed_bundles(
        installed_bundle_reports,
        product_version=version,
        expected_product_wheel_sha256=(
            packaged_wheel_sha256 if isinstance(packaged_wheel_sha256, str) else None
        ),
    )
    platform_gate, offline_gate, observed = assess_platforms(
        suite=acceptance_suite,
        platform_reports=platform_reports,
        cross_platform_evidence=cross_platform_evidence,
        expected_product_wheel_sha256=(
            packaged_wheel_sha256 if isinstance(packaged_wheel_sha256, str) else None
        ),
    )
    if offline_gate.outcome == "passed" and installed_bundle_gate.outcome != "passed":
        offline_gate = offline_gate.model_copy(
            update={
                "outcome": installed_bundle_gate.outcome,
                "detail": (
                    "Network flags alone do not prove post-acquisition operation from the "
                    "installed product bundle."
                ),
                "blockers": installed_bundle_gate.blockers,
                "sources": (*offline_gate.sources, *installed_bundle_gate.sources),
            }
        )
    gates = (
        assess_corpus(corpus_report),
        assess_soak(soak_report),
        assess_interruptions(interruption_report),
        assess_studio_lifecycle(studio_lifecycle_report),
        model_gate,
        lab_comparisons,
        lab_decisions,
        installed_bundle_gate,
        platform_gate,
        offline_gate,
        package_gate,
        assess_ci_portability(ci_portability_summary),
    )
    blockers = tuple(
        blocker
        for gate in gates
        if gate.required and gate.outcome != "passed"
        for blocker in gate.blockers
    )
    ready = all(not gate.required or gate.outcome == "passed" for gate in gates) and all(
        observed.get(target) == "passed" for target in RELEASE_TARGETS
    )
    return LocalV1AcceptanceStatus(
        generated_at=generated_at or datetime.now(UTC),
        product_version=version,
        declared_ci_targets=RELEASE_TARGETS,
        independently_observed_platforms=observed,
        model_readiness=models,
        gates=gates,
        blockers=blockers,
        local_v1_release_ready=ready,
    )


def write_local_v1_acceptance_status(path: Path, status: LocalV1AcceptanceStatus) -> None:
    """Atomically persist the consolidated report and its evidence hashes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(status.model_dump_json(indent=2).encode("utf-8") + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
