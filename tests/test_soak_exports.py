from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.verify_soak_exports import main, verify
from whoopy.audio.exports import ExportArtifact, ExportKind, ExportManifest, ExportMetadata


def _write_exports(run_directory: Path, content: bytes) -> str:
    run_directory.mkdir(parents=True, exist_ok=True)
    (run_directory / "narration.wav").write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    exports_directory = run_directory / "exports"
    exports_directory.mkdir(exist_ok=True)
    artifacts = []
    kinds: tuple[ExportKind, ...] = ("wav", "flac", "aac", "opus")
    for kind in kinds:
        filename = f"finished.{kind}"
        (exports_directory / filename).write_bytes(content)
        artifacts.append(
            ExportArtifact(
                kind=kind,
                filename=filename,
                codec=f"fixture-{kind}",
                byte_count=len(content),
                sha256=digest,
                duration_ms=60_000,
                sample_rate=24_000,
                channels=1,
                embedded_tags={},
            )
        )
    manifest = ExportManifest(
        generated_at=datetime.now(UTC),
        source_filename="narration.wav",
        source_byte_count=len(content),
        source_sha256=digest,
        source_duration_ms=60_000,
        metadata=ExportMetadata(
            title="Synthetic soak case", model="fixture", created_at=datetime.now(UTC)
        ),
        ffmpeg_executable="fixture-ffmpeg",
        ffmpeg_version="fixture",
        artifacts=tuple(artifacts),
    )
    (exports_directory / "export-manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    return digest


@pytest.fixture
def soak_corpus(tmp_path: Path) -> tuple[Path, Path]:
    receipts_directory = tmp_path / "receipts"
    receipts_directory.mkdir()
    runs_directory = tmp_path / "runs"
    for minutes in (1, 3, 5, 10, 20):
        for index in range(4):
            run_id = f"{minutes}-{index}"
            digest = _write_exports(runs_directory / run_id, f"original {run_id}".encode())
            receipt = {
                "run_id": run_id,
                "duration_minutes": minutes,
                "quality_passed": True,
                "artifacts": {"audio": {"sha256": digest}},
            }
            (receipts_directory / f"{run_id}.json").write_text(
                json.dumps(receipt), encoding="utf-8"
            )
    return receipts_directory, runs_directory


def test_soak_counts_exports_bound_to_qualified_audio(soak_corpus: tuple[Path, Path]) -> None:
    report = verify(*soak_corpus)

    assert report["passed"] is True
    assert report["errors"] == []
    assert len(report["cases"]) == 20
    assert report["duration_counts"] == {1: 4, 3: 4, 5: 4, 10: 4, 20: 4}


@pytest.mark.parametrize("change", ["replacement_audio", "missing_hash", "null_hash"])
def test_soak_rejects_unqualified_audio_without_rewriting_receipts(
    soak_corpus: tuple[Path, Path], change: str
) -> None:
    receipts_directory, runs_directory = soak_corpus
    receipt_path = receipts_directory / "1-0.json"
    if change == "replacement_audio":
        _write_exports(runs_directory / "1-0", b"replacement narration and matching exports")
    else:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if change == "missing_hash":
            del receipt["artifacts"]["audio"]["sha256"]
        else:
            receipt["artifacts"]["audio"]["sha256"] = None
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    original_receipts = {path.name: path.read_bytes() for path in receipts_directory.glob("*.json")}

    report = verify(receipts_directory, runs_directory)

    assert report["passed"] is False
    assert report["duration_counts"][1] == 3
    assert len(report["cases"]) == 19
    assert all(case["run_id"] != "1-0" for case in report["cases"])
    assert any(str(receipt_path) in error for error in report["errors"])
    assert {path.name: path.read_bytes() for path in receipts_directory.glob("*.json")} == (
        original_receipts
    )


def test_soak_reports_missing_and_damaged_manifests_and_continues(
    soak_corpus: tuple[Path, Path], tmp_path: Path
) -> None:
    receipts_directory, runs_directory = soak_corpus
    missing_manifest = runs_directory / "1-0" / "exports" / "export-manifest.json"
    damaged_manifest = runs_directory / "3-0" / "exports" / "export-manifest.json"
    missing_manifest.unlink()
    damaged_manifest.write_text("not valid json", encoding="utf-8")
    original_receipts = {path.name: path.read_bytes() for path in receipts_directory.glob("*.json")}
    output = tmp_path / "soak-report.json"
    output.write_text(json.dumps({"passed": True, "cases": ["stale"]}), encoding="utf-8")

    exit_code = main(
        [
            "--receipts-dir",
            str(receipts_directory),
            "--runs-dir",
            str(runs_directory),
            "--output",
            str(output),
        ]
    )

    report = json.loads(output.read_text(encoding="utf-8"))
    assert exit_code == 1
    assert report["passed"] is False
    assert len(report["cases"]) == 18
    assert report["duration_counts"] == {"1": 3, "3": 3, "5": 4, "10": 4, "20": 4}
    assert {case["run_id"] for case in report["cases"]} == {
        f"{minutes}-{index}"
        for minutes in (1, 3, 5, 10, 20)
        for index in range(4)
        if (minutes, index) not in {(1, 0), (3, 0)}
    }
    assert any(str(missing_manifest) in error for error in report["errors"])
    assert any(str(damaged_manifest) in error for error in report["errors"])
    assert {path.name: path.read_bytes() for path in receipts_directory.glob("*.json")} == (
        original_receipts
    )
