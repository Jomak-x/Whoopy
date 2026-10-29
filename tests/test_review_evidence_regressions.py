from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from zipfile import ZipFile

import pytest

import whoopy
from scripts import benchmark_writing_methods, local_v1_acceptance, meditation_corpus_report
from whoopy.evaluation.models import BakeoffCaseResult, BakeoffReport, EvaluationCase, EvaluationSet
from whoopy.ports import AdapterMetadata


def _product_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    package = tmp_path / "whoopy"
    package.mkdir()
    (package / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (package / "locations.py").write_text(
        "from pathlib import Path\n"
        "def default_config_directory(): return Path('config')\n"
        "def runtime_worker_script(name): return Path('scripts') / name\n"
    )
    (package / "resources").mkdir()
    (package / "resources" / "pacing.yaml").write_text("pace: calm\n")
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "default.yaml").write_text("profile: standard\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "moss_tts_worker.py").write_text("print('worker')\n")
    wheel = tmp_path / "whoopy-0.1.0-py3-none-any.whl"
    with ZipFile(wheel, "w") as archive:
        for path in package.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(tmp_path).as_posix())
        for relative in ("config/default.yaml", "scripts/moss_tts_worker.py"):
            archive.write(tmp_path / relative, f"whoopy-0.1.0.data/data/share/whoopy/{relative}")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(whoopy, "__file__", str(package / "__init__.py"))
    return wheel


def test_acceptance_attests_matching_executed_package_and_resources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wheel = _product_fixture(tmp_path, monkeypatch)

    assert (
        local_v1_acceptance._verified_product_wheel_sha256(wheel)
        == hashlib.sha256(wheel.read_bytes()).hexdigest()
    )


@pytest.mark.parametrize(
    "changed_path",
    [
        "whoopy/locations.py",
        "whoopy/resources/pacing.yaml",
        "config/default.yaml",
        "scripts/moss_tts_worker.py",
        "whoopy/extra.py",
    ],
)
def test_acceptance_rejects_same_version_wheel_with_different_executed_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_path: str
) -> None:
    scenario = Path("config/acceptance/local-v1-platform-v1.json").resolve()
    wheel = _product_fixture(tmp_path, monkeypatch)
    with (tmp_path / changed_path).open("a") as target:
        target.write("\n# Different same-version product bytes\n")

    with pytest.raises(SystemExit, match="does not match the executed product"):
        local_v1_acceptance.main(
            [
                "--scenario",
                str(scenario),
                "--output",
                str(tmp_path / "receipt.json"),
                "--product-wheel",
                str(wheel),
            ]
        )

    assert not (tmp_path / "receipt.json").exists()


@pytest.mark.parametrize("seed,profile", [(42, "standard"), (43, "standard"), (42, "lite")])
def test_writing_benchmark_requires_fresh_output_without_reusing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int, profile: str
) -> None:
    output = tmp_path / "benchmark"
    result = output / "grounding-3m" / "plan_first" / "result.json"
    result.parent.mkdir(parents=True)
    saved = json.dumps({"status": "generated_pending_content_review", "seed": 42})
    result.write_text(saved)
    monkeypatch.setattr(
        sys,
        "argv",
        ["benchmark", "--output", str(output), "--seed", str(seed), "--profile", profile],
    )

    with pytest.raises(SystemExit, match="use a new output directory"):
        benchmark_writing_methods.main()

    assert result.read_text() == saved
    assert not (output / "report.json").exists()
    assert not (output / "grounding-3m" / "recipe_whole_session").exists()


def test_corpus_rejects_mixed_profiles_instead_of_hiding_failed_candidate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    evaluation = EvaluationSet(
        evaluation_id="review-corpus",
        version=1,
        cases=[
            EvaluationCase(id="grounding", category="grounding", prompt="Rest", duration_seconds=60)
        ],
    )
    metadata = AdapterMetadata(
        adapter_id="test",
        versioned_model_id="fixture@1",
        runtime_id="test",
        runtime_version="1",
        license_id="test",
        device="cpu",
    )
    profiles: tuple[tuple[Literal["lite", "standard"], bool], ...] = (
        ("lite", False),
        ("standard", True),
    )
    report = BakeoffReport(
        evaluation_id=evaluation.evaluation_id,
        evaluation_version=1,
        created_at=datetime.now(UTC),
        seed=42,
        platform="test",
        candidates={"lite": metadata, "standard": metadata},
        results=[
            BakeoffCaseResult(
                case_id="grounding",
                category="grounding",
                profile=profile,
                success=success,
                elapsed_seconds=1,
                peak_process_tree_mb=1,
                model_artifact_bytes=1,
                validation_retries=0,
                safety_validation_passed=success,
                failure=None if success else "Generation failed",
            )
            for profile, success in profiles
        ],
    )
    evaluation_path = tmp_path / "evaluation.yaml"
    evaluation_path.write_text(evaluation.model_dump_json())
    report_path = tmp_path / "report.json"
    report_path.write_text(report.model_dump_json())
    output = tmp_path / "acceptance.json"

    with pytest.raises(SystemExit, match="2"):
        meditation_corpus_report.main(
            [
                "--evaluation-set",
                str(evaluation_path),
                "--bakeoff-report",
                str(report_path),
                "--output",
                str(output),
            ]
        )

    assert not output.exists()
    assert "exactly one profile" in capsys.readouterr().err
