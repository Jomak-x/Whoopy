from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import whoopy.locations as locations
from whoopy.config import load_settings
from whoopy.locations import default_config_directory, runtime_worker_script
from whoopy.release.status import assess_package


def test_installed_distribution_falls_back_to_bundled_config_and_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "venv"))
    monkeypatch.setattr(locations, "SOURCE_PROJECT_ROOT", tmp_path / "missing-checkout")
    data_root = tmp_path / "venv" / "share" / "whoopy"
    config = data_root / "config"
    workers = data_root / "scripts"
    config.mkdir(parents=True)
    workers.mkdir(parents=True)
    (config / "default.yaml").write_text("schema_version: 1\n", encoding="utf-8")
    (workers / "qwen3_tts_worker.py").write_text("# worker\n", encoding="utf-8")

    assert default_config_directory() == config
    assert runtime_worker_script("qwen3_tts_worker.py") == workers / "qwen3_tts_worker.py"


def test_built_wheel_contains_product_data_without_local_settings(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    project = tmp_path / "project"
    project.mkdir()
    for directory in ("src", "config", "docs", "scripts"):
        shutil.copytree(
            root / directory,
            project / directory,
            ignore=shutil.ignore_patterns("local.yaml", "__pycache__", "*.egg-info"),
        )
    for name in ("pyproject.toml", "README.md", "uv.lock"):
        shutil.copy2(root / name, project / name)
    (project / "config/local.yaml").write_text(
        "tts:\n  voice: synthetic-private-voice\n", encoding="utf-8"
    )
    assert load_settings(project / "config", environment={}).tts.voice == "synthetic-private-voice"

    uv = shutil.which("uv")
    assert uv is not None, "The wheel regression requires the project's uv build tool"
    result = subprocess.run(
        [uv, "build", "--offline", "--out-dir", str(tmp_path / "dist"), str(project)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    package_status = assess_package(project, tmp_path / "dist")
    assert package_status.outcome == "passed", package_status.blockers
    (project / "config/local.yaml").write_text("tts:\n  voice: changed-private-voice\n")
    assert assess_package(project, tmp_path / "dist").outcome == "passed"
    wheel = next((tmp_path / "dist").glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert not any(name.endswith("/local.yaml") for name in names)
        data_prefix = "whoopy-0.1.0.data/data/share/whoopy/"
        files = {name.removeprefix(data_prefix) for name in names if name.startswith(data_prefix)}
        assert {
            "config/default.yaml",
            "config/experimental_tts.yaml",
            "config/artifacts.yaml",
            "config/legacy_cleanup.yaml",
            "config/meditation_blueprints.yaml",
            "config/model_packs.yaml",
            "config/models.yaml",
            "config/pacing_profiles.yaml",
            "config/runtime_build_recipes.yaml",
            "config/runtime_packs.yaml",
            "config/runtime_profiles.yaml",
            "config/session_recipes.yaml",
            "config/voice_references.yaml",
            "config/prompts/whole_session_system.md",
            "config/evaluation/local-v1.yaml",
            "scripts/fish_speech_1_4_worker.py",
            "scripts/moss_tts_worker.py",
            "scripts/qwen3_tts_worker.py",
            "scripts/mlx_qwen3_tts_worker.py",
            "docs/experimental-mlx-qwen.md",
        } <= files
        assert "whoopy/adapters/tts/mlx_installation.py" in names
        assert "whoopy/adapters/tts/mlx_qwen3_tts.py" in names
        assert any(name.startswith("whoopy/webui/static/") for name in names)
        archive.extractall(tmp_path / "installed")
    config = tmp_path / "installed" / data_prefix / "config"
    assert load_settings(config, environment={}).tts.voice == "af_heart"
    assert (config / "experimental_tts.yaml").read_bytes() == (
        root / "config/experimental_tts.yaml"
    ).read_bytes()
    assert (config.parent / "scripts/mlx_qwen3_tts_worker.py").read_bytes() == (
        root / "scripts/mlx_qwen3_tts_worker.py"
    ).read_bytes()

    for relative in ("scripts/mlx_qwen3_tts_worker.py", "docs/experimental-mlx-qwen.md"):
        source = project / relative
        original = source.read_bytes()
        source.write_bytes(original + b"\n")
        stale = assess_package(project, tmp_path / "dist")
        assert stale.outcome == "stale"
        assert f"Wheel does not match current source: {source}" in stale.blockers
        source.write_bytes(original)
