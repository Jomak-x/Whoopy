"""Machine-readable offline, soak, and forced-interruption acceptance evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from whoopy import __version__
from whoopy.config import ConfigError, load_settings
from whoopy.hardware import HardwareSnapshot
from whoopy.model_packs import ModelPackError, ModelPackState, load_model_pack_registry
from whoopy.pipeline.acceptance_stage import (
    PAUSE_STAGE_ENV,
    STAGE_READY_FILE_ENV,
    AcceptanceStageMarker,
)
from whoopy.release.migrations import MigrationReport
from whoopy.release.network_isolation import valid_macos_network_isolation_evidence
from whoopy.release.platforms import (
    BackendSelection,
    PlatformMatrixEntry,
    ReleaseTarget,
    build_platform_matrix,
    detect_release_target,
    inspect_release_hardware,
    select_friendly_backend,
)

AcceptanceKind = Literal["soak", "forced_interruption"]
InterruptionStage = Literal["planning", "drafting", "synthesis", "assembly", "encoding", "qc"]
ExecutionDisposition = Literal["run", "pending", "external"]
INTERRUPTION_STAGES: tuple[InterruptionStage, ...] = (
    "planning",
    "drafting",
    "synthesis",
    "assembly",
    "encoding",
    "qc",
)


class AuditCheck(BaseModel):
    """One explicit offline-readiness condition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: str
    passed: bool
    detail: str


class OfflineAuditReport(BaseModel):
    """Post-acquisition evidence; this does not claim OS-level network isolation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    passed: bool
    network_disabled_during_test: bool
    network_isolation_evidence_kind: Literal[
        "none", "operator_attestation", "macos_sandbox_exec", "legacy_unspecified"
    ] = "legacy_unspecified"
    network_isolation_note: str | None = None
    process_network_isolation_enforced: bool = False
    network_isolation_evidence: dict[str, Any] | None = None
    required_pack_ids: tuple[str, ...]
    checks: tuple[AuditCheck, ...]

    @model_validator(mode="after")
    def _passed_requires_explicit_attestation(self) -> OfflineAuditReport:
        operator_proof = (
            self.network_disabled_during_test
            and self.network_isolation_evidence_kind == "operator_attestation"
            and bool(self.network_isolation_note)
            and not self.process_network_isolation_enforced
        )
        sandbox_proof = (
            self.network_disabled_during_test
            and self.network_isolation_evidence_kind == "macos_sandbox_exec"
            and bool(self.network_isolation_note)
            and self.process_network_isolation_enforced
            and valid_macos_network_isolation_evidence(self.network_isolation_evidence)
        )
        if self.passed and not (operator_proof or sandbox_proof):
            raise ValueError(
                "A passing offline audit requires operator-attested host isolation or a "
                "validated macOS sandbox proof."
            )
        return self


class AcceptanceCase(BaseModel):
    """One repeatable subprocess scenario, never interpreted through a shell."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    kind: AcceptanceKind
    command: tuple[str, ...] = Field(min_length=1)
    disposition: ExecutionDisposition = "run"
    evidence_note: str | None = Field(default=None, min_length=1)
    target_id: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    duration_minutes: int | None = Field(default=None, ge=1, le=30)
    repetitions: int = Field(default=1, ge=1, le=100)
    timeout_seconds: float = Field(default=600, gt=0, le=7_200)
    interruption_stage: InterruptionStage | None = None
    ready_file: Path | None = None
    interrupt_after_seconds: float | None = Field(default=None, gt=0, le=600)
    recovery_command: tuple[str, ...] | None = None
    evidence_contract: Literal["none", "production_receipt_v2"] = "none"

    @model_validator(mode="after")
    def _validate_kind(self) -> AcceptanceCase:
        if self.disposition != "run" and self.evidence_note is None:
            raise ValueError("pending and external cases require an evidence_note")
        if self.kind == "soak":
            if any(
                value is not None
                for value in (
                    self.interruption_stage,
                    self.ready_file,
                    self.interrupt_after_seconds,
                    self.recovery_command,
                )
            ):
                raise ValueError("soak cases cannot contain interruption controls")
        elif (
            self.interruption_stage is None
            or self.recovery_command is None
            or (self.ready_file is None and self.interrupt_after_seconds is None)
        ):
            raise ValueError(
                "forced-interruption cases require a stage, recovery command, and "
                "ready_file or interrupt_after_seconds"
            )
        elif self.repetitions != 1:
            raise ValueError("forced-interruption cases run exactly once")
        elif self.evidence_contract != "none":
            raise ValueError("forced-interruption cases cannot declare a soak evidence contract")
        return self


class AcceptanceSuite(BaseModel):
    """Versioned release scenario declaration."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    suite_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,79}$")
    suite_scope: Literal["primary_machine_full", "independent_platform"] = "primary_machine_full"
    description: str = Field(min_length=1)
    cases: tuple[AcceptanceCase, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_cases_and_required_coverage(self) -> AcceptanceSuite:
        identifiers = [case.case_id for case in self.cases]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("Acceptance scenario IDs must be unique")
        if self.suite_scope == "primary_machine_full":
            stages = {
                case.interruption_stage for case in self.cases if case.kind == "forced_interruption"
            }
            missing_stages = set(INTERRUPTION_STAGES) - stages
            if missing_stages:
                raise ValueError(
                    "Primary-machine suites must declare every interruption stage; "
                    f"missing: {', '.join(sorted(missing_stages))}"
                )
        soak_counts = {
            duration: sum(
                case.repetitions
                for case in self.cases
                if case.kind == "soak" and case.duration_minutes == duration
            )
            for duration in (1, 3, 5, 10, 20)
        }
        if self.suite_scope == "primary_machine_full":
            missing_durations = [
                str(duration) for duration, count in soak_counts.items() if count < 1
            ]
            if missing_durations:
                raise ValueError(
                    "Primary-machine suites must declare soak cases for 1, 3, 5, 10, and "
                    f"20 minutes; missing: {', '.join(missing_durations)}"
                )
            if sum(soak_counts.values()) < 20:
                raise ValueError("Primary-machine suites must declare at least 20 soak runs")
        elif not any(case.kind == "soak" for case in self.cases):
            raise ValueError("Independent-platform suites require a real production soak case")
        if any(
            case.evidence_contract != "production_receipt_v2"
            for case in self.cases
            if case.kind == "soak"
        ):
            raise ValueError(
                "Every versioned soak case must require embedded production_receipt_v2 evidence"
            )
        return self


class ProductionArtifactEvidence(BaseModel):
    """One hash-bound artifact recorded by the production case verifier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    bytes: int = Field(gt=0)


class ProductionCaseReceipt(BaseModel):
    """Self-contained successful prompt-to-audio evidence embedded in a platform report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2]
    evidence_scope: Literal["production_prompt_to_audio_case"]
    generated_at: datetime
    run_id: UUID
    command: tuple[str, ...]
    profile: Literal["lite", "standard"]
    duration_minutes: int = Field(ge=1, le=20)
    actual_duration_ms: int = Field(gt=0)
    quality_passed: Literal[True]
    artifacts: dict[
        Literal["run", "timeline", "audio", "manifest", "quality"],
        ProductionArtifactEvidence,
    ]

    @model_validator(mode="after")
    def _complete_artifacts(self) -> ProductionCaseReceipt:
        required = {"run", "timeline", "audio", "manifest", "quality"}
        if set(self.artifacts) != required:
            raise ValueError("Production receipt must contain all five authoritative artifacts")
        requested_ms = self.duration_minutes * 60_000
        if abs(self.actual_duration_ms - requested_ms) / requested_ms > 0.05:
            raise ValueError("Production receipt exceeds the five-percent duration gate")
        return self


class ProcessEvidence(BaseModel):
    """Bounded diagnostic evidence from one child invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    return_code: int
    duration_seconds: float = Field(ge=0)
    stdout_tail: str
    stderr_tail: str
    receipt_source_path: Path | None = None
    receipt_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    production_receipt: ProductionCaseReceipt | None = None


class AcceptanceCaseResult(BaseModel):
    """Observed result for one soak or forced-interruption scenario."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    kind: AcceptanceKind
    passed: bool
    status: Literal["passed", "failed", "not_run"]
    stage: InterruptionStage | None = None
    attempts: tuple[ProcessEvidence, ...] = ()
    interruption_observed: bool | None = None
    stage_marker: AcceptanceStageMarker | None = None
    recovery: ProcessEvidence | None = None
    detail: str


class AcceptanceReport(BaseModel):
    """Local evidence report that leaves every unrun platform visibly unrun."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    suite_id: str = "legacy-unversioned"
    suite_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    suite_complete: bool = True
    selected_case_ids: tuple[str, ...] = ()
    product_version: str = "legacy-unknown"
    # Optional so historical schema-v1 reports remain readable.  Current
    # independent-platform aggregation rejects an absent digest.
    product_wheel_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    target: ReleaseTarget
    backend: BackendSelection
    platform_matrix: tuple[PlatformMatrixEntry, ...]
    offline: OfflineAuditReport
    migrations: MigrationReport
    cases: tuple[AcceptanceCaseResult, ...]
    current_machine_passed: bool
    local_v1_release_ready: bool

    @model_validator(mode="after")
    def _completed_claim_requires_full_suite(self) -> AcceptanceReport:
        if self.current_machine_passed and not self.suite_complete:
            raise ValueError("current_machine_passed cannot be true for a filtered suite")
        if (
            self.current_machine_passed
            and self.suite_id != "legacy-unversioned"
            and self.suite_sha256 is None
        ):
            raise ValueError("A versioned passing report must bind the exact suite SHA-256")
        if self.selected_case_ids and self.selected_case_ids != tuple(
            result.case_id for result in self.cases
        ):
            raise ValueError("selected_case_ids must match the ordered case results")
        expected_release_ready = all(entry.status == "passed" for entry in self.platform_matrix)
        if self.local_v1_release_ready != expected_release_ready:
            raise ValueError("local_v1_release_ready must be derived from the platform matrix")
        return self


def audit_offline_post_acquisition(
    *,
    config_directory: Path,
    models_root: Path,
    required_pack_ids: tuple[str, ...],
    network_disabled_during_test: bool,
    network_isolation_note: str | None = None,
    network_isolation_evidence: dict[str, Any] | None = None,
    model_registry_path: Path | None = None,
) -> OfflineAuditReport:
    """Verify offline policy and real current-machine READY evidence for required packs."""

    checks: list[AuditCheck] = []
    try:
        settings = load_settings(config_directory, environment={})
    except ConfigError as error:
        checks.append(AuditCheck(check="settings", passed=False, detail=str(error)))
    else:
        checks.append(
            AuditCheck(
                check="remote_fallback_disabled",
                passed=not settings.hardware.allow_remote_fallback,
                detail=(
                    "Remote fallback is disabled."
                    if not settings.hardware.allow_remote_fallback
                    else "Remote fallback is enabled and violates Local V1 offline policy."
                ),
            )
        )

    if network_disabled_during_test and network_isolation_evidence is not None:
        raise ValueError("Operator attestation and enforced process isolation are exclusive.")
    attested_network_isolation = network_disabled_during_test and bool(
        network_isolation_note and network_isolation_note.strip()
    )
    sandbox_network_isolation = valid_macos_network_isolation_evidence(network_isolation_evidence)
    verified_network_isolation = attested_network_isolation or sandbox_network_isolation
    checks.append(
        AuditCheck(
            check="os_network_isolation",
            passed=verified_network_isolation,
            detail=(
                f"Operator attestation: {network_isolation_note.strip()}"
                if attested_network_isolation and network_isolation_note is not None
                else (
                    "macOS sandbox-exec denied pre-reachable external TCP controls while "
                    "permitting loopback."
                    if sandbox_network_isolation
                    else (
                        "--network-disabled requires a non-empty isolation note describing the "
                        "external OS control."
                        if network_disabled_during_test
                        else "Networking was not proven disabled; offline execution remains "
                        "unverified."
                    )
                )
            ),
        )
    )
    if not required_pack_ids:
        checks.append(
            AuditCheck(
                check="required_model_packs",
                passed=False,
                detail="No required model packs were declared for the offline path.",
            )
        )
    else:
        registry_path = model_registry_path or config_directory / "model_packs.yaml"
        try:
            registry = load_model_pack_registry(registry_path, models_root=models_root)
            for pack_id in required_pack_ids:
                status = registry.inspect(pack_id, verify_digests=True)
                checks.append(
                    AuditCheck(
                        check=f"model_pack:{pack_id}",
                        passed=status.state is ModelPackState.READY,
                        detail=(
                            "Pinned files, runtime probe, and real offline smoke "
                            "evidence are READY."
                            if status.state is ModelPackState.READY
                            else (
                                f"Current-machine model state is {status.state.value}; "
                                "READY required."
                            )
                        ),
                    )
                )
        except ModelPackError as error:
            checks.append(AuditCheck(check="model_registry", passed=False, detail=str(error)))
    return OfflineAuditReport(
        passed=bool(checks) and all(check.passed for check in checks),
        network_disabled_during_test=verified_network_isolation,
        network_isolation_evidence_kind=(
            "macos_sandbox_exec"
            if sandbox_network_isolation
            else "operator_attestation"
            if attested_network_isolation
            else "none"
        ),
        network_isolation_note=(
            "macOS sandbox-exec denied every pre-reachable fixed external TCP control "
            "while permitting a 127.0.0.1 round trip."
            if sandbox_network_isolation
            else network_isolation_note.strip()
            if attested_network_isolation and network_isolation_note is not None
            else None
        ),
        process_network_isolation_enforced=sandbox_network_isolation,
        network_isolation_evidence=(
            network_isolation_evidence if sandbox_network_isolation else None
        ),
        required_pack_ids=required_pack_ids,
        checks=tuple(checks),
    )


def load_acceptance_suite(path: Path) -> AcceptanceSuite:
    """Load a versioned suite and validate the complete release coverage declaration."""

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError("scenario root must be a versioned JSON object")
        return AcceptanceSuite.model_validate(document)
    except (OSError, json.JSONDecodeError, ValidationError, ValueError) as error:
        raise ValueError(f"Could not load acceptance scenarios from {path}: {error}") from error


def load_acceptance_cases(path: Path) -> tuple[AcceptanceCase, ...]:
    """Compatibility helper returning cases from a validated versioned suite."""

    return load_acceptance_suite(path).cases


def acceptance_suite_sha256(suite: AcceptanceSuite) -> str:
    """Return the canonical identity of every command and acceptance control."""

    payload = json.dumps(
        suite.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def run_acceptance(
    *,
    cases: tuple[AcceptanceCase, ...],
    offline: OfflineAuditReport,
    migrations: MigrationReport,
    hardware: HardwareSnapshot | None = None,
    generated_at: datetime | None = None,
    suite_id: str = "legacy-unversioned",
    suite_sha256: str | None = None,
    declared_case_ids: tuple[str, ...] | None = None,
    product_wheel_sha256: str | None = None,
) -> AcceptanceReport:
    """Execute local scenarios and assemble an honest five-target report."""

    snapshot = hardware or inspect_release_hardware()
    target = detect_release_target(
        operating_system=snapshot.operating_system,
        architecture=snapshot.architecture,
    )
    backend = select_friendly_backend(snapshot)
    results = tuple(_safe_run_case(case) for case in cases)
    selected_case_ids = tuple(case.case_id for case in cases)
    suite_complete = declared_case_ids is None or selected_case_ids == declared_case_ids
    current_results = tuple(
        result
        for case, result in zip(cases, results, strict=True)
        if case.disposition != "external"
    )
    current_passed = (
        target.supported
        and bool(current_results)
        and all(result.passed for result in current_results)
        and offline.passed
        and migrations.passed
        and suite_complete
    )
    evidence = (
        f"{len(current_results)} local scenarios passed with offline and migration audits."
        if current_passed
        else (
            "Filtered case execution is diagnostic only and cannot pass the complete suite."
            if not suite_complete
            else "Current-machine acceptance is incomplete or failed; inspect report checks."
        )
    )
    current_status: Literal["passed", "failed", "not_run"] = (
        "passed" if current_passed else "failed" if current_results else "not_run"
    )
    matrix = build_platform_matrix(
        target,
        current_status=current_status,
        current_evidence=evidence,
    )
    # A local report cannot prove the four other target systems. Aggregation of
    # their own reports belongs in release CI, so this remains false here.
    release_ready = all(entry.status == "passed" for entry in matrix)
    return AcceptanceReport(
        suite_id=suite_id,
        suite_sha256=suite_sha256,
        suite_complete=suite_complete,
        selected_case_ids=selected_case_ids,
        product_version=__version__,
        product_wheel_sha256=product_wheel_sha256,
        generated_at=generated_at or datetime.now(UTC),
        target=target,
        backend=backend,
        platform_matrix=matrix,
        offline=offline,
        migrations=migrations,
        cases=results,
        current_machine_passed=current_passed,
        local_v1_release_ready=release_ready,
    )


def write_acceptance_report(path: Path, report: AcceptanceReport) -> None:
    """Atomically persist a report after all scenario evidence is complete."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(report.model_dump_json(indent=2).encode("utf-8") + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _run_case(case: AcceptanceCase) -> AcceptanceCaseResult:
    if case.disposition != "run":
        return AcceptanceCaseResult(
            case_id=case.case_id,
            kind=case.kind,
            passed=False,
            status="not_run",
            stage=case.interruption_stage,
            detail=(
                f"{case.disposition.title()} evidence: {case.evidence_note}. "
                "No success is inferred by this report."
            ),
        )
    if case.kind == "soak":
        attempts: list[ProcessEvidence] = []
        for _index in range(case.repetitions):
            evidence = _run_command(case.command, timeout_seconds=case.timeout_seconds)
            if evidence.return_code == 0 and case.evidence_contract == "production_receipt_v2":
                evidence = _attach_production_receipt(
                    evidence, expected_minutes=case.duration_minutes
                )
            attempts.append(evidence)
            if evidence.return_code != 0:
                return AcceptanceCaseResult(
                    case_id=case.case_id,
                    kind=case.kind,
                    passed=False,
                    status="failed",
                    attempts=tuple(attempts),
                    detail=f"Soak attempt {len(attempts)} exited nonzero.",
                )
        return AcceptanceCaseResult(
            case_id=case.case_id,
            kind=case.kind,
            passed=True,
            status="passed",
            attempts=tuple(attempts),
            detail=f"All {len(attempts)} soak attempts completed successfully.",
        )
    return _run_forced_interruption(case)


def _safe_run_case(case: AcceptanceCase) -> AcceptanceCaseResult:
    try:
        return _run_case(case)
    except Exception as error:
        return AcceptanceCaseResult(
            case_id=case.case_id,
            kind=case.kind,
            passed=False,
            status="failed",
            stage=case.interruption_stage,
            detail=f"Acceptance harness error: {type(error).__name__}: {error}"[:2_000],
        )


def _run_forced_interruption(case: AcceptanceCase) -> AcceptanceCaseResult:
    assert case.interruption_stage is not None
    if case.ready_file is not None:
        case.ready_file.resolve().unlink(missing_ok=True)
    command = _resolve_command(case.command)
    started = time.monotonic()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_interruption_environment(case),
    )
    interrupted = False
    try:
        deadline = started + case.timeout_seconds
        if case.ready_file is not None:
            marker = case.ready_file.resolve()
            while not marker.is_file() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
        else:
            assert case.interrupt_after_seconds is not None
            while process.poll() is None and time.monotonic() < min(
                deadline, started + case.interrupt_after_seconds
            ):
                time.sleep(0.02)
        if process.poll() is None and time.monotonic() < deadline:
            process.terminate()
            interrupted = True
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    initial = ProcessEvidence(
        return_code=process.returncode,
        duration_seconds=round(time.monotonic() - started, 3),
        stdout_tail=stdout[-4_000:],
        stderr_tail=stderr[-4_000:],
    )
    marker_evidence, marker_error = _load_stage_marker(case, expected_pid=process.pid)
    assert case.recovery_command is not None
    recovery = (
        _run_command(case.recovery_command, timeout_seconds=case.timeout_seconds)
        if marker_evidence is not None
        else None
    )
    passed = (
        interrupted
        and process.returncode != 0
        and marker_evidence is not None
        and recovery is not None
        and recovery.return_code == 0
    )
    return AcceptanceCaseResult(
        case_id=case.case_id,
        kind=case.kind,
        passed=passed,
        status="passed" if passed else "failed",
        stage=case.interruption_stage,
        attempts=(initial,),
        interruption_observed=interrupted,
        stage_marker=marker_evidence,
        recovery=recovery,
        detail=(
            "The production stage marker matched the child PID and durable run stage; "
            "the child was forcibly stopped and its recovery verifier completed."
            if passed
            else (
                "The process did not demonstrate an exact marked-stage interruption and "
                f"successful verified recovery. {marker_error or ''}"
            ).strip()
        ),
    )


def _run_command(command: tuple[str, ...], *, timeout_seconds: float) -> ProcessEvidence:
    resolved = _resolve_command(command)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            resolved,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=_offline_environment(),
        )
        return_code = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as error:
        return_code = 124
        stdout = _decode_timeout_output(error.stdout)
        stderr = _decode_timeout_output(error.stderr) + "\nAcceptance command timed out."
    return ProcessEvidence(
        return_code=return_code,
        duration_seconds=round(time.monotonic() - started, 3),
        stdout_tail=stdout[-4_000:],
        stderr_tail=stderr[-4_000:],
    )


def _attach_production_receipt(
    evidence: ProcessEvidence, *, expected_minutes: int | None
) -> ProcessEvidence:
    """Embed and hash the exact receipt emitted by the production case child."""

    try:
        lines = [line.strip() for line in evidence.stdout_tail.splitlines() if line.strip()]
        if not lines:
            raise ValueError("production case did not print a receipt path")
        candidate = Path(lines[-1]).expanduser()
        if candidate.is_symlink():
            raise ValueError("production receipt path may not be a symbolic link")
        receipt_path = candidate.resolve(strict=True)
        if not receipt_path.is_file():
            raise ValueError("production receipt path is not a regular file")
        payload = receipt_path.read_bytes()
        receipt = ProductionCaseReceipt.model_validate_json(payload)
        if expected_minutes is None or receipt.duration_minutes != expected_minutes:
            raise ValueError(
                f"production receipt duration {receipt.duration_minutes} does not match "
                f"declared case duration {expected_minutes}"
            )
        return evidence.model_copy(
            update={
                "receipt_source_path": receipt_path,
                "receipt_sha256": hashlib.sha256(payload).hexdigest(),
                "production_receipt": receipt,
            }
        )
    except (OSError, ValidationError, ValueError) as error:
        return evidence.model_copy(
            update={
                "return_code": 65,
                "stderr_tail": (
                    evidence.stderr_tail
                    + f"\nProduction receipt evidence failed validation: {type(error).__name__}: "
                    f"{error}"
                )[-4_000:],
            }
        )


def _resolve_command(command: tuple[str, ...]) -> tuple[str, ...]:
    executable: str | None
    if command[0] == "{python}":
        # Preserve a virtual-environment launcher rather than resolving its
        # symlink to a base interpreter that cannot import the tested install.
        executable = str(Path(sys.executable).absolute())
    else:
        executable = shutil.which(command[0])
    if executable is None:
        candidate = Path(command[0]).expanduser().resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            executable = str(candidate)
    if executable is None:
        raise ValueError(f"Acceptance executable was not found or is not runnable: {command[0]}")
    return (executable, *command[1:])


def _offline_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "PYTHONNOUSERSITE": "1",
            "LOCAL_V1_ACCEPTANCE_OFFLINE": "1",
        }
    )
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "PIP_USER",
        "PIP_TARGET",
        "PIP_PREFIX",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONUSERBASE",
        "VIRTUAL_ENV",
    ):
        environment.pop(name, None)
    return environment


def _interruption_environment(case: AcceptanceCase) -> dict[str, str]:
    environment = _offline_environment()
    if case.ready_file is not None and case.interruption_stage is not None:
        environment[STAGE_READY_FILE_ENV] = str(case.ready_file.resolve())
        environment[PAUSE_STAGE_ENV] = case.interruption_stage
    return environment


def _load_stage_marker(
    case: AcceptanceCase,
    *,
    expected_pid: int,
) -> tuple[AcceptanceStageMarker | None, str | None]:
    if case.ready_file is None:
        return None, "No structured ready-file marker was configured."
    try:
        marker = AcceptanceStageMarker.model_validate_json(
            case.ready_file.resolve().read_text(encoding="utf-8")
        )
    except (OSError, ValidationError) as error:
        return None, f"Stage marker was missing or invalid: {error}"
    if marker.process_id != expected_pid:
        return None, (
            f"Stage marker PID {marker.process_id} did not match child PID {expected_pid}."
        )
    if marker.stage != case.interruption_stage:
        return None, (
            f"Stage marker recorded {marker.stage!r}, expected {case.interruption_stage!r}."
        )
    if marker.durable_run_status != "running":
        return None, (
            f"Stage marker durable status was {marker.durable_run_status!r}, expected 'running'."
        )
    expected_durable_stage = {
        "synthesis": "synthesizing",
        "assembly": "assembling",
        "qc": "quality_check",
    }.get(case.interruption_stage, case.interruption_stage)
    if marker.durable_run_stage != expected_durable_stage:
        return None, (
            f"Stage marker durable stage was {marker.durable_run_stage!r}, expected "
            f"{expected_durable_stage!r}."
        )
    return marker, None


def _decode_timeout_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
