"""Strict aggregation of independently produced Local V1 platform evidence."""

from __future__ import annotations

import hashlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from whoopy import __version__
from whoopy.release.acceptance import (
    AcceptanceReport,
    AcceptanceSuite,
    acceptance_suite_sha256,
)
from whoopy.release.network_isolation import valid_macos_network_isolation_evidence
from whoopy.release.platforms import PlatformMatrixEntry, detect_release_target


class ImportedPlatformEvidence(BaseModel):
    """Provenance for one independently generated acceptance report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str
    source_path: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    accepted: bool
    detail: str


class CrossPlatformReleaseEvidence(BaseModel):
    """Five-target evidence matrix assembled without rerunning foreign targets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    suite_id: str
    # Optional only to keep historical schema-v1 aggregates readable.  A
    # current aggregate is always bound to one exact shared product wheel.
    product_wheel_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    generated_at: datetime
    platform_matrix: tuple[PlatformMatrixEntry, ...]
    imported_reports: tuple[ImportedPlatformEvidence, ...]
    local_v1_release_ready: bool


def aggregate_platform_reports(
    *,
    suite: AcceptanceSuite,
    report_paths: tuple[Path, ...],
    generated_at: datetime | None = None,
) -> CrossPlatformReleaseEvidence:
    """Validate and combine reports; absent targets remain explicitly ``not_run``."""

    if suite.suite_scope != "independent_platform":
        raise ValueError(
            "Cross-platform aggregation requires an independent_platform acceptance suite."
        )

    expected_targets = (
        detect_release_target(operating_system="windows", architecture="x86_64"),
        detect_release_target(operating_system="darwin", architecture="arm64"),
        detect_release_target(operating_system="darwin", architecture="x86_64"),
        detect_release_target(operating_system="linux", architecture="x86_64"),
        detect_release_target(operating_system="linux", architecture="arm64"),
    )
    known_targets = {target.target_id: target for target in expected_targets}
    imported: list[ImportedPlatformEvidence] = []
    statuses: dict[str, tuple[Literal["passed", "failed"], str]] = {}
    product_wheel_sha256: str | None = None

    for path in report_paths:
        resolved = path.expanduser().resolve()
        try:
            payload = resolved.read_bytes()
            report = AcceptanceReport.model_validate_json(payload)
        except (OSError, ValidationError, ValueError) as error:
            raise ValueError(f"Could not import acceptance report {resolved}: {error}") from error
        target_id = report.target.target_id
        if target_id not in known_targets:
            raise ValueError(f"Report {resolved} claims unsupported target {target_id!r}.")
        if target_id in statuses:
            raise ValueError(f"More than one report was supplied for target {target_id!r}.")
        _validate_report_contract(report=report, suite=suite, path=resolved)
        assert report.product_wheel_sha256 is not None
        if product_wheel_sha256 is None:
            product_wheel_sha256 = report.product_wheel_sha256
        elif report.product_wheel_sha256 != product_wheel_sha256:
            raise ValueError("Independent platform reports do not share one product wheel SHA-256.")
        passed = report.current_machine_passed
        detail = (
            f"Independent {suite.suite_id} report passed all declared cases, "
            "offline, and migrations."
            if passed
            else "The independent report is complete but one or more acceptance gates failed."
        )
        statuses[target_id] = ("passed" if passed else "failed", detail)
        imported.append(
            ImportedPlatformEvidence(
                target_id=target_id,
                source_path=resolved,
                sha256=hashlib.sha256(payload).hexdigest(),
                generated_at=report.generated_at,
                accepted=passed,
                detail=detail,
            )
        )

    matrix = tuple(
        PlatformMatrixEntry(
            target=target,
            current_machine=False,
            status=statuses.get(target.target_id, ("not_run", ""))[0],
            evidence=(statuses[target.target_id][1] if target.target_id in statuses else None),
        )
        for target in expected_targets
    )
    return CrossPlatformReleaseEvidence(
        suite_id=suite.suite_id,
        product_wheel_sha256=product_wheel_sha256,
        generated_at=generated_at or datetime.now(UTC),
        platform_matrix=matrix,
        imported_reports=tuple(imported),
        local_v1_release_ready=all(entry.status == "passed" for entry in matrix),
    )


def _validate_report_contract(
    *, report: AcceptanceReport, suite: AcceptanceSuite, path: Path
) -> None:
    if report.suite_id != suite.suite_id:
        raise ValueError(
            f"Report {path} used suite {report.suite_id!r}; expected {suite.suite_id!r}."
        )
    expected_suite_digest = acceptance_suite_sha256(suite)
    if report.suite_sha256 != expected_suite_digest:
        raise ValueError(
            f"Report {path} is not bound to the exact suite content "
            f"({report.suite_sha256!r} != {expected_suite_digest!r})."
        )
    if report.product_version != __version__:
        raise ValueError(
            f"Report {path} used product {report.product_version!r}; expected {__version__!r}."
        )
    if report.product_wheel_sha256 is None:
        raise ValueError(
            f"Report {path} is legacy/unbound and does not identify the shared product wheel."
        )
    if not report.suite_complete:
        raise ValueError(f"Report {path} contains only a filtered diagnostic case selection.")
    if not report.target.supported:
        raise ValueError(f"Report {path} was produced for an unsupported target.")
    matrix_current = [entry for entry in report.platform_matrix if entry.current_machine]
    if len(matrix_current) != 1 or matrix_current[0].target.target_id != report.target.target_id:
        raise ValueError(f"Report {path} has an inconsistent current-machine matrix entry.")
    if any(
        entry.status != "not_run" for entry in report.platform_matrix if not entry.current_machine
    ):
        raise ValueError(f"Report {path} improperly grants evidence to another machine.")

    expected = {case.case_id: case for case in suite.cases}
    actual = {result.case_id: result for result in report.cases}
    if len(actual) != len(report.cases) or set(actual) != set(expected):
        raise ValueError(f"Report {path} does not contain exactly the complete declared suite.")
    for case_id, case in expected.items():
        result = actual[case_id]
        if result.kind != case.kind or result.stage != case.interruption_stage:
            raise ValueError(f"Report {path} has mismatched metadata for case {case_id!r}.")
        if (
            report.current_machine_passed
            and case.kind == "soak"
            and len(result.attempts) != case.repetitions
        ):
            raise ValueError(
                f"Report {path} has {len(result.attempts)} attempts for {case_id!r}; "
                f"expected {case.repetitions}."
            )
        if (
            report.current_machine_passed
            and case.kind == "soak"
            and any(attempt.return_code != 0 for attempt in result.attempts)
        ):
            raise ValueError(f"Report {path} contains a failed attempt for {case_id!r}.")
        if (
            report.current_machine_passed
            and case.kind == "soak"
            and case.evidence_contract == "production_receipt_v2"
            and any(
                attempt.production_receipt is None
                or attempt.receipt_sha256 is None
                or attempt.production_receipt.duration_minutes != case.duration_minutes
                for attempt in result.attempts
            )
        ):
            raise ValueError(
                f"Report {path} lacks embedded production receipts for soak case {case_id!r}."
            )
        if (
            report.current_machine_passed
            and case.kind == "forced_interruption"
            and (
                len(result.attempts) != 1
                or result.interruption_observed is not True
                or result.stage_marker is None
                or result.stage_marker.stage != case.interruption_stage
                or result.recovery is None
                or result.recovery.return_code != 0
            )
        ):
            raise ValueError(
                f"Report {path} lacks complete forced-interruption evidence for {case_id!r}."
            )
    operator_isolation = (
        report.offline.network_isolation_evidence_kind == "operator_attestation"
        and not report.offline.process_network_isolation_enforced
    )
    macos_isolation = (
        report.target.operating_system == "darwin"
        and report.offline.network_isolation_evidence_kind == "macos_sandbox_exec"
        and report.offline.process_network_isolation_enforced
        and valid_macos_network_isolation_evidence(report.offline.network_isolation_evidence)
    )
    if report.current_machine_passed and (
        not report.offline.passed
        or not report.offline.network_disabled_during_test
        or not (operator_isolation or macos_isolation)
        or not report.offline.network_isolation_note
        or not report.migrations.passed
        or any(result.status != "passed" or not result.passed for result in report.cases)
        or matrix_current[0].status != "passed"
    ):
        raise ValueError(f"Report {path} claims a pass without complete supporting evidence.")


def write_cross_platform_evidence(path: Path, evidence: CrossPlatformReleaseEvidence) -> None:
    """Atomically persist the aggregate and its source-report digests."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(evidence.model_dump_json(indent=2).encode("utf-8") + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
