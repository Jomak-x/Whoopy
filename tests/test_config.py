from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from whoopy.config import ConfigError, load_settings


def test_default_configuration_loads() -> None:
    settings = load_settings(environment={})

    assert settings.llm.backend == "auto"
    assert settings.tts.backend == "auto"
    assert settings.hardware.profile == "auto"
    assert settings.pipeline.checkpoint_dir == Path("runs")


def test_local_environment_and_cli_precedence(tmp_path: Path) -> None:
    (tmp_path / "default.yaml").write_text(
        Path("config/default.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / "local.yaml").write_text("tts:\n  voice: local_voice\n", encoding="utf-8")

    local_settings = load_settings(tmp_path, environment={})
    environment_settings = load_settings(
        tmp_path,
        environment={"WHOOPY_TTS__VOICE": "environment_voice"},
    )
    cli_settings = load_settings(
        tmp_path,
        environment={"WHOOPY_TTS__VOICE": "environment_voice"},
        cli_overrides={"tts": {"voice": "cli_voice"}},
    )

    assert local_settings.tts.voice == "local_voice"
    assert environment_settings.tts.voice == "environment_voice"
    assert cli_settings.tts.voice == "cli_voice"


def test_missing_local_file_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "default.yaml").write_text(
        Path("config/default.yaml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    assert load_settings(tmp_path, environment={}).tts.voice == "af_heart"


def test_missing_default_file_has_readable_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="Required configuration file not found"):
        load_settings(tmp_path, environment={})


def test_invalid_setting_has_readable_error() -> None:
    with pytest.raises(ConfigError, match=r"tts\.speed"):
        load_settings(environment={"WHOOPY_TTS__SPEED": "0"})


def test_unknown_environment_setting_is_rejected() -> None:
    with pytest.raises(ConfigError, match="unknown"):
        load_settings(environment={"WHOOPY_TTS__UNKNOWN": "value"})


def test_unknown_runtime_profile_is_rejected() -> None:
    with pytest.raises(ConfigError, match=r"hardware\.profile"):
        load_settings(environment={"WHOOPY_HARDWARE__PROFILE": "impossible"})


@pytest.mark.parametrize(
    "path",
    [
        "config/models.yaml",
        "config/pacing_profiles.yaml",
        "config/runtime_profiles.yaml",
    ],
)
def test_registry_skeletons_are_valid_yaml_mappings(path: str) -> None:
    document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    assert isinstance(document, dict)
    assert document
