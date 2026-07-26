"""Typed, layered configuration loading for all Whoopy entry points.

The precedence is deliberately centralized here:

1. `config/default.yaml`
2. `config/local.yaml` when present
3. `WHOOPY_*` environment variables
4. explicit CLI overrides

Future API and worker processes should call this module instead of inventing
their own environment handling.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class ConfigError(ValueError):
    """Raised when Whoopy configuration cannot be loaded or validated."""


class StrictSettings(BaseModel):
    """Reject misspelled keys instead of silently ignoring them."""

    model_config = ConfigDict(extra="forbid")


class LLMSettings(StrictSettings):
    backend: str
    hq_backend: str | None = None
    max_context_tokens: int = Field(ge=1)


class TTSSettings(StrictSettings):
    backend: str
    voice: str
    speed: float = Field(gt=0)


class HardwareSettings(StrictSettings):
    profile: Literal["auto", "basic", "lite", "standard", "high", "studio"] = "auto"
    allow_remote_fallback: bool = False


class AmbienceSettings(StrictSettings):
    backend: str
    default_tags: list[str] = Field(default_factory=list)


class RenderSettings(StrictSettings):
    target_lufs: float
    true_peak_db: float
    master_format: str
    delivery: list[str] = Field(min_length=1)


class PauseSettings(StrictSettings):
    micro_max_ms: int = Field(ge=0)
    deliberate_min_ms: int = Field(gt=0)


class PipelineSettings(StrictSettings):
    checkpoint_dir: Path
    cache: str


class StorageSettings(StrictSettings):
    db_url: str


class Settings(StrictSettings):
    """Complete Phase 0 configuration contract."""

    llm: LLMSettings
    tts: TTSSettings
    hardware: HardwareSettings
    ambience: AmbienceSettings
    render: RenderSettings
    pauses: PauseSettings
    pipeline: PipelineSettings
    storage: StorageSettings


def _read_yaml(path: Path, *, required: bool) -> dict[str, Any]:
    if not path.exists():
        if required:
            raise ConfigError(f"Required configuration file not found: {path}")
        return {}

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"Could not read {path}: {error}") from error

    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Configuration root in {path} must be a mapping")
    return raw


def _deep_merge(base: dict[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    """Merge nested mappings while replacing scalar and list values."""

    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = value
    return merged


def _environment_overrides(environment: Mapping[str, str]) -> dict[str, Any]:
    prefix = "WHOOPY_"
    overrides: dict[str, Any] = {}

    for name, raw_value in environment.items():
        if not name.startswith(prefix):
            continue
        path = [part.lower() for part in name[len(prefix) :].split("__") if part]
        if len(path) != 2:
            raise ConfigError(f"Environment setting {name} must use WHOOPY_<SECTION>__<FIELD>")

        # YAML scalar parsing makes `false`, `12`, and `[rain, drone]` useful
        # without maintaining a separate type-conversion table.
        try:
            value = yaml.safe_load(raw_value)
        except yaml.YAMLError as error:
            raise ConfigError(f"Invalid value for {name}: {error}") from error
        overrides.setdefault(path[0], {})[path[1]] = value

    return overrides


def load_settings(
    config_dir: Path = Path("config"),
    *,
    environment: Mapping[str, str] | None = None,
    cli_overrides: Mapping[str, Any] | None = None,
) -> Settings:
    """Load and validate settings using the documented precedence order."""

    values = _read_yaml(config_dir / "default.yaml", required=True)
    values = _deep_merge(values, _read_yaml(config_dir / "local.yaml", required=False))
    active_environment = environment if environment is not None else os.environ
    values = _deep_merge(values, _environment_overrides(active_environment))
    values = _deep_merge(values, cli_overrides or {})

    try:
        return Settings.model_validate(values)
    except ValidationError as error:
        raise ConfigError(f"Invalid Whoopy configuration:\n{error}") from error
