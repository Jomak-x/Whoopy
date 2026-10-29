"""Exact desktop release targets and adapter-neutral backend selection."""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from whoopy.hardware import HardwareSnapshot, inspect_hardware

EvidenceStatus = Literal["passed", "failed", "not_run"]
BackendKind = Literal["cpu", "metal", "cuda", "vulkan"]

_TARGETS: tuple[tuple[str, str, str], ...] = (
    ("windows", "x86_64", "Windows x64"),
    ("darwin", "arm64", "macOS Apple Silicon"),
    ("darwin", "x86_64", "macOS Intel"),
    ("linux", "x86_64", "Linux x64"),
    ("linux", "arm64", "Linux ARM64"),
)


class ReleaseTarget(BaseModel):
    """Normalized current or declared release platform."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operating_system: str
    architecture: str
    target_id: str
    display_name: str
    supported: bool


class PlatformMatrixEntry(BaseModel):
    """One target's evidence state; support never implies it was tested."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: ReleaseTarget
    current_machine: bool
    status: EvidenceStatus = "not_run"
    evidence: str | None = None


class BackendSelection(BaseModel):
    """Friendly automatic choice while retaining inspectable backend evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: BackendKind
    display_name: str
    reason: str
    available: tuple[BackendKind, ...]


def _normalize_architecture(value: str) -> str:
    normalized = value.lower().strip()
    if normalized in {"amd64", "x64", "x86-64"}:
        return "x86_64"
    if normalized in {"aarch64", "arm64", "arm64e"}:
        return "arm64"
    return normalized


def _normalize_system(value: str) -> str:
    normalized = value.lower().strip()
    if normalized in {"macos", "mac", "osx"}:
        return "darwin"
    if normalized.startswith("win"):
        return "windows"
    return normalized


def detect_release_target(
    *, operating_system: str | None = None, architecture: str | None = None
) -> ReleaseTarget:
    """Normalize a platform pair and match it against the five Local V1 targets."""

    system = _normalize_system(operating_system or platform.system())
    machine = _normalize_architecture(architecture or platform.machine())
    match = next(
        (
            (target_system, target_arch, display)
            for target_system, target_arch, display in _TARGETS
            if (target_system, target_arch) == (system, machine)
        ),
        None,
    )
    if match is None:
        return ReleaseTarget(
            operating_system=system,
            architecture=machine,
            target_id=f"{system}-{machine}",
            display_name=f"{system} {machine}",
            supported=False,
        )
    return ReleaseTarget(
        operating_system=match[0],
        architecture=match[1],
        target_id=f"{match[0]}-{match[1]}",
        display_name=match[2],
        supported=True,
    )


def build_platform_matrix(
    current: ReleaseTarget,
    *,
    current_status: EvidenceStatus = "not_run",
    current_evidence: str | None = None,
) -> tuple[PlatformMatrixEntry, ...]:
    """Build the complete matrix without granting evidence to unrun targets."""

    entries: list[PlatformMatrixEntry] = []
    for system, architecture, _display in _TARGETS:
        target = detect_release_target(operating_system=system, architecture=architecture)
        is_current = target.target_id == current.target_id
        entries.append(
            PlatformMatrixEntry(
                target=target,
                current_machine=is_current,
                status=current_status if is_current else "not_run",
                evidence=current_evidence if is_current else None,
            )
        )
    return tuple(entries)


def inspect_release_hardware(path: Path | None = None) -> HardwareSnapshot:
    """Extend the normal hardware snapshot with a real Vulkan loader probe."""

    snapshot = inspect_hardware(path or Path.cwd())
    if "vulkan" in snapshot.accelerators or not _has_working_vulkan():
        return snapshot
    return snapshot.model_copy(update={"accelerators": [*snapshot.accelerators, "vulkan"]})


def _has_working_vulkan() -> bool:
    executable = shutil.which("vulkaninfo")
    if executable is None:
        return False
    try:
        completed = subprocess.run(
            [executable, "--summary"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def select_friendly_backend(snapshot: HardwareSnapshot) -> BackendSelection:
    """Choose the safest accelerated backend without exposing setup knobs."""

    declared = {value.lower() for value in snapshot.accelerators}
    available: list[BackendKind] = ["cpu"]
    for candidate in ("metal", "cuda", "vulkan"):
        if candidate in declared:
            available.append(candidate)

    if snapshot.operating_system == "darwin" and "metal" in available:
        return BackendSelection(
            backend="metal",
            display_name="Automatic - Apple GPU",
            reason="Metal was probed on this Mac and is preferred for local inference.",
            available=tuple(available),
        )
    if "cuda" in available:
        return BackendSelection(
            backend="cuda",
            display_name="Automatic - NVIDIA GPU",
            reason="A working CUDA-capable NVIDIA device was probed.",
            available=tuple(available),
        )
    if "vulkan" in available:
        return BackendSelection(
            backend="vulkan",
            display_name="Automatic - compatible GPU",
            reason="A working Vulkan compute runtime was probed.",
            available=tuple(available),
        )
    return BackendSelection(
        backend="cpu",
        display_name="Automatic - processor",
        reason="No verified GPU inference backend is available; CPU is the portable fallback.",
        available=tuple(available),
    )
