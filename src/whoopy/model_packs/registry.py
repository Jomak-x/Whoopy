"""Versioned, read-only inspection of Whoopy's local model packs.

The registry never downloads, imports, loads, deletes, or moves a model.  Its
job is narrower and safety-critical: compare files already on disk with a
reviewed manifest and explain exactly why a pack is or is not ready.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import socket
import sys
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from whoopy.hardware import HardwareSnapshot, inspect_hardware


class ModelPackError(ValueError):
    """Raised when a model-pack manifest or requested pack is invalid."""


class ModelPackState(StrEnum):
    """Honest local state, from absent files through proven readiness."""

    MISSING = "missing"
    PARTIAL = "partial"
    CORRUPT = "corrupt"
    INSTALLED = "installed"
    RESOURCE_BLOCKED = "resource_blocked"
    INCOMPATIBLE = "incompatible"
    READY = "ready"


class FileRole(StrEnum):
    """Purpose of a pinned file inside a model pack."""

    MODEL = "model"
    TOKENIZER = "tokenizer"
    CONFIG = "config"
    SHARD_INDEX = "shard_index"


class DigestSpec(BaseModel):
    """A published content digest and the algorithm used to verify it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    algorithm: Literal["sha256", "git_sha1"] = "sha256"
    value: str

    @model_validator(mode="after")
    def _validate_value(self) -> DigestSpec:
        expected_length = 64 if self.algorithm == "sha256" else 40
        normalized = self.value.lower()
        if len(normalized) != expected_length or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError(f"{self.algorithm} must be {expected_length} hexadecimal characters")
        object.__setattr__(self, "value", normalized)
        return self


def _safe_relative_path(value: Path) -> Path:
    if value.is_absolute() or not value.parts or ".." in value.parts:
        raise ValueError("must be a safe relative path")
    return value


class PinnedFileSpec(BaseModel):
    """One immutable file required for a pack revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    size_bytes: int = Field(gt=0)
    digest: DigestSpec
    role: FileRole

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: Path) -> Path:
        return _safe_relative_path(value)


class ShardIndexSpec(BaseModel):
    """Relationship between a Transformers index and its expected shards."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    shard_paths: list[Path] = Field(min_length=1)

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: Path) -> Path:
        return _safe_relative_path(value)

    @field_validator("shard_paths")
    @classmethod
    def _validate_shards(cls, paths: list[Path]) -> list[Path]:
        checked = [_safe_relative_path(path) for path in paths]
        if len(set(checked)) != len(checked):
            raise ValueError("shard paths must be unique")
        return checked


class RuntimeSpec(BaseModel):
    """Pinned isolated runtime expected to load a pack."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: str
    revision: str
    candidate_directories: list[Path] = Field(min_length=1)
    required_markers: list[Path] = Field(min_length=1)

    @field_validator("candidate_directories", "required_markers")
    @classmethod
    def _validate_paths(cls, paths: list[Path]) -> list[Path]:
        return [_safe_relative_path(path) for path in paths]


class HardwareRequirement(BaseModel):
    """Conservative resources required before attempting a model load."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    min_total_ram_gb: float = Field(gt=0)
    min_available_ram_gb: float = Field(gt=0)
    min_free_disk_gb: float = Field(ge=0)
    accelerator_any_of: list[str] = Field(default_factory=lambda: ["cpu"], min_length=1)


class ModelPackSpec(BaseModel):
    """One model family revision and all facts needed to inspect it offline."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str
    display_name: str
    revision: str
    installed_tree_sha256: str | None = None
    installed_tree_directory: Path | None = None
    source_repository: str
    license_id: str
    license_url: str
    commercial_use_allowed: bool
    license_notice: str
    managed_directory: Path
    existing_directories: list[Path] = Field(default_factory=list)
    supported_platforms: list[str] = Field(min_length=1)
    files: list[PinnedFileSpec] = Field(min_length=1)
    shard_indexes: list[ShardIndexSpec] = Field(default_factory=list)
    runtime: RuntimeSpec
    hardware: HardwareRequirement
    dependencies: list[str] = Field(default_factory=list)

    @field_validator("pack_id")
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789.-_"
        if not value or any(character not in allowed for character in value):
            raise ValueError("must be a lowercase model-pack identifier")
        return value

    @field_validator("source_repository", "license_url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("must use HTTPS")
        return value

    @field_validator("installed_tree_sha256")
    @classmethod
    def _validate_tree_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("installed_tree_sha256 must be 64 hexadecimal characters")
        return normalized

    @field_validator("managed_directory")
    @classmethod
    def _validate_managed_path(cls, value: Path) -> Path:
        return _safe_relative_path(value)

    @field_validator("installed_tree_directory")
    @classmethod
    def _validate_tree_directory(cls, value: Path | None) -> Path | None:
        return _safe_relative_path(value) if value is not None else None

    @field_validator("existing_directories")
    @classmethod
    def _validate_existing_paths(cls, paths: list[Path]) -> list[Path]:
        return [_safe_relative_path(path) for path in paths]

    @model_validator(mode="after")
    def _validate_files_and_indexes(self) -> ModelPackSpec:
        if self.installed_tree_directory is not None and self.installed_tree_sha256 is None:
            raise ValueError("installed_tree_directory requires installed_tree_sha256")
        paths = [file.path for file in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("pinned file paths must be unique")
        file_by_path = {file.path: file for file in self.files}
        for index in self.shard_indexes:
            index_file = file_by_path.get(index.path)
            if index_file is None or index_file.role != FileRole.SHARD_INDEX:
                raise ValueError(f"shard index {index.path} must be a pinned shard_index file")
            unknown = sorted(set(index.shard_paths) - set(paths))
            if unknown:
                raise ValueError(f"shard index references unpinned files: {unknown}")
        if len(set(self.dependencies)) != len(self.dependencies):
            raise ValueError("dependencies must be unique")
        return self


class ModelPackManifest(BaseModel):
    """Complete versioned model-pack registry document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    packs: list[ModelPackSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_registry(self) -> ModelPackManifest:
        pack_ids = [pack.pack_id for pack in self.packs]
        if len(set(pack_ids)) != len(pack_ids):
            raise ValueError("model-pack IDs must be unique")
        known = set(pack_ids)
        for pack in self.packs:
            unknown = sorted(set(pack.dependencies) - known)
            if unknown:
                raise ValueError(f"pack {pack.pack_id} has unknown dependencies: {unknown}")
            if pack.pack_id in pack.dependencies:
                raise ValueError(f"pack {pack.pack_id} cannot depend on itself")

        by_id = {pack.pack_id: pack for pack in self.packs}
        visited: set[str] = set()
        active: list[str] = []

        def visit(pack_id: str) -> None:
            if pack_id in visited:
                return
            if pack_id in active:
                start = active.index(pack_id)
                cycle = " -> ".join((*active[start:], pack_id))
                raise ValueError(f"model-pack dependency graph contains a cycle: {cycle}")
            active.append(pack_id)
            for dependency in by_id[pack_id].dependencies:
                visit(dependency)
            active.pop()
            visited.add(pack_id)

        for pack_id in pack_ids:
            visit(pack_id)
        return self


class MachineIdentity(BaseModel):
    """Stable-enough local identity used to scope probe evidence to one laptop."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    machine_id: str
    operating_system: str
    architecture: str

    @classmethod
    def current(cls) -> MachineIdentity:
        operating_system = platform.system().lower()
        architecture = _normalize_architecture(platform.machine())
        # Store only a one-way digest, not the potentially identifying hostname.
        raw_identity = f"{socket.gethostname()}\0{operating_system}\0{architecture}".encode()
        return cls(
            machine_id=hashlib.sha256(raw_identity).hexdigest(),
            operating_system=operating_system,
            architecture=architecture,
        )


class RuntimeEvidence(BaseModel):
    """Result of starting a runtime in its isolated environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    pack_id: str
    model_revision: str
    runtime_revision: str
    runtime_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id: str
    checked_at: datetime
    success: bool
    message: str


class SmokeTestEvidence(BaseModel):
    """Offline synthesis proof tied to a specific machine and model revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    pack_id: str
    model_revision: str
    runtime_revision: str
    runtime_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id: str
    checked_at: datetime
    success: bool
    offline: bool
    output_sha256: str | None = None
    output_duration_seconds: float | None = Field(default=None, gt=0)
    message: str

    @field_validator("output_sha256")
    @classmethod
    def _validate_output_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("output_sha256 must be 64 hexadecimal characters")
        return normalized

    @model_validator(mode="after")
    def _successful_smoke_has_audio(self) -> SmokeTestEvidence:
        if self.success and (
            not self.offline or self.output_sha256 is None or self.output_duration_seconds is None
        ):
            raise ValueError("successful smoke evidence requires offline audio output")
        return self


class FileInspection(BaseModel):
    """Observed state of one pinned file without modifying it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    relative_path: Path
    absolute_path: Path
    exists: bool
    expected_size_bytes: int
    actual_size_bytes: int | None
    digest_verified: bool
    message: str


class ReadinessCheck(BaseModel):
    """One explicit condition contributing to the final pack state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: str
    passed: bool
    message: str


class ModelPackStatus(BaseModel):
    """Complete, reproducible explanation of one local pack's state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pack_id: str
    display_name: str
    revision: str
    state: ModelPackState
    selected_directory: Path
    license_id: str
    license_url: str
    commercial_use_allowed: bool
    license_notice: str
    files: list[FileInspection]
    checks: list[ReadinessCheck]


def _normalize_architecture(value: str) -> str:
    normalized = value.lower()
    if normalized in {"amd64", "x64"}:
        return "x86_64"
    if normalized == "aarch64":
        return "arm64"
    return normalized


def _digest(path: Path, spec: DigestSpec) -> str:
    digest = hashlib.sha256() if spec.algorithm == "sha256" else hashlib.sha1()
    if spec.algorithm == "git_sha1":
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """Hash extracted paths, file bytes, and symlink targets deterministically.

    This intentionally matches :mod:`whoopy.artifacts` so a bundle adopted
    from the artifact installer has one canonical extracted-tree identity.
    """

    digest = hashlib.sha256()
    paths = sorted(root.rglob("*"), key=lambda path: path.relative_to(root).as_posix())
    for path in paths:
        relative = path.relative_to(root).as_posix().encode()
        if path.is_symlink():
            digest.update(b"link\0" + relative + b"\0")
            digest.update(os.readlink(path).encode())
        elif path.is_dir():
            digest.update(b"directory\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"file\0" + relative + b"\0")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        else:
            raise ModelPackError(f"Unsupported model-pack filesystem entry: {path}")
    return digest.hexdigest()


def _read_evidence(path: Path, model: type[RuntimeEvidence] | type[SmokeTestEvidence]) -> Any:
    try:
        return model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError):
        return None


_RUNTIME_PROJECT_SOURCES: dict[str, tuple[Path, ...]] = {
    "fish-speech": (
        Path("scripts/fish_speech_1_4_worker.py"),
        Path("src/whoopy/adapters/tts/fish_speech.py"),
        Path("src/whoopy/adapters/tts/_json_process.py"),
    ),
    "moss-tts": (
        Path("scripts/moss_tts_worker.py"),
        Path("src/whoopy/adapters/tts/moss_tts.py"),
        Path("src/whoopy/adapters/tts/_json_process.py"),
    ),
    "sherpa-onnx": (Path("src/whoopy/adapters/tts/sherpa_onnx.py"),),
}


def _runtime_fingerprint(runtime_directory: Path, runtime: RuntimeSpec) -> str:
    """Hash the executable runtime facts that made a smoke probe meaningful.

    A manifest revision alone cannot detect a locally replaced interpreter,
    edited worker, or changed Python environment.  The fingerprint therefore
    includes the current platform/interpreter ABI, every declared marker,
    Whoopy's runtime-facing worker sources, conventional lock manifests, and
    installed distribution metadata.  READY evidence becomes stale whenever
    any one of those inputs changes.
    """

    digest = hashlib.sha256()

    def add_value(label: str, value: str) -> None:
        encoded_label = label.encode("utf-8")
        encoded_value = value.encode("utf-8")
        digest.update(len(encoded_label).to_bytes(4, "big"))
        digest.update(encoded_label)
        digest.update(len(encoded_value).to_bytes(8, "big"))
        digest.update(encoded_value)

    def add_file(label: str, path: Path) -> None:
        if not path.is_file():
            add_value(label, "missing")
            return
        file_digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                file_digest.update(chunk)
        add_value(label, f"{path.stat().st_size}:{file_digest.hexdigest()}")

    add_value("runtime_id", runtime.runtime_id)
    add_value("runtime_revision", runtime.revision)
    add_value("platform", platform.platform())
    add_value("architecture", _normalize_architecture(platform.machine()))
    add_value("host_python", f"{sys.implementation.name}:{sys.version_info[:3]}")

    for marker in sorted(runtime.required_markers, key=lambda item: item.as_posix()):
        add_file(f"marker:{marker.as_posix()}", runtime_directory / marker)

    interpreter_candidates = (
        runtime_directory / ".venv" / "bin" / "python",
        runtime_directory / ".venv" / "Scripts" / "python.exe",
        runtime_directory / "python",
        runtime_directory / "python.exe",
    )
    for interpreter in interpreter_candidates:
        if interpreter.is_file():
            add_file("runtime_interpreter", interpreter)
            break
    else:
        add_value("runtime_interpreter", "missing")

    project_root = Path(__file__).resolve().parents[3]
    for source in _RUNTIME_PROJECT_SOURCES.get(runtime.runtime_id, ()):
        add_file(f"project_source:{source.as_posix()}", project_root / source)

    lock_names = (
        "uv.lock",
        "poetry.lock",
        "Pipfile.lock",
        "requirements.txt",
        "requirements.lock",
        ".whoopy-wheels.json",
        "pyvenv.cfg",
    )
    lock_paths: set[Path] = set()
    for name in lock_names:
        for base in (runtime_directory, runtime_directory / ".venv"):
            candidate = base / name
            if candidate.is_file():
                lock_paths.add(candidate)
    for lock_path in sorted(lock_paths, key=lambda item: item.as_posix()):
        add_file(
            f"package_lock:{lock_path.relative_to(runtime_directory).as_posix()}",
            lock_path,
        )

    # Isolated environments are not always built from a retained lockfile.
    # Distribution metadata is the authoritative installed package snapshot.
    metadata_paths = sorted(
        runtime_directory.glob(".venv/lib/python*/site-packages/*.dist-info/METADATA"),
        key=lambda item: item.as_posix(),
    )
    if not metadata_paths:
        add_value("installed_packages", "none")
    for metadata in metadata_paths:
        add_file(
            f"installed_package:{metadata.parent.name}",
            metadata,
        )
    return digest.hexdigest()


class ModelPackRegistry:
    """Inspect all configured packs against one models directory."""

    def __init__(self, manifest: ModelPackManifest, models_root: Path) -> None:
        self.manifest = manifest
        self.models_root = models_root
        self._by_id = {pack.pack_id: pack for pack in manifest.packs}

    @property
    def packs(self) -> tuple[ModelPackSpec, ...]:
        """Return immutable pack declarations in manifest order."""

        return tuple(self.manifest.packs)

    def get(self, pack_id: str) -> ModelPackSpec:
        """Resolve a pack ID or raise a user-facing registry error."""

        try:
            return self._by_id[pack_id]
        except KeyError as error:
            available = ", ".join(sorted(self._by_id))
            raise ModelPackError(
                f"Unknown model pack {pack_id!r}; available: {available}"
            ) from error

    def records_directory(self, pack_id: str) -> Path:
        """Return the managed location for runtime and smoke evidence."""

        self.get(pack_id)
        return self.models_root / "managed" / "model-packs" / pack_id / "records"

    def runtime_directory(self, pack: ModelPackSpec | str) -> Path | None:
        """Resolve a complete declared runtime without importing or executing it."""

        spec = self.get(pack) if isinstance(pack, str) else pack
        return next(
            (
                self.models_root / candidate
                for candidate in spec.runtime.candidate_directories
                if all(
                    (self.models_root / candidate / marker).is_file()
                    for marker in spec.runtime.required_markers
                )
            ),
            None,
        )

    def runtime_fingerprint(self, pack: ModelPackSpec | str) -> str | None:
        """Return the current verified runtime fingerprint, if complete."""

        spec = self.get(pack) if isinstance(pack, str) else pack
        directory = self.runtime_directory(spec)
        return None if directory is None else _runtime_fingerprint(directory, spec.runtime)

    def list_statuses(
        self,
        *,
        hardware: HardwareSnapshot | None = None,
        machine: MachineIdentity | None = None,
        verify_digests: bool = True,
    ) -> list[ModelPackStatus]:
        """Inspect every pack; this is read-only and never imports a runtime."""

        active_hardware = hardware or inspect_hardware(self._existing_inspection_path())
        active_machine = machine or MachineIdentity.current()
        memo: dict[str, ModelPackStatus] = {}
        return [
            self._inspect(pack, active_hardware, active_machine, verify_digests, memo, set())
            for pack in self.manifest.packs
        ]

    def inspect(
        self,
        pack_id: str,
        *,
        hardware: HardwareSnapshot | None = None,
        machine: MachineIdentity | None = None,
        verify_digests: bool = True,
    ) -> ModelPackStatus:
        """Inspect one pack and all declared dependencies without changing disk."""

        active_hardware = hardware or inspect_hardware(self._existing_inspection_path())
        active_machine = machine or MachineIdentity.current()
        return self._inspect(
            self.get(pack_id),
            active_hardware,
            active_machine,
            verify_digests,
            {},
            set(),
        )

    def _existing_inspection_path(self) -> Path:
        """Find an existing ancestor for disk inspection before first install."""

        candidate = self.models_root.resolve(strict=False)
        while not candidate.exists() and candidate != candidate.parent:
            candidate = candidate.parent
        return candidate

    def _inspect(
        self,
        pack: ModelPackSpec,
        hardware: HardwareSnapshot,
        machine: MachineIdentity,
        verify_digests: bool,
        memo: dict[str, ModelPackStatus],
        visiting: set[str],
    ) -> ModelPackStatus:
        if pack.pack_id in memo:
            return memo[pack.pack_id]
        if pack.pack_id in visiting:
            raise ModelPackError(f"model-pack dependency cycle includes {pack.pack_id}")
        visiting.add(pack.pack_id)

        target = f"{hardware.operating_system}-{_normalize_architecture(hardware.architecture)}"
        platform_ok = "all" in pack.supported_platforms or target in pack.supported_platforms
        checks = [
            ReadinessCheck(
                check="platform",
                passed=platform_ok,
                message=(f"{target} is supported" if platform_ok else f"{target} is not supported"),
            )
        ]

        selected = self._select_directory(pack)
        files = self._inspect_files(pack, selected, verify_digests)
        present_count = sum(file.exists for file in files)
        corrupt_files = [
            file
            for file in files
            if file.exists
            and (
                file.actual_size_bytes != file.expected_size_bytes
                or (verify_digests and not file.digest_verified)
            )
        ]
        files_ok = present_count == len(files) and not corrupt_files
        checks.append(
            ReadinessCheck(
                check="pinned_files",
                passed=files_ok,
                message=(
                    "all pinned files match size and digest"
                    if files_ok
                    else f"{present_count} of {len(files)} pinned files are present"
                ),
            )
        )
        tree_check: ReadinessCheck | None = None
        if pack.installed_tree_sha256 is not None:
            tree_root = (
                self.models_root / pack.installed_tree_directory
                if pack.installed_tree_directory is not None
                else selected
            )
            if not tree_root.is_dir():
                tree_check = ReadinessCheck(
                    check="installed_tree",
                    passed=False,
                    message=f"the pinned extracted model tree is missing at {tree_root}",
                )
            elif not verify_digests:
                tree_check = ReadinessCheck(
                    check="installed_tree",
                    passed=False,
                    message="extracted-tree digest verification was skipped",
                )
            else:
                observed_tree = _tree_sha256(tree_root)
                tree_matches = observed_tree == pack.installed_tree_sha256
                tree_result = "matches" if tree_matches else "does not match"
                tree_check = ReadinessCheck(
                    check="installed_tree",
                    passed=tree_matches,
                    message=(
                        f"the complete extracted model tree at {tree_root} "
                        f"{tree_result} its pinned digest"
                    ),
                )
            checks.append(tree_check)

        if not platform_ok:
            state = ModelPackState.INCOMPATIBLE
        elif present_count == 0:
            state = ModelPackState.MISSING
        elif present_count < len(files):
            state = ModelPackState.PARTIAL
        elif corrupt_files or (tree_check is not None and verify_digests and not tree_check.passed):
            state = ModelPackState.CORRUPT
        else:
            index_check = self._check_shard_indexes(pack, selected)
            checks.append(index_check)
            if not index_check.passed:
                state = ModelPackState.CORRUPT
            elif not verify_digests:
                checks.append(
                    ReadinessCheck(
                        check="full_digest_verification",
                        passed=False,
                        message="digest verification was skipped; READY is not allowed",
                    )
                )
                state = ModelPackState.INSTALLED
            else:
                state = self._readiness_state(pack, hardware, machine, checks, memo, visiting)

        status = ModelPackStatus(
            pack_id=pack.pack_id,
            display_name=pack.display_name,
            revision=pack.revision,
            state=state,
            selected_directory=selected,
            license_id=pack.license_id,
            license_url=pack.license_url,
            commercial_use_allowed=pack.commercial_use_allowed,
            license_notice=pack.license_notice,
            files=files,
            checks=checks,
        )
        memo[pack.pack_id] = status
        visiting.remove(pack.pack_id)
        return status

    def _select_directory(self, pack: ModelPackSpec) -> Path:
        candidates = [pack.managed_directory, *pack.existing_directories]
        absolute = [self.models_root / candidate for candidate in candidates]
        # Prefer the directory containing the most exact required paths.  This
        # observes old experimental downloads in place without adopting,
        # renaming, or deleting any of them.
        return max(
            absolute,
            key=lambda directory: sum((directory / file.path).is_file() for file in pack.files),
        )

    def _inspect_files(
        self, pack: ModelPackSpec, directory: Path, verify_digests: bool
    ) -> list[FileInspection]:
        inspected: list[FileInspection] = []
        for file in pack.files:
            path = directory / file.path
            exists = path.is_file()
            actual_size = path.stat().st_size if exists else None
            digest_verified = False
            if exists and actual_size == file.size_bytes and verify_digests:
                digest_verified = _digest(path, file.digest) == file.digest.value
            elif exists and actual_size == file.size_bytes:
                # A caller can request the cheap size-only view explicitly, but
                # such a view can never yield READY below because evidence is
                # meaningful only after a full verification pass.
                digest_verified = False
            if not exists:
                message = "missing"
            elif actual_size != file.size_bytes:
                message = f"size mismatch: expected {file.size_bytes}, found {actual_size}"
            elif verify_digests and not digest_verified:
                message = f"{file.digest.algorithm} mismatch"
            elif verify_digests:
                message = "size and digest verified"
            else:
                message = "size matches; digest verification skipped"
            inspected.append(
                FileInspection(
                    relative_path=file.path,
                    absolute_path=path,
                    exists=exists,
                    expected_size_bytes=file.size_bytes,
                    actual_size_bytes=actual_size,
                    digest_verified=digest_verified,
                    message=message,
                )
            )
        return inspected

    def _check_shard_indexes(self, pack: ModelPackSpec, directory: Path) -> ReadinessCheck:
        if not pack.shard_indexes:
            return ReadinessCheck(check="shard_indexes", passed=True, message="not sharded")
        file_sizes = {file.path: file.size_bytes for file in pack.files}
        for index in pack.shard_indexes:
            try:
                document = json.loads((directory / index.path).read_text(encoding="utf-8"))
                weight_map = document["weight_map"]
                declared_shards = set(weight_map.values())
                total_size = document.get("metadata", {}).get("total_size")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, AttributeError):
                return ReadinessCheck(
                    check="shard_indexes", passed=False, message=f"invalid shard index {index.path}"
                )
            expected = {str(path) for path in index.shard_paths}
            if declared_shards != expected:
                return ReadinessCheck(
                    check="shard_indexes",
                    passed=False,
                    message=f"{index.path} does not map exactly the pinned shards",
                )
            expected_size = sum(file_sizes[path] for path in index.shard_paths)
            # Transformers records tensor bytes in ``metadata.total_size``;
            # the physical safetensors shards are slightly larger because each
            # has a header. Bound that overhead instead of requiring equality.
            maximum_header_bytes = max(1024 * 1024, expected_size // 100)
            if (
                not isinstance(total_size, int)
                or total_size <= 0
                or total_size > expected_size
                or expected_size - total_size > maximum_header_bytes
            ):
                return ReadinessCheck(
                    check="shard_indexes",
                    passed=False,
                    message=f"{index.path} total_size is inconsistent with pinned shard sizes",
                )
        return ReadinessCheck(
            check="shard_indexes", passed=True, message="all shard indexes are structurally valid"
        )

    def _readiness_state(
        self,
        pack: ModelPackSpec,
        hardware: HardwareSnapshot,
        machine: MachineIdentity,
        checks: list[ReadinessCheck],
        memo: dict[str, ModelPackStatus],
        visiting: set[str],
    ) -> ModelPackState:
        resources_ok, resource_message = _hardware_check(pack.hardware, hardware)
        checks.append(
            ReadinessCheck(
                check="hardware_preflight", passed=resources_ok, message=resource_message
            )
        )
        if not resources_ok:
            return ModelPackState.RESOURCE_BLOCKED

        runtime_directory = self.runtime_directory(pack)
        checks.append(
            ReadinessCheck(
                check="isolated_runtime_files",
                passed=runtime_directory is not None,
                message=(
                    f"isolated runtime found at {runtime_directory}"
                    if runtime_directory
                    else "isolated runtime files are missing"
                ),
            )
        )
        if runtime_directory is None:
            return ModelPackState.INSTALLED

        runtime_fingerprint = _runtime_fingerprint(runtime_directory, pack.runtime)

        records = self.records_directory(pack.pack_id)
        runtime = _read_evidence(records / "runtime.json", RuntimeEvidence)
        runtime_matches = (
            isinstance(runtime, RuntimeEvidence)
            and runtime.pack_id == pack.pack_id
            and runtime.model_revision == pack.revision
            and runtime.runtime_revision == pack.runtime.revision
            and runtime.runtime_fingerprint == runtime_fingerprint
            and runtime.machine_id == machine.machine_id
        )
        runtime_ok = bool(runtime_matches and runtime.success)
        checks.append(
            ReadinessCheck(
                check="runtime_probe",
                passed=runtime_ok,
                message=(
                    runtime.message
                    if runtime_matches
                    else (
                        "no runtime probe matches this machine, model revision, and "
                        "verified runtime fingerprint"
                    )
                ),
            )
        )
        if runtime_matches and not runtime.success:
            return ModelPackState.INCOMPATIBLE
        if not runtime_ok:
            return ModelPackState.INSTALLED

        for dependency_id in pack.dependencies:
            dependency = self._inspect(
                self.get(dependency_id), hardware, machine, True, memo, visiting
            )
            dependency_ok = dependency.state == ModelPackState.READY
            checks.append(
                ReadinessCheck(
                    check=f"dependency:{dependency_id}",
                    passed=dependency_ok,
                    message=f"dependency is {dependency.state}",
                )
            )
            if not dependency_ok:
                return ModelPackState.INSTALLED

        smoke = _read_evidence(records / "smoke.json", SmokeTestEvidence)
        smoke_matches = (
            isinstance(smoke, SmokeTestEvidence)
            and smoke.pack_id == pack.pack_id
            and smoke.model_revision == pack.revision
            and smoke.runtime_revision == pack.runtime.revision
            and smoke.runtime_fingerprint == runtime_fingerprint
            and smoke.machine_id == machine.machine_id
        )
        smoke_ok = bool(smoke_matches and smoke.success and smoke.offline)
        checks.append(
            ReadinessCheck(
                check="offline_smoke_test",
                passed=smoke_ok,
                message=(
                    smoke.message
                    if smoke_matches
                    else (
                        "no offline smoke test matches this machine, model revision, and "
                        "verified runtime fingerprint"
                    )
                ),
            )
        )
        if smoke_matches and not smoke.success:
            return ModelPackState.INCOMPATIBLE
        return ModelPackState.READY if smoke_ok else ModelPackState.INSTALLED


def _hardware_check(
    requirement: HardwareRequirement, hardware: HardwareSnapshot
) -> tuple[bool, str]:
    reasons: list[str] = []
    if hardware.total_ram_gb < requirement.min_total_ram_gb:
        reasons.append(f"requires {requirement.min_total_ram_gb:g} GB total RAM")
    if hardware.available_ram_gb < requirement.min_available_ram_gb:
        reasons.append(f"requires {requirement.min_available_ram_gb:g} GB available RAM")
    if hardware.free_disk_gb < requirement.min_free_disk_gb:
        reasons.append(f"requires {requirement.min_free_disk_gb:g} GB free disk")
    if not set(hardware.accelerators).intersection(requirement.accelerator_any_of):
        reasons.append(
            "requires one of these accelerators: " + ", ".join(requirement.accelerator_any_of)
        )
    return (not reasons, "hardware preflight passed" if not reasons else "; ".join(reasons))


def load_model_pack_registry(
    path: Path = Path("config/model_packs.yaml"), *, models_root: Path = Path("models")
) -> ModelPackRegistry:
    """Load and validate a reviewed model-pack manifest."""

    try:
        raw: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
        manifest = ModelPackManifest.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ModelPackError(f"Could not load model-pack registry from {path}: {error}") from error
    return ModelPackRegistry(manifest, models_root)
