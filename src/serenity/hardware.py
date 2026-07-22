"""Cross-platform capability inspection and safe runtime-profile selection.

Phase 0 deliberately measures only resources available through stable operating-
system APIs. Later runtime PRs add short llama.cpp and TTS benchmarks before a
specific model artifact is downloaded.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Any

import psutil
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from serenity.config import ConfigError

DEFAULT_INSPECTION_PATH = Path.cwd()
SUPPORTED_TARGETS = {
    ("darwin", "arm64"),
    ("darwin", "x86_64"),
    ("linux", "arm64"),
    ("linux", "x86_64"),
    ("windows", "arm64"),
    ("windows", "x86_64"),
}


class HardwareSnapshot(BaseModel):
    """Portable facts used to make a recommendation without loading a model."""

    model_config = ConfigDict(frozen=True)

    operating_system: str
    architecture: str
    cpu_count: int = Field(ge=1)
    total_ram_gb: float = Field(gt=0)
    available_ram_gb: float = Field(ge=0)
    free_disk_gb: float = Field(ge=0)
    accelerators: list[str]


class RuntimeProfile(BaseModel):
    """Minimum safe resources and capabilities for one user-facing tier."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    rank: int = Field(ge=0)
    min_total_ram_gb: float = Field(gt=0)
    min_available_ram_gb: float = Field(gt=0)
    min_free_disk_gb: float = Field(gt=0)
    approximate_download_gb: float = Field(ge=0)
    llm_runtime: str
    llm_model_class: str
    tts_runtime: str
    modes: list[str] = Field(min_length=1)


class DoctorResult(BaseModel):
    """Structured output suitable for the CLI today and the future UI."""

    model_config = ConfigDict(frozen=True)

    supported: bool
    snapshot: HardwareSnapshot
    selected_profile: RuntimeProfile | None
    messages: list[str]


def _gib(byte_count: int) -> float:
    return round(byte_count / (1024**3), 1)


def _has_working_command(command: list[str]) -> bool:
    executable = shutil.which(command[0])
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, *command[1:]],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def inspect_hardware(path: Path = DEFAULT_INSPECTION_PATH) -> HardwareSnapshot:
    """Inspect resources using APIs available on Windows, macOS, and Linux."""

    memory = psutil.virtual_memory()
    disk = psutil.disk_usage(str(path))
    operating_system = platform.system().lower()
    architecture = platform.machine().lower()
    if architecture in {"amd64", "x64"}:
        architecture = "x86_64"
    elif architecture == "aarch64":
        architecture = "arm64"
    accelerators = ["cpu"]

    # Apple Silicon exposes Metal as an OS capability. Runtime-level validation
    # is deferred until the llama.cpp binary is present.
    if operating_system == "darwin" and architecture in {"arm64", "aarch64"}:
        accelerators.append("metal")
    if _has_working_command(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"]):
        accelerators.append("cuda")

    return HardwareSnapshot(
        operating_system=operating_system,
        architecture=architecture,
        cpu_count=psutil.cpu_count(logical=True) or 1,
        total_ram_gb=_gib(memory.total),
        available_ram_gb=_gib(memory.available),
        free_disk_gb=_gib(disk.free),
        accelerators=accelerators,
    )


def load_runtime_profiles(path: Path) -> list[RuntimeProfile]:
    """Load user-facing profiles from a versioned, reviewable YAML registry."""

    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError(f"Could not read runtime profiles from {path}: {error}") from error
    if not isinstance(document, dict) or not document:
        raise ConfigError(f"Runtime profiles in {path} must be a non-empty mapping")

    try:
        profiles = [
            RuntimeProfile.model_validate({"name": name, **values})
            for name, values in document.items()
            if isinstance(values, dict)
        ]
    except (TypeError, ValidationError) as error:
        raise ConfigError(f"Invalid runtime profiles in {path}: {error}") from error
    if len(profiles) != len(document):
        raise ConfigError(f"Every runtime profile in {path} must be a mapping")
    return sorted(profiles, key=lambda profile: profile.rank, reverse=True)


def diagnose(
    snapshot: HardwareSnapshot,
    profiles: list[RuntimeProfile],
    requested_profile: str = "auto",
) -> DoctorResult:
    """Choose the highest profile that fits conservative live-resource margins."""

    if (snapshot.operating_system, snapshot.architecture) not in SUPPORTED_TARGETS:
        return DoctorResult(
            supported=False,
            snapshot=snapshot,
            selected_profile=None,
            messages=[
                (
                    f"No native release target is defined for {snapshot.operating_system} "
                    f"{snapshot.architecture}."
                ),
                "Serenity will not download or load a model on an untested target.",
            ],
        )

    candidates = profiles
    if requested_profile != "auto":
        candidates = [profile for profile in profiles if profile.name == requested_profile]
        if not candidates:
            return DoctorResult(
                supported=False,
                snapshot=snapshot,
                selected_profile=None,
                messages=[f"Unknown runtime profile: {requested_profile}"],
            )

    for profile in candidates:
        if (
            snapshot.total_ram_gb >= profile.min_total_ram_gb
            and snapshot.available_ram_gb >= profile.min_available_ram_gb
            and snapshot.free_disk_gb >= profile.min_free_disk_gb
        ):
            mode_summary = ", ".join(profile.modes)
            return DoctorResult(
                supported=True,
                snapshot=snapshot,
                selected_profile=profile,
                messages=[
                    f"Selected {profile.name} because its RAM and disk safety margins fit.",
                    f"Available modes: {mode_summary}.",
                    "No model has been downloaded or loaded by this check.",
                ],
            )

    minimum = min(candidates, key=lambda profile: profile.rank)
    profile_label = "Basic" if requested_profile == "auto" else minimum.name
    messages = [
        f"This machine does not currently meet the {profile_label} profile safety margins.",
        (
            f"{profile_label} requires {minimum.min_total_ram_gb:g} GB total RAM, "
            f"{minimum.min_available_ram_gb:g} GB currently available RAM, and "
            f"{minimum.min_free_disk_gb:g} GB free disk."
        ),
        (
            "Free memory or disk space and run the check again; "
            "Serenity will not attempt a model load."
        ),
    ]
    return DoctorResult(
        supported=False,
        snapshot=snapshot,
        selected_profile=None,
        messages=messages,
    )
