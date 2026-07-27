"""Verified, platform-aware installation of Whoopy's native model stack.

The artifact manager intentionally does not import or load an ML runtime. It
turns a versioned lock file into verified files on disk so later adapters can
depend on stable paths rather than mutable download URLs.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from contextlib import closing
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


class ArtifactError(RuntimeError):
    """Raised when locked artifacts cannot be resolved, verified, or installed."""


class ArchiveFormat(StrEnum):
    """Archive formats the installer can extract without external tools."""

    NONE = "none"
    TAR_GZ = "tar.gz"
    TAR_BZ2 = "tar.bz2"
    ZIP = "zip"


class ArtifactSpec(BaseModel):
    """One immutable downloadable file in the artifact lock."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    component: str
    display_name: str
    version: str
    kind: Literal["model", "runtime", "tts_model", "python_wheel"]
    license_id: str
    source_url: str
    filename: str
    size_bytes: int = Field(gt=0)
    sha256: str
    operating_systems: list[str] = Field(min_length=1)
    architectures: list[str] = Field(min_length=1)
    archive: ArchiveFormat = ArchiveFormat.NONE

    @field_validator("artifact_id", "component")
    @classmethod
    def _validate_identifier(cls, value: str) -> str:
        allowed = "abcdefghijklmnopqrstuvwxyz0123456789_-"
        if not value or any(character not in allowed for character in value):
            raise ValueError(
                "must contain only lowercase letters, numbers, underscores, and hyphens"
            )
        return value

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, value: str) -> str:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ValueError("must be one safe filename without path components")
        return value

    @field_validator("sha256")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = value.lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("must be a 64-character hexadecimal SHA-256 digest")
        return normalized

    @field_validator("source_url")
    @classmethod
    def _require_https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("must use HTTPS")
        return value

    def supports(self, target: TargetPlatform) -> bool:
        """Return whether this locked file applies to the current platform."""

        return (
            "all" in self.operating_systems or target.operating_system in self.operating_systems
        ) and ("all" in self.architectures or target.architecture in self.architectures)


class ArtifactProfile(BaseModel):
    """Logical components required by one user-facing hardware profile."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    components: list[str] = Field(min_length=1)


class ArtifactLock(BaseModel):
    """Complete, validated contents of ``config/artifacts.yaml``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    artifacts: list[ArtifactSpec] = Field(min_length=1)
    profiles: dict[str, ArtifactProfile] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_registry(self) -> ArtifactLock:
        artifact_ids = [artifact.artifact_id for artifact in self.artifacts]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("artifact IDs must be unique")

        known_components = {artifact.component for artifact in self.artifacts}
        for profile_name, profile in self.profiles.items():
            unknown = sorted(set(profile.components) - known_components)
            if unknown:
                raise ValueError(
                    f"profile {profile_name} references unknown components: {', '.join(unknown)}"
                )
            if len(set(profile.components)) != len(profile.components):
                raise ValueError(f"profile {profile_name} contains duplicate components")
        return self

    def resolve(self, profile_name: str, target: TargetPlatform) -> list[ArtifactSpec]:
        """Resolve every logical profile component to exactly one platform file."""

        profile = self.profiles.get(profile_name)
        if profile is None:
            raise ArtifactError(f"No locked artifact plan exists for profile {profile_name!r}.")

        resolved: list[ArtifactSpec] = []
        for component in profile.components:
            matches = [
                artifact
                for artifact in self.artifacts
                if artifact.component == component and artifact.supports(target)
            ]
            if not matches:
                raise ArtifactError(
                    f"No locked {component!r} artifact supports "
                    f"{target.operating_system} {target.architecture}."
                )
            if len(matches) > 1:
                match_ids = ", ".join(artifact.artifact_id for artifact in matches)
                raise ArtifactError(
                    f"Artifact component {component!r} is ambiguous for "
                    f"{target.operating_system} {target.architecture}: {match_ids}."
                )
            resolved.append(matches[0])
        filenames = [artifact.filename for artifact in resolved]
        if len(set(filenames)) != len(filenames):
            raise ArtifactError(
                f"Profile {profile_name!r} resolves two components to the same filename."
            )
        return resolved


class TargetPlatform(BaseModel):
    """Normalized operating system and CPU architecture used for resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operating_system: str
    architecture: str

    @classmethod
    def current(cls) -> TargetPlatform:
        operating_system = platform.system().lower()
        architecture = platform.machine().lower()
        if architecture in {"amd64", "x64"}:
            architecture = "x86_64"
        elif architecture == "aarch64":
            architecture = "arm64"
        return cls(operating_system=operating_system, architecture=architecture)


class ArtifactState(StrEnum):
    """Local verification state without importing or loading the artifact."""

    MISSING = "missing"
    INSTALLED = "installed"
    CORRUPT = "corrupt"


class ArtifactStatus(BaseModel):
    """Inspectable state for one locked artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    component: str
    display_name: str
    version: str
    license_id: str
    state: ArtifactState
    download_path: Path
    installed_path: Path | None
    size_bytes: int
    message: str


class InstallReport(BaseModel):
    """Machine-readable result of one profile installation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: str
    target: TargetPlatform
    installed: list[str]
    reused: list[str]
    artifacts: list[ArtifactStatus]


class _VerificationRecord(BaseModel):
    """Small trust record that avoids hashing multi-gigabyte files on every list."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    version: str
    sha256: str
    size_bytes: int
    file_mtime_ns: int
    installed_relative_path: str | None
    installed_tree_sha256: str | None = None


def load_artifact_lock(path: Path) -> ArtifactLock:
    """Load the artifact lock and turn syntax/schema failures into one error type."""

    try:
        document: Any = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise ArtifactError(f"Could not read artifact lock {path}: {error}") from error
    try:
        return ArtifactLock.model_validate(document)
    except ValidationError as error:
        raise ArtifactError(f"Invalid artifact lock {path}:\n{error}") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_sha256(root: Path) -> str:
    """Hash extracted paths, file bytes, and symlink targets deterministically."""

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
            raise ArtifactError(f"Unsupported extracted filesystem entry: {path}")
    return digest.hexdigest()


def _write_json_atomic(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(document, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        temporary_path.replace(path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


class ArtifactStore:
    """Own verified downloads, extracted runtimes, and their trust records."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.downloads = root / "downloads"
        self.installed = root / "installed"
        self.records = root / "records"
        self.quarantine = root / "quarantine"

    def download_path(self, artifact: ArtifactSpec) -> Path:
        return self.downloads / artifact.filename

    def installed_path(self, artifact: ArtifactSpec) -> Path | None:
        if artifact.archive is ArchiveFormat.NONE:
            return self.download_path(artifact)
        return self.installed / artifact.artifact_id

    def require(self, artifact: ArtifactSpec) -> Path:
        """Return a fully reverified path or refuse before a runtime can load it."""

        status = self.inspect(artifact, verify_digest=True)
        if status.state is not ArtifactState.INSTALLED:
            raise ArtifactError(
                f"Artifact {artifact.artifact_id} is not safe to load: {status.message}"
            )
        installed_path = self.installed_path(artifact)
        if installed_path is None:
            raise ArtifactError(f"Artifact {artifact.artifact_id} has no installed path.")
        return installed_path

    def _record_path(self, artifact: ArtifactSpec) -> Path:
        return self.records / f"{artifact.artifact_id}.json"

    def _read_record(self, artifact: ArtifactSpec) -> _VerificationRecord | None:
        path = self._record_path(artifact)
        if not path.is_file():
            return None
        try:
            return _VerificationRecord.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None

    def inspect(self, artifact: ArtifactSpec, *, verify_digest: bool = False) -> ArtifactStatus:
        """Inspect file metadata, optionally rehashing the complete download."""

        download_path = self.download_path(artifact)
        installed_path = self.installed_path(artifact)
        if not download_path.is_file():
            return self._status(
                artifact,
                ArtifactState.MISSING,
                "The verified download is not installed.",
            )

        try:
            file_stat = download_path.stat()
        except OSError as error:
            return self._status(artifact, ArtifactState.CORRUPT, f"Cannot stat download: {error}")
        if file_stat.st_size != artifact.size_bytes:
            return self._status(
                artifact,
                ArtifactState.CORRUPT,
                f"Expected {artifact.size_bytes} bytes but found {file_stat.st_size}.",
            )

        record = self._read_record(artifact)
        record_matches = (
            record is not None
            and record.artifact_id == artifact.artifact_id
            and record.version == artifact.version
            and record.sha256 == artifact.sha256
            and record.size_bytes == file_stat.st_size
            and record.file_mtime_ns == file_stat.st_mtime_ns
        )
        if (verify_digest or not record_matches) and _sha256(download_path) != artifact.sha256:
            return self._status(
                artifact,
                ArtifactState.CORRUPT,
                "The download SHA-256 does not match the lock.",
            )

        if artifact.archive is not ArchiveFormat.NONE and (
            installed_path is None
            or not installed_path.is_dir()
            or not record_matches
            or record is None
            or record.installed_tree_sha256 is None
        ):
            return self._status(
                artifact,
                ArtifactState.CORRUPT,
                "The archive is verified but its extracted installation is incomplete.",
            )
        if (
            verify_digest
            and artifact.archive is not ArchiveFormat.NONE
            and installed_path is not None
            and record is not None
            and _tree_sha256(installed_path) != record.installed_tree_sha256
        ):
            return self._status(
                artifact,
                ArtifactState.CORRUPT,
                "The extracted installation digest does not match its verification record.",
            )

        return self._status(
            artifact,
            ArtifactState.INSTALLED,
            "Installed and matched to the immutable lock.",
        )

    def _status(
        self,
        artifact: ArtifactSpec,
        state: ArtifactState,
        message: str,
    ) -> ArtifactStatus:
        return ArtifactStatus(
            artifact_id=artifact.artifact_id,
            component=artifact.component,
            display_name=artifact.display_name,
            version=artifact.version,
            license_id=artifact.license_id,
            state=state,
            download_path=self.download_path(artifact),
            installed_path=self.installed_path(artifact),
            size_bytes=artifact.size_bytes,
            message=message,
        )

    def record_verified(self, artifact: ArtifactSpec) -> None:
        download_path = self.download_path(artifact)
        file_stat = download_path.stat()
        installed_path = self.installed_path(artifact)
        installed_relative_path = (
            str(installed_path.relative_to(self.root)) if installed_path is not None else None
        )
        installed_tree_sha256 = (
            _tree_sha256(installed_path)
            if artifact.archive is not ArchiveFormat.NONE
            and installed_path is not None
            and installed_path.is_dir()
            else None
        )
        record = _VerificationRecord(
            artifact_id=artifact.artifact_id,
            version=artifact.version,
            sha256=artifact.sha256,
            size_bytes=file_stat.st_size,
            file_mtime_ns=file_stat.st_mtime_ns,
            installed_relative_path=installed_relative_path,
            installed_tree_sha256=installed_tree_sha256,
        )
        _write_json_atomic(
            self._record_path(artifact),
            record.model_dump(mode="json"),
        )

    def quarantine_file(self, path: Path, artifact: ArtifactSpec) -> Path:
        """Move an untrusted managed file aside instead of silently deleting it."""

        self.quarantine.mkdir(parents=True, exist_ok=True)
        candidate = self.quarantine / f"{artifact.artifact_id}-{path.name}"
        suffix = 1
        while candidate.exists():
            candidate = self.quarantine / f"{artifact.artifact_id}-{suffix}-{path.name}"
            suffix += 1
        path.replace(candidate)
        return candidate


def _copy_offline(source: Path, destination: Path) -> None:
    """Copy an offline source so corruption cannot damage the original cache."""

    shutil.copyfile(source, destination)


def _find_offline_artifact(root: Path, filename: str) -> Path | None:
    if not root.is_dir():
        return None
    matches = [
        path
        for path in root.rglob(filename)
        if path.is_file() and not path.is_symlink() and path.name == filename
    ]
    if not matches:
        return None
    if len(matches) > 1:
        raise ArtifactError(
            f"Offline directory {root} contains more than one file named {filename!r}."
        )
    return matches[0]


def _download_with_resume(
    artifact: ArtifactSpec,
    partial_path: Path,
    *,
    timeout_seconds: float,
) -> None:
    """Download to a partial file and continue it when the server supports Range."""

    partial_size = partial_path.stat().st_size if partial_path.exists() else 0
    if partial_size > artifact.size_bytes:
        raise ArtifactError(
            f"Partial download for {artifact.artifact_id} is larger than the locked artifact."
        )

    request = urllib.request.Request(artifact.source_url)
    if partial_size:
        request.add_header("Range", f"bytes={partial_size}-")
    try:
        response = urllib.request.urlopen(request, timeout=timeout_seconds)
    except (OSError, urllib.error.URLError) as error:
        raise ArtifactError(f"Could not download {artifact.display_name}: {error}") from error

    with closing(response):
        status = getattr(response, "status", 200)
        append = partial_size > 0 and status == 206
        mode = "ab" if append else "wb"
        with partial_path.open(mode) as destination:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
            destination.flush()
            os.fsync(destination.fileno())


def _safe_archive_member(name: str) -> bool:
    member_path = PurePosixPath(name.replace("\\", "/"))
    return (
        not member_path.is_absolute()
        and ".." not in member_path.parts
        and member_path.parts not in {(), (".",)}
    )


def _link_stays_inside_archive(member: tarfile.TarInfo) -> bool:
    """Allow required internal links while rejecting links that can escape."""

    link_path = PurePosixPath(member.linkname.replace("\\", "/"))
    if link_path.is_absolute():
        return False
    if member.islnk():
        combined_parts = link_path.parts
    else:
        member_parent = PurePosixPath(member.name.replace("\\", "/")).parent
        combined_parts = (*member_parent.parts, *link_path.parts)

    depth = 0
    for part in combined_parts:
        if part in {"", "."}:
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return False
        else:
            depth += 1
    return True


def _extract_tar_safely(
    archive_path: Path,
    destination: Path,
    mode: Literal["r:gz", "r:bz2"],
) -> None:
    with tarfile.open(archive_path, mode) as archive:
        members = archive.getmembers()
        for member in members:
            if not _safe_archive_member(member.name):
                raise ArtifactError(f"Unsafe archive path: {member.name!r}")
            if member.isdev():
                raise ArtifactError(f"Unsupported archive device: {member.name!r}")
            if (member.issym() or member.islnk()) and not _link_stays_inside_archive(member):
                raise ArtifactError(f"Unsafe archive link: {member.name!r} -> {member.linkname!r}")
        archive.extractall(destination, members=members)


def _extract_zip_safely(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            if not _safe_archive_member(member.filename):
                raise ArtifactError(f"Unsafe archive path: {member.filename!r}")
            member_mode = member.external_attr >> 16
            if stat.S_ISLNK(member_mode):
                raise ArtifactError(f"Unsupported archive link: {member.filename!r}")
        archive.extractall(destination)


def _extract_atomic(artifact: ArtifactSpec, archive_path: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{artifact.artifact_id}.", dir=destination.parent))
    try:
        if artifact.archive is ArchiveFormat.TAR_GZ:
            _extract_tar_safely(archive_path, temporary, "r:gz")
        elif artifact.archive is ArchiveFormat.TAR_BZ2:
            _extract_tar_safely(archive_path, temporary, "r:bz2")
        elif artifact.archive is ArchiveFormat.ZIP:
            _extract_zip_safely(archive_path, temporary)
        else:
            raise ArtifactError(f"Artifact {artifact.artifact_id} is not an archive.")

        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


class ArtifactInstaller:
    """Install a resolved artifact plan without importing model code."""

    def __init__(
        self,
        store: ArtifactStore,
        *,
        timeout_seconds: float = 30.0,
        status_callback: Callable[[str], None] | None = None,
    ) -> None:
        self.store = store
        self.timeout_seconds = timeout_seconds
        self.status_callback = status_callback or (lambda _message: None)

    def install_profile(
        self,
        profile_name: str,
        artifacts: Iterable[ArtifactSpec],
        target: TargetPlatform,
        *,
        offline_directory: Path | None = None,
        allow_network: bool = True,
    ) -> InstallReport:
        installed: list[str] = []
        reused: list[str] = []
        resolved = list(artifacts)
        for artifact in resolved:
            state = self.store.inspect(artifact)
            if state.state is ArtifactState.INSTALLED:
                self.status_callback(f"Reusing {artifact.display_name}")
                reused.append(artifact.artifact_id)
                continue
            self._install_one(
                artifact,
                offline_directory=offline_directory,
                allow_network=allow_network,
            )
            installed.append(artifact.artifact_id)

        statuses = [self.store.inspect(artifact) for artifact in resolved]
        failures = [status for status in statuses if status.state is not ArtifactState.INSTALLED]
        if failures:
            failed_ids = ", ".join(status.artifact_id for status in failures)
            raise ArtifactError(f"Installation did not produce valid artifacts: {failed_ids}")
        return InstallReport(
            profile=profile_name,
            target=target,
            installed=installed,
            reused=reused,
            artifacts=statuses,
        )

    def _install_one(
        self,
        artifact: ArtifactSpec,
        *,
        offline_directory: Path | None,
        allow_network: bool,
    ) -> None:
        self.store.downloads.mkdir(parents=True, exist_ok=True)
        destination = self.store.download_path(artifact)
        partial = destination.with_name(f"{destination.name}.part")

        if destination.exists():
            state = self.store.inspect(artifact, verify_digest=True)
            if state.state is ArtifactState.CORRUPT:
                quarantined = self.store.quarantine_file(destination, artifact)
                self.status_callback(f"Quarantined invalid file at {quarantined}")

        if not destination.exists():
            offline_source = (
                _find_offline_artifact(offline_directory, artifact.filename)
                if offline_directory is not None
                else None
            )
            if offline_source is not None:
                self.status_callback(f"Importing {artifact.display_name} from offline storage")
                partial.unlink(missing_ok=True)
                _copy_offline(offline_source, partial)
            elif allow_network:
                self.status_callback(f"Downloading {artifact.display_name}")
                if not partial.exists() or partial.stat().st_size != artifact.size_bytes:
                    _download_with_resume(
                        artifact,
                        partial,
                        timeout_seconds=self.timeout_seconds,
                    )
            else:
                raise ArtifactError(
                    f"{artifact.display_name} is missing from {offline_directory}; "
                    "network access is disabled."
                )

            if partial.stat().st_size != artifact.size_bytes:
                raise ArtifactError(
                    f"{artifact.display_name} has {partial.stat().st_size} bytes; "
                    f"the lock requires {artifact.size_bytes}."
                )
            if _sha256(partial) != artifact.sha256:
                rejected = self.store.quarantine_file(partial, artifact)
                raise ArtifactError(
                    f"{artifact.display_name} failed SHA-256 verification. "
                    f"The rejected file is at {rejected}."
                )
            partial.replace(destination)

        if artifact.archive is not ArchiveFormat.NONE:
            installed_path = self.store.installed_path(artifact)
            if installed_path is None:
                raise ArtifactError(f"No install path exists for archive {artifact.artifact_id}.")
            self.status_callback(f"Extracting {artifact.display_name}")
            _extract_atomic(artifact, destination, installed_path)

        self.store.record_verified(artifact)
        verified = self.store.inspect(artifact)
        if verified.state is not ArtifactState.INSTALLED:
            raise ArtifactError(f"Could not verify {artifact.display_name}: {verified.message}")


def inspect_profile(
    artifact_lock: ArtifactLock,
    profile_name: str,
    target: TargetPlatform,
    store: ArtifactStore,
    *,
    verify_digest: bool = False,
) -> list[ArtifactStatus]:
    """Resolve and inspect one profile without network access or model loading."""

    return [
        store.inspect(artifact, verify_digest=verify_digest)
        for artifact in artifact_lock.resolve(profile_name, target)
    ]
