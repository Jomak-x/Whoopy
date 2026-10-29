"""Read-only migration compatibility inspection for durable Local V1 data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from whoopy.config import ConfigError, load_settings
from whoopy.model_packs import RuntimeEvidence, RuntimeInstallReceipt, SmokeTestEvidence
from whoopy.pipeline import RunRecord
from whoopy.pipeline.cache import SegmentCacheMetadata
from whoopy.timeline import Timeline

MigrationState = Literal["current", "legacy_readable", "incompatible", "not_found"]


class MigrationFinding(BaseModel):
    """Compatibility evidence for one persisted artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Literal["timeline", "run", "cache", "settings", "model_record", "runtime_record"]
    path: Path
    state: MigrationState
    schema_version: int | None = None
    detail: str


class MigrationReport(BaseModel):
    """Complete, non-mutating inventory of data that a Local V1 launch may read."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    findings: tuple[MigrationFinding, ...]
    passed: bool
    current_count: int
    legacy_readable_count: int
    incompatible_count: int


def inspect_migrations(
    *, runs_directory: Path, config_directory: Path, models_root: Path
) -> MigrationReport:
    """Validate old/current artifacts without rewriting or silently dropping any."""

    findings: list[MigrationFinding] = []
    _inspect_settings(config_directory, findings)
    _inspect_runs(runs_directory, findings)
    _inspect_cache(runs_directory, findings)
    _inspect_model_records(models_root, findings)
    _inspect_runtime_records(models_root, findings)
    incompatible = sum(item.state == "incompatible" for item in findings)
    return MigrationReport(
        findings=tuple(findings),
        passed=incompatible == 0,
        current_count=sum(item.state == "current" for item in findings),
        legacy_readable_count=sum(item.state == "legacy_readable" for item in findings),
        incompatible_count=incompatible,
    )


def _inspect_settings(config_directory: Path, findings: list[MigrationFinding]) -> None:
    default_path = config_directory / "default.yaml"
    try:
        load_settings(config_directory, environment={})
    except ConfigError as error:
        findings.append(
            MigrationFinding(
                category="settings",
                path=default_path,
                state="incompatible" if default_path.exists() else "not_found",
                detail=str(error),
            )
        )
    else:
        findings.append(
            MigrationFinding(
                category="settings",
                path=default_path,
                state="current",
                detail="Layered settings validate against the current strict schema.",
            )
        )


def _inspect_runs(runs_directory: Path, findings: list[MigrationFinding]) -> None:
    run_files = sorted(runs_directory.glob("*/run.json")) if runs_directory.is_dir() else []
    timeline_files = (
        sorted(runs_directory.glob("*/timeline.json")) if runs_directory.is_dir() else []
    )
    if not run_files:
        findings.append(
            MigrationFinding(
                category="run",
                path=runs_directory,
                state="not_found",
                detail="No durable run records were found.",
            )
        )
    if not timeline_files:
        findings.append(
            MigrationFinding(
                category="timeline",
                path=runs_directory,
                state="not_found",
                detail="No timeline artifacts were found.",
            )
        )
    for path in run_files:
        _inspect_json_model(
            path,
            RunRecord,
            "run",
            current_version=6,
            required_current_fields=("audience",),
            findings=findings,
        )
    for path in timeline_files:
        _inspect_json_model(path, Timeline, "timeline", current_version=4, findings=findings)


def _inspect_model_records(models_root: Path, findings: list[MigrationFinding]) -> None:
    root = models_root / "managed" / "model-packs"
    records = sorted(root.glob("*/records/runtime.json")) + sorted(
        root.glob("*/records/smoke.json")
    )
    if not records:
        findings.append(
            MigrationFinding(
                category="model_record",
                path=root,
                state="not_found",
                detail="No model runtime or smoke records were found.",
            )
        )
        return
    for path in sorted(records):
        model = RuntimeEvidence if path.name == "runtime.json" else SmokeTestEvidence
        _inspect_json_model(
            path,
            model,
            "model_record",
            current_version=2,
            findings=findings,
        )


def _inspect_cache(runs_directory: Path, findings: list[MigrationFinding]) -> None:
    root = runs_directory / ".cache" / "segments"
    records = sorted(root.rglob("metadata.json")) if root.is_dir() else []
    if not records:
        findings.append(
            MigrationFinding(
                category="cache",
                path=root,
                state="not_found",
                detail="No segment-cache metadata records were found.",
            )
        )
        return
    for path in records:
        _inspect_json_model(
            path,
            SegmentCacheMetadata,
            "cache",
            current_version=1,
            findings=findings,
        )


def _inspect_runtime_records(models_root: Path, findings: list[MigrationFinding]) -> None:
    """Validate managed runtime receipts; never bless old development declarations."""

    records = sorted((models_root / "managed" / "runtimes").glob("*/.whoopy-runtime.json"))
    for path in records:
        _inspect_json_model(
            path,
            RuntimeInstallReceipt,
            "runtime_record",
            current_version=1,
            findings=findings,
        )


def _inspect_json_model(
    path: Path,
    model: (
        type[RunRecord]
        | type[Timeline]
        | type[RuntimeEvidence]
        | type[SmokeTestEvidence]
        | type[RuntimeInstallReceipt]
        | type[SegmentCacheMetadata]
    ),
    category: Literal["timeline", "run", "cache", "model_record", "runtime_record"],
    *,
    current_version: int,
    required_current_fields: tuple[str, ...] = (),
    findings: list[MigrationFinding],
) -> None:
    try:
        raw_text = path.read_text(encoding="utf-8")
        raw_document = json.loads(raw_text)
        parsed = model.model_validate_json(raw_text)
    except (OSError, ValidationError, ValueError) as error:
        findings.append(
            MigrationFinding(
                category=category,
                path=path,
                state="incompatible",
                detail=f"Current reader rejected this artifact: {error}",
            )
        )
        return
    version = int(parsed.schema_version)
    state: MigrationState
    missing_current_fields = (
        [field for field in required_current_fields if field not in raw_document]
        if isinstance(raw_document, dict)
        else list(required_current_fields)
    )
    if version == current_version and missing_current_fields:
        state = "legacy_readable"
    elif version == current_version:
        state = "current"
    elif version < current_version:
        state = "legacy_readable"
    else:
        state = "incompatible"
    findings.append(
        MigrationFinding(
            category=category,
            path=path,
            state=state,
            schema_version=version,
            detail=(
                f"Schema v{version} validates with the current reader."
                if state == "current"
                else (
                    (
                        f"Schema v{version} remains readable but lacks current field(s): "
                        f"{', '.join(missing_current_fields)}."
                        if missing_current_fields
                        else (
                            f"Legacy schema v{version} remains readable; "
                            f"current is v{current_version}."
                        )
                    )
                    if state == "legacy_readable"
                    else (
                        f"Future schema v{version} cannot be accepted as current "
                        f"v{current_version}."
                    )
                )
            ),
        )
    )
