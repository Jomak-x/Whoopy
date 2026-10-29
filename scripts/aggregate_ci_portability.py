"""Aggregate CI portability receipts while keeping them separate from release acceptance."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

EXPECTED_TARGETS = frozenset(
    {
        "windows-x86_64",
        "darwin-arm64",
        "darwin-x86_64",
        "linux-x86_64",
        "linux-arm64",
    }
)


class PortabilityReceipt(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: Literal[1]
    evidence_scope: Literal["ci_portability_not_release_acceptance"]
    expected_target: str
    detected_target: str | None = None
    wheel_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    offline_process_environment: bool
    os_network_isolation_proven: Literal[False]
    passed: bool


def aggregate(receipt_root: Path) -> dict[str, object]:
    receipts: dict[str, PortabilityReceipt] = {}
    errors: list[str] = []
    for path in sorted(receipt_root.rglob("*.json")):
        try:
            receipt = PortabilityReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError) as error:
            errors.append(f"{path}: {error}")
            continue
        if receipt.expected_target in receipts:
            errors.append(f"Duplicate receipt for {receipt.expected_target}.")
            continue
        receipts[receipt.expected_target] = receipt
    unknown = sorted(set(receipts) - EXPECTED_TARGETS)
    missing = sorted(EXPECTED_TARGETS - set(receipts))
    if unknown:
        errors.append(f"Unknown targets: {', '.join(unknown)}")
    if missing:
        errors.append(f"Missing targets: {', '.join(missing)}")
    for target, receipt in receipts.items():
        if not receipt.passed or receipt.detected_target != target:
            errors.append(f"Portability smoke did not pass for {target}.")
    return {
        "schema_version": 1,
        "evidence_scope": "ci_portability_summary_not_release_acceptance",
        "generated_at": datetime.now(UTC).isoformat(),
        "targets": {
            target: receipts[target].model_dump(mode="json")
            for target in sorted(receipts)
            if target in EXPECTED_TARGETS
        },
        "missing_targets": missing,
        "errors": errors,
        "passed": not errors,
        "local_v1_release_ready": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    summary = aggregate(args.receipts)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(args.output.resolve())
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
