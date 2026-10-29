"""Pinned, resumable installation of isolated heavyweight Python runtimes.

Model checkpoints and the Python environment that executes them have separate
lifecycles.  This module deliberately installs only reviewed runtime bundles:
an upstream Git revision is useful provenance, but is never treated as an
installable artifact until a release bundle has an immutable size and digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from whoopy.model_packs.operations import (
    ModelPackOperationError,
    PackInstallLock,
    safe_managed_path,
)


class RuntimePackState(StrEnum):
    """Truthful runtime lifecycle, including intentionally unpublished packs."""

    UNAVAILABLE = "unavailable"
    MISSING = "missing"
    PARTIAL = "partial"
    CORRUPT = "corrupt"
    INSTALLED = "installed"
    INCOMPATIBLE = "incompatible"


class RuntimeBundleSpec(BaseModel):
    """One immutable release archive for one exact desktop target."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str
    filename: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    archive: Literal["tar.gz", "zip"]
    install_mode: Literal["prebuilt", "locked-wheelhouse"] = "prebuilt"
    url: str | None = None

    @field_validator("target")
    @classmethod
    def _target_is_safe(cls, value: str) -> str:
        if (
            not value
            or len(value) > 64
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in value)
        ):
            raise ValueError("runtime target must be a safe platform identifier")
        return value

    @field_validator("url")
    @classmethod
    def _immutable_https_url(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("https://"):
            raise ValueError("runtime bundle URLs must use HTTPS")
        return value


class RuntimePackSpec(BaseModel):
    """Reviewed recipe for a clean isolated runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    display_name: str
    revision: str
    upstream_repository: str
    upstream_revision: str | None
    python_version: str
    license_id: str
    managed_directory: Path
    required_markers: tuple[Path, ...] = Field(min_length=1)
    bundles: tuple[RuntimeBundleSpec, ...] = ()
    unavailable_reason: str | None = None

    @field_validator("upstream_repository")
    @classmethod
    def _https_repository(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("upstream repository must use HTTPS")
        return value

    @field_validator("managed_directory")
    @classmethod
    def _safe_directory(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts or not value.parts:
            raise ValueError("managed_directory must be a safe relative path")
        return value

    @field_validator("required_markers")
    @classmethod
    def _safe_markers(cls, values: tuple[Path, ...]) -> tuple[Path, ...]:
        for value in values:
            if value.is_absolute() or ".." in value.parts or not value.parts:
                raise ValueError("runtime markers must be safe relative paths")
        return values

    @model_validator(mode="after")
    def _availability_is_explicit(self) -> RuntimePackSpec:
        targets = [bundle.target for bundle in self.bundles]
        if len(targets) != len(set(targets)):
            raise ValueError("runtime bundle targets must be unique")
        if not self.bundles and not self.unavailable_reason:
            raise ValueError("an unpublished runtime requires unavailable_reason")
        if self.bundles and not self.upstream_revision:
            raise ValueError("a published runtime requires an exact upstream revision")
        return self


class RuntimePackManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    runtimes: tuple[RuntimePackSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> RuntimePackManifest:
        identifiers = [runtime.runtime_id for runtime in self.runtimes]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("runtime IDs must be unique")
        return self


class RuntimeInstallReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    runtime_id: str
    revision: str
    upstream_revision: str
    target: str
    bundle_sha256: str
    install_mode: Literal["prebuilt", "locked-wheelhouse"] = "prebuilt"
    installed_tree_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installed_at: datetime


class RuntimeInstallIntent(BaseModel):
    """Durable proof that an unreceipted directory is ours to rebuild."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    runtime_id: str
    revision: str
    upstream_revision: str
    target: str
    bundle_sha256: str


class RuntimePackStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: str
    display_name: str
    state: RuntimePackState
    revision: str
    target: str
    directory: Path
    message: str
    remedy: str | None = None


def current_runtime_target() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86-64": "x86_64",
        "aarch64": "arm64",
        "arm64e": "arm64",
    }.get(machine, machine)
    return f"{system}-{architecture}"


def load_runtime_pack_manifest(path: Path) -> RuntimePackManifest:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        return RuntimePackManifest.model_validate(document)
    except (OSError, UnicodeDecodeError, yaml.YAMLError, ValueError) as error:
        raise ModelPackOperationError(f"Invalid runtime-pack manifest {path}: {error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _member_path(name: str) -> Path:
    if "\\" in name:
        raise ModelPackOperationError(f"Unsafe runtime archive member: {name!r}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or ".." in pure.parts or not pure.parts:
        raise ModelPackOperationError(f"Unsafe runtime archive member: {name!r}")
    return Path(*pure.parts)


def _tree_sha256(root: Path) -> str:
    """Hash every installed runtime byte except the self-describing receipt."""

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative_path = path.relative_to(root)
        relative = relative_path.as_posix().encode()
        # Interpreter bytecode is a disposable execution cache. New imports may
        # add entries after installation without changing any reviewed source or
        # dependency bytes, so it cannot be part of the immutable tree identity.
        if "__pycache__" in relative_path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            # POSIX venvs created from a selected exact interpreter use these
            # links even though release archives themselves forbid links. Hash
            # both the literal target and resolved interpreter bytes so a moved,
            # replaced, or retargeted Python invalidates the install receipt.
            if (
                relative_path.parts[:2] != (".venv", "bin")
                or len(relative_path.parts) != 3
                or not relative_path.name.startswith("python")
            ):
                raise ModelPackOperationError(
                    "Managed runtimes may only contain Python launcher links created by venv."
                )
            link_target = os.readlink(path).encode()
            resolved = path.resolve(strict=True)
            if not resolved.is_file():
                raise ModelPackOperationError("Managed runtime Python launcher is invalid.")
            digest.update(b"symlink\0" + relative + b"\0" + link_target + b"\0")
            with resolved.open("rb") as source:
                while chunk := source.read(4 * 1024 * 1024):
                    digest.update(chunk)
        if path.is_dir():
            digest.update(b"directory\0" + relative + b"\0")
        elif path.is_file() and relative not in {
            b".whoopy-runtime.json",
            b".whoopy-installing.json",
        }:
            digest.update(b"file\0" + relative + b"\0")
            with path.open("rb") as source:
                while chunk := source.read(4 * 1024 * 1024):
                    digest.update(chunk)
    return digest.hexdigest()


class ManagedRuntimePacks:
    """Install exact runtime release bundles without copying development venvs."""

    def __init__(self, manifest: RuntimePackManifest, *, models_root: Path) -> None:
        self.manifest = manifest
        self.models_root = Path(os.path.abspath(models_root))
        self._by_id = {item.runtime_id: item for item in manifest.runtimes}

    @classmethod
    def from_path(cls, path: Path, *, models_root: Path) -> ManagedRuntimePacks:
        return cls(load_runtime_pack_manifest(path), models_root=models_root)

    def get(self, runtime_id: str) -> RuntimePackSpec:
        try:
            return self._by_id[runtime_id]
        except KeyError as error:
            raise ModelPackOperationError(f"Unknown runtime pack: {runtime_id}") from error

    def _bundle(self, spec: RuntimePackSpec, target: str) -> RuntimeBundleSpec | None:
        return next((bundle for bundle in spec.bundles if bundle.target == target), None)

    def inspect(self, runtime_id: str, *, target: str | None = None) -> RuntimePackStatus:
        spec = self.get(runtime_id)
        selected_target = target or current_runtime_target()
        destination = safe_managed_path(
            self.models_root,
            self.models_root / spec.managed_directory,
            label="managed runtime directory",
        )
        bundle = self._bundle(spec, selected_target)
        if bundle is None:
            reason = spec.unavailable_reason or (
                f"No reviewed {selected_target} bundle is published for this runtime revision."
            )
            if spec.upstream_revision is None:
                release_remedy = (
                    "Use Kokoro. Release engineering must approve an exact upstream source "
                    "revision before a reproducible Qwen3-TTS runtime can be built; copied "
                    "development environments are never accepted."
                )
            else:
                release_remedy = (
                    f"Use Kokoro. A future Whoopy release must publish a checksum-reviewed "
                    f"{selected_target} bundle built from the exact {spec.python_version} "
                    "dependency lock and complete wheelhouse; copied development virtualenvs "
                    "are never accepted."
                )
            return RuntimePackStatus(
                runtime_id=runtime_id,
                display_name=spec.display_name,
                state=RuntimePackState.UNAVAILABLE,
                revision=spec.revision,
                target=selected_target,
                directory=destination,
                message=reason,
                remedy=release_remedy,
            )
        if not destination.exists():
            partial = self._partial_path(spec, bundle)
            state = RuntimePackState.PARTIAL if partial.exists() else RuntimePackState.MISSING
            remedy = (
                f"Run `whoopy models runtime install {runtime_id}`."
                if bundle.url is not None
                else (
                    f"Provide {bundle.filename}, then run `whoopy models runtime install "
                    f"{runtime_id} --offline-dir /path/to/runtime-bundles --no-network "
                    f"--python /path/to/python-{spec.python_version}`."
                )
            )
            return RuntimePackStatus(
                runtime_id=runtime_id,
                display_name=spec.display_name,
                state=state,
                revision=spec.revision,
                target=selected_target,
                directory=destination,
                message=(
                    "A verified runtime download can be resumed."
                    if state is RuntimePackState.PARTIAL
                    else "The pinned isolated runtime is not installed."
                ),
                remedy=remedy,
            )
        receipt_path = destination / ".whoopy-runtime.json"
        try:
            receipt = RuntimeInstallReceipt.model_validate_json(receipt_path.read_text("utf-8"))
        except (OSError, ValueError):
            return self._corrupt(spec, selected_target, destination, "install receipt is missing")
        expected = (
            receipt.runtime_id == spec.runtime_id
            and receipt.revision == spec.revision
            and receipt.upstream_revision == spec.upstream_revision
            and receipt.target == selected_target
            and receipt.bundle_sha256 == bundle.sha256
            and receipt.install_mode == bundle.install_mode
        )
        if not expected:
            return self._corrupt(spec, selected_target, destination, "install receipt is stale")
        try:
            tree_matches = _tree_sha256(destination) == receipt.installed_tree_sha256
        except (OSError, ModelPackOperationError) as error:
            return self._corrupt(
                spec,
                selected_target,
                destination,
                f"installed tree cannot be verified: {error}",
            )
        if not tree_matches:
            return self._corrupt(
                spec,
                selected_target,
                destination,
                "installed tree digest does not match",
            )
        missing = [
            str(marker) for marker in spec.required_markers if not (destination / marker).is_file()
        ]
        if missing:
            return self._corrupt(
                spec,
                selected_target,
                destination,
                f"required markers are missing: {', '.join(missing)}",
            )
        return RuntimePackStatus(
            runtime_id=runtime_id,
            display_name=spec.display_name,
            state=RuntimePackState.INSTALLED,
            revision=spec.revision,
            target=selected_target,
            directory=destination,
            message="Pinned isolated runtime bundle and install receipt are verified.",
        )

    def _corrupt(
        self, spec: RuntimePackSpec, target: str, directory: Path, detail: str
    ) -> RuntimePackStatus:
        return RuntimePackStatus(
            runtime_id=spec.runtime_id,
            display_name=spec.display_name,
            state=RuntimePackState.CORRUPT,
            revision=spec.revision,
            target=target,
            directory=directory,
            message=f"The managed runtime is corrupt: {detail}.",
            remedy="Remove this managed runtime directory and reinstall its pinned bundle.",
        )

    def list(self, *, target: str | None = None) -> tuple[RuntimePackStatus, ...]:
        return tuple(
            self.inspect(item.runtime_id, target=target) for item in self.manifest.runtimes
        )

    def _download_root(self, spec: RuntimePackSpec) -> Path:
        return safe_managed_path(
            self.models_root,
            self.models_root / "managed" / ".downloads" / "runtimes" / spec.runtime_id,
            label="runtime download directory",
        )

    def _partial_path(self, spec: RuntimePackSpec, bundle: RuntimeBundleSpec) -> Path:
        return self._download_root(spec) / f"{bundle.filename}.part"

    def install(
        self,
        runtime_id: str,
        *,
        offline_directory: Path | None = None,
        allow_network: bool = True,
        target: str | None = None,
        python_executable: Path | None = None,
    ) -> RuntimePackStatus:
        spec = self.get(runtime_id)
        selected_target = target or current_runtime_target()
        bundle = self._bundle(spec, selected_target)
        if bundle is None:
            status = self.inspect(runtime_id, target=selected_target)
            raise ModelPackOperationError(f"{status.message} {status.remedy}")
        with PackInstallLock(self.models_root, f"runtime-{runtime_id}"):
            current = self.inspect(runtime_id, target=selected_target)
            if current.state is RuntimePackState.INSTALLED:
                return current
            if current.state is RuntimePackState.CORRUPT:
                if bundle.install_mode == "locked-wheelhouse" and self._recoverable_install(
                    current.directory, spec, bundle, selected_target
                ):
                    shutil.rmtree(current.directory)
                else:
                    raise ModelPackOperationError(current.message + " " + str(current.remedy))
            archive = self._obtain_bundle(
                spec,
                bundle,
                offline_directory=offline_directory,
                allow_network=allow_network,
            )
            self._install_archive(
                spec,
                bundle,
                archive,
                selected_target,
                python_executable=python_executable,
            )
            installed = self.inspect(runtime_id, target=selected_target)
            if installed.state is not RuntimePackState.INSTALLED:
                raise ModelPackOperationError(installed.message)
            return installed

    def _obtain_bundle(
        self,
        spec: RuntimePackSpec,
        bundle: RuntimeBundleSpec,
        *,
        offline_directory: Path | None,
        allow_network: bool,
    ) -> Path:
        root = self._download_root(spec)
        root.mkdir(parents=True, exist_ok=True)
        complete = safe_managed_path(self.models_root, root / bundle.filename)
        partial = safe_managed_path(self.models_root, self._partial_path(spec, bundle))
        if (
            complete.is_file()
            and complete.stat().st_size == bundle.size_bytes
            and _sha256(complete) == bundle.sha256
        ):
            return complete
        complete.unlink(missing_ok=True)
        offline = offline_directory / bundle.filename if offline_directory is not None else None
        if offline is not None and offline.is_file():
            self._copy_resumable(offline, partial, bundle.size_bytes)
        elif allow_network and bundle.url is not None:
            self._download_resumable(bundle.url, partial, bundle.size_bytes)
        else:
            raise ModelPackOperationError(
                f"Pinned runtime bundle {bundle.filename} is unavailable. Provide it with "
                "--offline-dir or enable its declared immutable HTTPS download."
            )
        if partial.stat().st_size != bundle.size_bytes or _sha256(partial) != bundle.sha256:
            partial.unlink(missing_ok=True)
            raise ModelPackOperationError("Runtime bundle failed its pinned size or SHA-256 check.")
        partial.replace(complete)
        return complete

    @staticmethod
    def _copy_resumable(source: Path, destination: Path, expected: int) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        offset = destination.stat().st_size if destination.exists() else 0
        if offset > expected:
            destination.unlink()
            offset = 0
        with source.open("rb") as reader, destination.open("ab") as writer:
            reader.seek(offset)
            shutil.copyfileobj(reader, writer, length=4 * 1024 * 1024)

    @staticmethod
    def _download_resumable(url: str, destination: Path, expected: int) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        offset = destination.stat().st_size if destination.exists() else 0
        if offset > expected:
            destination.unlink()
            offset = 0
        request = urllib.request.Request(
            url, headers={"Range": f"bytes={offset}-"} if offset else {}
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if offset and getattr(response, "status", None) != 206:
                    destination.unlink(missing_ok=True)
                    offset = 0
                    return ManagedRuntimePacks._download_resumable(url, destination, expected)
                with destination.open("ab") as writer:
                    shutil.copyfileobj(response, writer, length=4 * 1024 * 1024)
        except (OSError, urllib.error.URLError) as error:
            raise ModelPackOperationError(
                f"Runtime download failed and can be resumed: {error}"
            ) from error

    def _install_archive(
        self,
        spec: RuntimePackSpec,
        bundle: RuntimeBundleSpec,
        archive: Path,
        target: str,
        *,
        python_executable: Path | None = None,
    ) -> None:
        destination = safe_managed_path(self.models_root, self.models_root / spec.managed_directory)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = safe_managed_path(
            self.models_root,
            destination.parent / f".{destination.name}.install-{uuid.uuid4().hex}",
        )
        staging.mkdir()
        install_root = staging
        promoted = False
        completed = False
        try:
            if bundle.archive == "tar.gz":
                with tarfile.open(archive, "r:gz") as source:
                    for member in source.getmembers():
                        relative = _member_path(member.name)
                        if member.issym() or member.islnk() or member.isdev():
                            raise ModelPackOperationError(
                                "Runtime archives may not contain links or devices."
                            )
                        if member.isdir():
                            (staging / relative).mkdir(parents=True, exist_ok=True)
                        elif member.isfile():
                            output = staging / relative
                            output.parent.mkdir(parents=True, exist_ok=True)
                            extracted = source.extractfile(member)
                            if extracted is None:
                                raise ModelPackOperationError(f"Could not extract {member.name}")
                            with extracted, output.open("wb") as writer:
                                shutil.copyfileobj(extracted, writer)
                            output.chmod(member.mode & 0o777)
            else:
                with zipfile.ZipFile(archive) as source:
                    for zip_member in source.infolist():
                        relative = _member_path(zip_member.filename)
                        output = staging / relative
                        member_mode = zip_member.external_attr >> 16
                        if stat.S_ISLNK(member_mode):
                            raise ModelPackOperationError(
                                "Runtime archives may not contain symbolic links."
                            )
                        if zip_member.is_dir():
                            output.mkdir(parents=True, exist_ok=True)
                        else:
                            output.parent.mkdir(parents=True, exist_ok=True)
                            with source.open(zip_member) as reader, output.open("wb") as writer:
                                shutil.copyfileobj(reader, writer)
                            if member_mode:
                                output.chmod(member_mode & 0o777)
            if bundle.install_mode == "locked-wheelhouse":
                # A venv is not relocatable on all supported platforms. Promote
                # verified release bytes first so launchers are generated with
                # their permanent path, then remove the whole destination if
                # materialization fails before a receipt can be written.
                if destination.exists():
                    raise ModelPackOperationError(
                        "Refusing to overwrite an existing managed runtime."
                    )
                staging.replace(destination)
                install_root = destination
                promoted = True
                intent = RuntimeInstallIntent(
                    runtime_id=spec.runtime_id,
                    revision=spec.revision,
                    upstream_revision=str(spec.upstream_revision),
                    target=target,
                    bundle_sha256=bundle.sha256,
                )
                (install_root / ".whoopy-installing.json").write_text(
                    intent.model_dump_json(indent=2) + "\n", "utf-8"
                )
                self._materialize_locked_runtime(
                    install_root,
                    spec,
                    target,
                    python_executable=python_executable,
                )
            missing = [
                str(item)
                for item in spec.required_markers
                if item != Path(".whoopy-runtime.json") and not (install_root / item).is_file()
            ]
            if missing:
                raise ModelPackOperationError(
                    f"Runtime archive lacks required markers: {', '.join(missing)}"
                )
            receipt = RuntimeInstallReceipt(
                runtime_id=spec.runtime_id,
                revision=spec.revision,
                upstream_revision=str(spec.upstream_revision),
                target=target,
                bundle_sha256=bundle.sha256,
                install_mode=bundle.install_mode,
                installed_tree_sha256=_tree_sha256(install_root),
                installed_at=datetime.now(UTC),
            )
            (install_root / ".whoopy-runtime.json").write_text(
                receipt.model_dump_json(indent=2) + "\n", "utf-8"
            )
            (install_root / ".whoopy-installing.json").unlink(missing_ok=True)
            if not promoted:
                if destination.exists():
                    raise ModelPackOperationError(
                        "Refusing to overwrite an existing managed runtime."
                    )
                staging.replace(destination)
            completed = True
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if promoted and not completed and destination.exists():
                shutil.rmtree(destination)

    @staticmethod
    def _recoverable_install(
        destination: Path,
        spec: RuntimePackSpec,
        bundle: RuntimeBundleSpec,
        target: str,
    ) -> bool:
        """Recognize only an interrupted install for the exact current release."""

        if (destination / ".whoopy-runtime.json").exists():
            return False
        try:
            intent = RuntimeInstallIntent.model_validate_json(
                (destination / ".whoopy-installing.json").read_text("utf-8")
            )
        except (OSError, ValueError):
            return False
        return (
            intent.runtime_id == spec.runtime_id
            and intent.revision == spec.revision
            and intent.upstream_revision == spec.upstream_revision
            and intent.target == target
            and intent.bundle_sha256 == bundle.sha256
        )

    @staticmethod
    def _materialize_locked_runtime(
        staging: Path,
        spec: RuntimePackSpec,
        target: str,
        *,
        python_executable: Path | None,
    ) -> None:
        """Create a new venv from a bundle's complete hash-locked wheelhouse."""

        from whoopy.model_packs.runtime_builder import RuntimeBundleBuildRecord

        build_root = staging / "_whoopy_bundle"
        descriptor_path = build_root / "build.json"
        lock_path = build_root / "requirements.lock"
        wheelhouse = build_root / "wheelhouse"
        try:
            descriptor = RuntimeBundleBuildRecord.model_validate_json(
                descriptor_path.read_text("utf-8")
            )
        except (OSError, ValueError) as error:
            raise ModelPackOperationError(
                f"Locked runtime bundle has no valid build descriptor: {error}"
            ) from error
        expected = (
            descriptor.runtime_id == spec.runtime_id
            and descriptor.revision == spec.revision
            and descriptor.upstream_revision == spec.upstream_revision
            and descriptor.python_version == spec.python_version
            and descriptor.target == target
        )
        if not expected:
            raise ModelPackOperationError(
                "Locked runtime build descriptor does not match the reviewed registry entry."
            )
        if not lock_path.is_file() or not wheelhouse.is_dir():
            raise ModelPackOperationError(
                "Locked runtime bundle is missing its requirements lock or wheelhouse."
            )
        if hashlib.sha256(lock_path.read_bytes()).hexdigest() != descriptor.requirements_sha256:
            raise ModelPackOperationError("Locked runtime requirements digest does not match.")
        actual_wheels = {
            path.name: _sha256(path)
            for path in sorted(wheelhouse.iterdir())
            if path.is_file() and path.suffix == ".whl"
        }
        if actual_wheels != descriptor.wheelhouse:
            raise ModelPackOperationError(
                "Locked runtime wheelhouse digest inventory does not match."
            )

        requested = str(python_executable) if python_executable is not None else None
        if requested is None and platform.python_version() == spec.python_version:
            requested = sys.executable
        if requested is None:
            major_minor = ".".join(spec.python_version.split(".")[:2])
            requested = shutil.which(f"python{major_minor}")
        if requested is None:
            raise ModelPackOperationError(
                f"Python {spec.python_version} is required. Install that exact interpreter or "
                "pass `--python /path/to/python` to the runtime installer."
            )

        def run(command: list[str], label: str, *, cwd: Path | None = None) -> str:
            completed = subprocess.run(
                command, cwd=cwd, check=False, capture_output=True, text=True
            )
            if completed.returncode:
                detail = (completed.stderr or completed.stdout).strip()[-2000:]
                raise ModelPackOperationError(f"{label} failed: {detail}")
            return completed.stdout.strip()

        version_payload = run(
            [
                requested,
                "-I",
                "-c",
                "import json,platform; print(json.dumps({'version': platform.python_version()}))",
            ],
            "Runtime Python probe",
        )
        try:
            actual_version = json.loads(version_payload)["version"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise ModelPackOperationError(
                "Runtime Python probe returned invalid output."
            ) from error
        if actual_version != spec.python_version:
            raise ModelPackOperationError(
                f"Runtime requires Python {spec.python_version}; selected interpreter is "
                f"{actual_version}."
            )
        venv = staging / ".venv"
        run([requested, "-m", "venv", str(venv)], "Creating isolated runtime")
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        common = [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "install",
            "--no-index",
            "--find-links",
            str(wheelhouse),
        ]
        run(
            [*common, "--require-hashes", "-r", str(lock_path)],
            "Installing hash-locked runtime dependencies",
        )
        run(
            [*common, "--no-deps", "--no-build-isolation", str(staging)],
            "Installing pinned runtime source",
        )
        # Fish and some other reviewed research runtimes intentionally execute
        # directly from their pinned source tree (their upstream wheel metadata
        # does not include every namespace package). Mirror the production
        # worker's explicit source-root insertion while retaining Python's
        # isolated mode so user site-packages cannot satisfy the probe.
        imports = "; ".join(
            [f"import sys; sys.path.insert(0, {str(staging)!r})"]
            + [f"import {module}" for module in descriptor.import_modules]
        )
        run([str(python), "-I", "-c", imports], "Runtime import probe", cwd=staging)
        shutil.rmtree(build_root)
