from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

import whoopy.locations as locations
from whoopy.locations import default_config_directory, runtime_worker_script


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


def test_wheel_declares_complete_local_product_data() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    data_files = project["tool"]["setuptools"]["data-files"]

    assert "config/*.yaml" in data_files["share/whoopy/config"]
    assert "config/prompts/*.md" in data_files["share/whoopy/config/prompts"]
    assert "config/evaluation/*.yaml" in data_files["share/whoopy/config/evaluation"]
    assert set(data_files["share/whoopy/scripts"]) == {
        "scripts/fish_speech_1_4_worker.py",
        "scripts/moss_tts_worker.py",
        "scripts/qwen3_tts_worker.py",
    }
    assert project["tool"]["setuptools"]["package-data"]["whoopy.webui"] == [
        "static/*",
        "static/**/*",
    ]
