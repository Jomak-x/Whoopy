"""Verify all finished formats for the required 20-run Local V1 soak corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from whoopy.audio import load_export_manifest

REQUIRED_DURATIONS = (1, 3, 5, 10, 20)
REQUIRED_KINDS = {"wav", "flac", "aac", "opus"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify(receipts_directory: Path, runs_directory: Path) -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    errors: list[str] = []
    durations: Counter[int] = Counter()
    receipt_paths = sorted(receipts_directory.glob("*.json"))
    for receipt_path in receipt_paths:
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            run_id = str(receipt["run_id"])
            minutes = int(receipt["duration_minutes"])
            if receipt.get("quality_passed") is not True:
                raise ValueError("production receipt did not pass quality")
            run_directory = runs_directory / run_id
            narration = run_directory / "narration.wav"
            narration_sha256 = _sha256(narration)
            if receipt["artifacts"]["audio"]["sha256"] != narration_sha256:
                raise ValueError("production receipt audio hash does not match narration.wav")
            manifest_path = run_directory / "exports" / "export-manifest.json"
            manifest = load_export_manifest(manifest_path)
            if manifest.source_sha256 != narration_sha256:
                raise ValueError("export manifest source hash does not match narration.wav")
            artifacts = {artifact.kind: artifact for artifact in manifest.artifacts}
            if set(artifacts) != REQUIRED_KINDS or len(manifest.artifacts) != 4:
                raise ValueError(
                    "export manifest must contain WAV, FLAC, AAC, and Opus exactly once"
                )
            files: dict[str, dict[str, Any]] = {}
            for kind, artifact in artifacts.items():
                path = run_directory / "exports" / artifact.filename
                if not path.is_file() or path.stat().st_size != artifact.byte_count:
                    raise ValueError(f"{kind} export is missing or has the wrong byte count")
                if _sha256(path) != artifact.sha256:
                    raise ValueError(f"{kind} export hash does not match its manifest")
                if artifact.decode_verified is not True:
                    raise ValueError(f"{kind} export lacks successful decode evidence")
                files[kind] = {
                    "filename": artifact.filename,
                    "bytes": artifact.byte_count,
                    "sha256": artifact.sha256,
                    "decode_verified": True,
                }
            durations[minutes] += 1
            cases.append(
                {
                    "run_id": run_id,
                    "duration_minutes": minutes,
                    "receipt_sha256": _sha256(receipt_path),
                    "export_manifest_sha256": _sha256(manifest_path),
                    "files": files,
                    "passed": True,
                }
            )
        except (OSError, KeyError, TypeError, ValueError) as error:
            errors.append(f"{receipt_path}: {error}")
    expected_counts = {duration: 4 for duration in REQUIRED_DURATIONS}
    observed_counts = {duration: durations[duration] for duration in REQUIRED_DURATIONS}
    if observed_counts != expected_counts:
        errors.append(
            f"Required four runs at each duration; observed {observed_counts}, "
            f"expected {expected_counts}."
        )
    if len(receipt_paths) != 20:
        errors.append(f"Expected exactly 20 production receipts; found {len(receipt_paths)}.")
    return {
        "schema_version": 1,
        "evidence_scope": "primary_machine_soak_finished_exports",
        "generated_at": datetime.now(UTC).isoformat(),
        "receipt_directory": str(receipts_directory.resolve()),
        "runs_directory": str(runs_directory.resolve()),
        "duration_counts": observed_counts,
        "cases": cases,
        "errors": errors,
        "passed": not errors and len(cases) == 20,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipts-dir", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    report = verify(args.receipts_dir, args.runs_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(args.output.resolve())
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
