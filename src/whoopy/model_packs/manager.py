"""Safe lifecycle facade shared by the model-pack CLI and local web API."""

from __future__ import annotations

import hashlib
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from whoopy.audio.models import PcmAudio
from whoopy.audio.quality import pcm_integrity_error
from whoopy.model_packs.operations import (
    HeavyweightModelSlot,
    HeavyweightModelSlotUnavailable,
    HeavyweightSlotStatus,
    ManagedPackTrash,
    ModelPackOperationError,
    PackInstallLock,
    PackProgressStore,
    PackTransferProgress,
    TransferState,
    safe_managed_path,
)
from whoopy.model_packs.registry import (
    DigestSpec,
    MachineIdentity,
    ModelPackRegistry,
    ModelPackSpec,
    ModelPackState,
    ModelPackStatus,
    RuntimeEvidence,
    SmokeTestEvidence,
    load_model_pack_registry,
)
from whoopy.ports.errors import FatalAdapterError, TransientAdapterError


class SmokeResult(BaseModel):
    """Audio evidence returned by a pack-specific, networking-disabled probe."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pcm_s16le: bytes
    sample_rate: int
    message: str
    validated_dependencies: tuple[str, ...] = ()


class SmokeRunner(Protocol):
    """Runtime-specific offline probe injected by an adapter integration."""

    def __call__(self, pack: ModelPackSpec, selected_directory: Path) -> SmokeResult: ...


class PackOperationReport(BaseModel):
    """Stable machine-readable response for one lifecycle command."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["install", "verify", "smoke-test", "unload", "remove", "restore"]
    pack_id: str | None
    state: ModelPackState | None
    message: str
    status: ModelPackStatus | None = None
    progress: PackTransferProgress | None = None
    receipt_id: str | None = None


class PackListReport(BaseModel):
    """Registry states plus recoverable-trash receipts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    packs: tuple[ModelPackStatus, ...]
    trash_receipts: tuple[str, ...]


def _digest(path: Path, spec: DigestSpec) -> str:
    digest = hashlib.sha256() if spec.algorithm == "sha256" else hashlib.sha1()
    if spec.algorithm == "git_sha1":
        digest.update(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _file_is_verified(path: Path, *, size_bytes: int, digest: DigestSpec) -> bool:
    return (
        path.is_file()
        and path.stat().st_size == size_bytes
        and _digest(path, digest) == digest.value
    )


def _atomic_evidence(path: Path, document: BaseModel, *, safety_root: Path) -> None:
    path = safe_managed_path(safety_root, path, label="model-pack evidence")
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_managed_path(safety_root, path.parent, label="model-pack evidence parent")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            destination.write(document.model_dump_json(indent=2))
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        safe_managed_path(safety_root, path, label="model-pack evidence")
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _deterministic_smoke_failure(error: BaseException) -> bool:
    """Return whether a failure proves this exact runtime cannot be used.

    Busy slots, timeouts, cancellation, and other transient failures say
    nothing about compatibility and must never poison durable READY evidence.
    Only errors deliberately classified as fatal, or local contract/setup
    violations with deterministic inputs, are persisted as incompatible.
    """

    if isinstance(error, (TransientAdapterError, HeavyweightModelSlotUnavailable, TimeoutError)):
        return False
    return isinstance(error, (FatalAdapterError, ModelPackOperationError, ValueError, TypeError))


class ManagedModelPacks:
    """Compose registry inspection with safe, durable local mutations."""

    def __init__(
        self,
        registry: ModelPackRegistry,
        *,
        smoke_runner: SmokeRunner | None = None,
    ) -> None:
        self.registry = registry
        self.models_root = registry.models_root
        self.smoke_runner = smoke_runner
        self.trash = ManagedPackTrash(self.models_root / "managed" / "model-packs")

    @classmethod
    def from_paths(cls, registry_path: Path, models_root: Path) -> ManagedModelPacks:
        registry = load_model_pack_registry(registry_path, models_root=models_root)
        # Keep direct construction injectable for unit tests while ensuring the
        # real CLI and local web API never silently use a placeholder probe.
        from whoopy.model_packs.smoke import OfflineModelPackSmokeRunner

        return cls(
            registry,
            smoke_runner=OfflineModelPackSmokeRunner(
                registry,
                references_path=registry_path.parent / "voice_references.yaml",
            ),
        )

    def list(self) -> PackListReport:
        return PackListReport(
            packs=tuple(self.registry.list_statuses()),
            trash_receipts=tuple(entry.trash_id for entry in self.trash.list()),
        )

    def verify(self, pack_id: str) -> PackOperationReport:
        status = self.registry.inspect(pack_id, verify_digests=True)
        runtime_check = next(
            (check for check in status.checks if check.check == "isolated_runtime_files"),
            None,
        )
        message = "Every managed file was installed only after pinned digest verification."
        if runtime_check is not None and not runtime_check.passed:
            message += (
                " Checkpoint bytes are installed, but the matching isolated runtime "
                "is still required before this pack can become ready."
            )
        return PackOperationReport(
            action="verify",
            pack_id=pack_id,
            state=status.state,
            status=status,
            message=f"Pinned files inspected; pack is {status.state}.",
        )

    def install(
        self,
        pack_id: str,
        *,
        offline_directory: Path | None,
        allow_network: bool,
    ) -> PackOperationReport:
        """Serialize the complete install transaction for one immutable pack."""

        with PackInstallLock(self.models_root, pack_id):
            return self._install_locked(
                pack_id,
                offline_directory=offline_directory,
                allow_network=allow_network,
            )

    def _install_locked(
        self,
        pack_id: str,
        *,
        offline_directory: Path | None,
        allow_network: bool,
    ) -> PackOperationReport:
        """Install only verified files into the pack's declared managed path.

        Bytes are first written below ``.downloads``. Existing experimental
        caches are read-only candidates and are never moved or overwritten.
        """

        pack = self.registry.get(pack_id)
        destination = safe_managed_path(
            self.models_root,
            self.models_root / pack.managed_directory,
            label="managed pack destination",
        )
        current = self.registry.inspect(pack_id, verify_digests=True)
        if current.selected_directory.resolve(strict=False) != destination.resolve(
            strict=False
        ) and current.state not in {
            ModelPackState.MISSING,
            ModelPackState.PARTIAL,
            ModelPackState.CORRUPT,
        }:
            return PackOperationReport(
                action="install",
                pack_id=pack_id,
                state=current.state,
                status=current,
                message=(
                    "A fully pinned existing installation was reused in place; "
                    "Whoopy did not move or duplicate its model bytes."
                ),
            )
        for dependency in pack.dependencies:
            dependency_status = self.registry.inspect(dependency)
            if dependency_status.state in {
                ModelPackState.MISSING,
                ModelPackState.PARTIAL,
                ModelPackState.CORRUPT,
            }:
                self.install(
                    dependency,
                    offline_directory=offline_directory,
                    allow_network=allow_network,
                )

        records = self.registry.records_directory(pack_id)
        safe_managed_path(self.models_root, records, label="model-pack records")
        progress_store = PackProgressStore(records, safety_root=self.models_root)
        expected_sizes = {file.path.as_posix(): file.size_bytes for file in pack.files}
        progress = progress_store.load()
        if (
            progress is None
            or progress.pack_id != pack_id
            or progress.revision != pack.revision
            or {item.artifact_id: item.bytes_total for item in progress.artifacts} != expected_sizes
            or progress.state
            in {TransferState.COMPLETE, TransferState.FAILED, TransferState.CANCELLED}
        ):
            progress = progress_store.start(
                pack_id=pack_id,
                revision=pack.revision,
                artifacts=expected_sizes,
            )
        operation_id = progress.operation_id

        revision_key = hashlib.sha256(pack.revision.encode("utf-8")).hexdigest()
        staging = safe_managed_path(
            self.models_root,
            self.models_root / "managed" / ".downloads" / "model-packs" / pack_id / revision_key,
            label="model-pack staging directory",
        )
        try:
            for file in pack.files:
                artifact_id = file.path.as_posix()
                installed = safe_managed_path(
                    self.models_root,
                    destination / file.path,
                    label="managed model file",
                )
                if installed.exists() and not _file_is_verified(
                    installed, size_bytes=file.size_bytes, digest=file.digest
                ):
                    raise ModelPackOperationError(
                        f"Refusing to overwrite unverified managed file {installed}. "
                        "Move the pack to managed trash first."
                    )
                if _file_is_verified(installed, size_bytes=file.size_bytes, digest=file.digest):
                    progress = progress_store.update_artifact(
                        operation_id,
                        artifact_id,
                        file.size_bytes,
                    )
                    continue

                complete = safe_managed_path(
                    self.models_root,
                    staging / file.path,
                    label="verified staging file",
                )
                partial_path = safe_managed_path(
                    self.models_root,
                    complete.with_name(f"{complete.name}.part"),
                    label="partial staging file",
                )
                progress_callback = partial(
                    progress_store.update_artifact, operation_id, artifact_id
                )
                complete.parent.mkdir(parents=True, exist_ok=True)
                safe_managed_path(
                    self.models_root,
                    complete.parent,
                    label="model-pack staging parent",
                )
                if not _file_is_verified(complete, size_bytes=file.size_bytes, digest=file.digest):
                    complete.unlink(missing_ok=True)
                    if _file_is_verified(
                        partial_path, size_bytes=file.size_bytes, digest=file.digest
                    ):
                        safe_managed_path(
                            self.models_root, partial_path, label="partial staging file"
                        )
                        safe_managed_path(self.models_root, complete, label="verified staging file")
                        partial_path.replace(complete)
                    else:
                        source = self._offline_source(offline_directory, file.path)
                        if source is not None:
                            self._copy_with_progress(
                                source,
                                partial_path,
                                expected_size=file.size_bytes,
                                progress=progress_callback,
                                safety_root=self.models_root,
                            )
                        elif allow_network:
                            if pack.source_repository.startswith("https://github.com/"):
                                raise ModelPackOperationError(
                                    f"{pack_id} uses a release archive rather than individual "
                                    "remote files. Reuse the legacy verified installation or "
                                    "provide an extracted offline directory."
                                )
                            self._download_with_progress(
                                pack,
                                file.path,
                                partial_path,
                                expected_size=file.size_bytes,
                                progress=progress_callback,
                                safety_root=self.models_root,
                            )
                        else:
                            raise ModelPackOperationError(
                                f"{artifact_id} is unavailable offline and network access "
                                "is disabled."
                            )
                        if not _file_is_verified(
                            partial_path, size_bytes=file.size_bytes, digest=file.digest
                        ):
                            raise ModelPackOperationError(
                                f"Downloaded bytes for {artifact_id} failed size or digest "
                                "verification."
                            )
                        safe_managed_path(
                            self.models_root, partial_path, label="partial staging file"
                        )
                        safe_managed_path(self.models_root, complete, label="verified staging file")
                        partial_path.replace(complete)
                safe_managed_path(self.models_root, installed.parent, label="managed model parent")
                installed.parent.mkdir(parents=True, exist_ok=True)
                safe_managed_path(self.models_root, installed.parent, label="managed model parent")
                safe_managed_path(self.models_root, complete, label="verified staging file")
                safe_managed_path(self.models_root, installed, label="managed model file")
                complete.replace(installed)
                progress = progress_store.update_artifact(
                    operation_id, artifact_id, file.size_bytes
                )
            progress = progress_store.set_state(operation_id, TransferState.COMPLETE)
        except BaseException as error:
            progress = progress_store.set_state(
                operation_id,
                TransferState.FAILED,
                message=f"{type(error).__name__}: {error}"[:2_000],
            )
            raise

        status = self.registry.inspect(pack_id, verify_digests=True)
        return PackOperationReport(
            action="install",
            pack_id=pack_id,
            state=status.state,
            status=status,
            progress=progress,
            message="All pinned model-pack files were installed and verified.",
        )

    def smoke_test(self, pack_id: str) -> PackOperationReport:
        """Run an injected adapter probe and bind evidence to this machine/revision."""

        pack = self.registry.get(pack_id)
        status = self.registry.inspect(pack_id)
        if status.state in {
            ModelPackState.MISSING,
            ModelPackState.PARTIAL,
            ModelPackState.CORRUPT,
            ModelPackState.RESOURCE_BLOCKED,
        }:
            raise ModelPackOperationError(f"Cannot smoke-test pack in state {status.state}.")
        if self.smoke_runner is None:
            return PackOperationReport(
                action="smoke-test",
                pack_id=pack_id,
                state=status.state,
                status=status,
                message=(
                    f"No offline synthesis probe is registered for {pack_id}; "
                    "the pack remains installed but not ready."
                ),
            )
        machine = MachineIdentity.current()
        records = self.registry.records_directory(pack_id)
        checked_at = datetime.now(UTC)
        runtime_fingerprint = self.registry.runtime_fingerprint(pack)
        if runtime_fingerprint is None:
            raise ModelPackOperationError(
                f"Cannot smoke-test {pack_id}: its declared isolated runtime is incomplete."
            )
        try:
            result = self.smoke_runner(pack, status.selected_directory)
            try:
                audio = PcmAudio(
                    pcm_s16le=result.pcm_s16le,
                    sample_rate=result.sample_rate,
                )
            except ValueError as error:
                raise ModelPackOperationError(
                    f"Smoke runner returned invalid PCM audio: {error}"
                ) from error
            integrity_error = pcm_integrity_error(audio)
            if integrity_error is not None:
                raise ModelPackOperationError(
                    f"Smoke runner returned unsafe PCM audio: {integrity_error}."
                )
            duration = audio.frame_count / audio.sample_rate
            undeclared = set(result.validated_dependencies) - set(pack.dependencies)
            if undeclared:
                raise ModelPackOperationError(
                    "Smoke runner reported undeclared dependencies: "
                    + ", ".join(sorted(undeclared))
                )
            runtime = RuntimeEvidence(
                pack_id=pack_id,
                model_revision=pack.revision,
                runtime_revision=pack.runtime.revision,
                runtime_fingerprint=runtime_fingerprint,
                machine_id=machine.machine_id,
                checked_at=checked_at,
                success=True,
                message="Isolated runtime started for the offline synthesis probe.",
            )
            smoke = SmokeTestEvidence(
                pack_id=pack_id,
                model_revision=pack.revision,
                runtime_revision=pack.runtime.revision,
                runtime_fingerprint=runtime_fingerprint,
                machine_id=machine.machine_id,
                checked_at=checked_at,
                success=True,
                offline=True,
                output_sha256=hashlib.sha256(result.pcm_s16le).hexdigest(),
                output_duration_seconds=duration,
                message=result.message,
            )
        except BaseException as error:
            if not isinstance(error, Exception):
                raise
            if _deterministic_smoke_failure(error):
                runtime = RuntimeEvidence(
                    pack_id=pack_id,
                    model_revision=pack.revision,
                    runtime_revision=pack.runtime.revision,
                    runtime_fingerprint=runtime_fingerprint,
                    machine_id=machine.machine_id,
                    checked_at=checked_at,
                    success=False,
                    message=f"{type(error).__name__}: {error}"[:2_000],
                )
                _atomic_evidence(records / "runtime.json", runtime, safety_root=self.models_root)
            raise ModelPackOperationError(
                f"{pack_id} offline smoke test failed: {type(error).__name__}: {error}"
            ) from error
        _atomic_evidence(records / "runtime.json", runtime, safety_root=self.models_root)
        _atomic_evidence(records / "smoke.json", smoke, safety_root=self.models_root)
        # A model-specific probe may exercise a declared dependency as part of
        # the same real synthesis. MOSS generation, for example, necessarily
        # encodes the reference and decodes the output through Audio Tokenizer
        # v2. Bind that evidence to the dependency's own pinned revisions so a
        # successful parent probe does not require loading the same 5B runtime
        # a second time merely to repeat identical codec work.
        declared_dependencies = set(pack.dependencies)
        for dependency_id in result.validated_dependencies:
            assert dependency_id in declared_dependencies
            dependency = self.registry.get(dependency_id)
            dependency_records = self.registry.records_directory(dependency_id)
            dependency_runtime_fingerprint = self.registry.runtime_fingerprint(dependency)
            if dependency_runtime_fingerprint is None:
                raise ModelPackOperationError(
                    f"Smoke runner validated {dependency_id}, but its declared runtime "
                    "is incomplete."
                )
            dependency_runtime = RuntimeEvidence(
                pack_id=dependency_id,
                model_revision=dependency.revision,
                runtime_revision=dependency.runtime.revision,
                runtime_fingerprint=dependency_runtime_fingerprint,
                machine_id=machine.machine_id,
                checked_at=checked_at,
                success=True,
                message=f"Runtime exercised by the offline {pack_id} synthesis probe.",
            )
            dependency_smoke = SmokeTestEvidence(
                pack_id=dependency_id,
                model_revision=dependency.revision,
                runtime_revision=dependency.runtime.revision,
                runtime_fingerprint=dependency_runtime_fingerprint,
                machine_id=machine.machine_id,
                checked_at=checked_at,
                success=True,
                offline=True,
                output_sha256=smoke.output_sha256,
                output_duration_seconds=smoke.output_duration_seconds,
                message=f"Encode and decode paths passed inside the {pack_id} smoke test.",
            )
            _atomic_evidence(
                dependency_records / "runtime.json",
                dependency_runtime,
                safety_root=self.models_root,
            )
            _atomic_evidence(
                dependency_records / "smoke.json",
                dependency_smoke,
                safety_root=self.models_root,
            )
        final = self.registry.inspect(pack_id)
        return PackOperationReport(
            action="smoke-test",
            pack_id=pack_id,
            state=final.state,
            status=final,
            message=smoke.message,
        )

    def unload(self) -> PackOperationReport:
        slot = self._slot_status()
        owner = slot.owner.pack_id if slot.owner is not None else None
        if not slot.in_use:
            message = "No heavyweight model runtime is currently loaded."
        elif owner is None:
            message = (
                "The heavyweight-model slot is held by an active process, but its "
                "diagnostic owner record is unavailable; cancel or finish that process."
            )
        else:
            message = (
                f"{owner} is loaded by another active process; cancel or finish that run "
                "so its adapter can unload safely."
            )
        return PackOperationReport(action="unload", pack_id=owner, state=None, message=message)

    def remove(self, pack_id: str, *, confirmed: bool) -> PackOperationReport:
        pack = self.registry.get(pack_id)
        managed_pack_root = self.models_root / "managed" / "model-packs" / pack_id
        declared = self.models_root / pack.managed_directory
        declared_resolved = declared.resolve(strict=False)
        if not declared_resolved.is_relative_to(managed_pack_root.resolve(strict=False)):
            raise ModelPackOperationError(
                f"Pack {pack_id} is not installed below its managed pack root."
            )
        if not confirmed:
            raise ModelPackOperationError("Explicit removal confirmation is required.")
        current = self.registry.inspect(pack_id)
        if current.selected_directory.resolve(strict=False) != declared_resolved:
            raise ModelPackOperationError(
                f"{pack_id} is currently reused from {current.selected_directory}; that "
                "shared installation is read-only to the pack manager. Remove it through "
                "the installer that owns it, or install a managed copy first."
            )
        entry = self.trash.move_to_trash(
            pack_id,
            confirmation=ManagedPackTrash.remove_confirmation(pack_id),
        )
        status = self.registry.inspect(pack_id)
        return PackOperationReport(
            action="remove",
            pack_id=pack_id,
            state=status.state,
            status=status,
            receipt_id=entry.trash_id,
            message="Pack moved to recoverable managed trash; no model bytes were deleted.",
        )

    def restore(self, receipt_id: str) -> PackOperationReport:
        entry = self.trash.restore(
            receipt_id,
            confirmation=ManagedPackTrash.restore_confirmation(receipt_id),
        )
        status = self.registry.inspect(entry.pack_id)
        return PackOperationReport(
            action="restore",
            pack_id=entry.pack_id,
            state=status.state,
            status=status,
            receipt_id=receipt_id,
            message="Pack restored from managed trash.",
        )

    def _slot_status(self) -> HeavyweightSlotStatus:
        return HeavyweightModelSlot(self.models_root, "probe").status()

    @staticmethod
    def _offline_source(root: Path | None, relative_path: Path) -> Path | None:
        if root is None or not root.is_dir():
            return None
        direct = root / relative_path
        if direct.is_file() and not direct.is_symlink():
            return direct
        matches = [
            path
            for path in root.rglob(relative_path.name)
            if path.is_file() and not path.is_symlink()
        ]
        if len(matches) > 1:
            raise ModelPackOperationError(
                f"Offline directory contains ambiguous copies of {relative_path.name}."
            )
        return matches[0] if matches else None

    @staticmethod
    def _copy_with_progress(
        source: Path,
        destination: Path,
        *,
        expected_size: int,
        progress: Callable[[int], object],
        safety_root: Path | None = None,
    ) -> None:
        if source.stat().st_size != expected_size:
            raise ModelPackOperationError(
                f"Offline file {source} has {source.stat().st_size} bytes; "
                f"expected {expected_size}."
            )
        if safety_root is not None:
            destination = safe_managed_path(safety_root, destination, label="partial staging file")
        destination.unlink(missing_ok=True)
        if safety_root is not None:
            safe_managed_path(safety_root, destination, label="partial staging file")
        with source.open("rb") as input_file, destination.open("wb") as output_file:
            copied = 0
            while chunk := input_file.read(4 * 1024 * 1024):
                output_file.write(chunk)
                output_file.flush()
                os.fsync(output_file.fileno())
                copied += len(chunk)
                progress(copied)

    @staticmethod
    def _download_with_progress(
        pack: ModelPackSpec,
        relative_path: Path,
        destination: Path,
        *,
        expected_size: int,
        progress: Callable[[int], object],
        safety_root: Path | None = None,
    ) -> None:
        if safety_root is not None:
            destination = safe_managed_path(safety_root, destination, label="partial staging file")
        existing = destination.stat().st_size if destination.is_file() else 0
        if existing > expected_size:
            raise ModelPackOperationError("Partial download exceeds the pinned file size.")
        # The caller has already verified that this path does not match the
        # pinned digest. A full-sized file therefore cannot be resumed: asking
        # for ``bytes=<size>-`` commonly produces HTTP 416 forever. Restart
        # that one artifact while preserving genuinely partial downloads.
        if existing == expected_size:
            destination.unlink()
            existing = 0
        url = (
            pack.source_repository.rstrip("/")
            + "/resolve/"
            + urllib.parse.quote(pack.revision, safe="")
            + "/"
            + urllib.parse.quote(relative_path.as_posix(), safe="/")
        )
        request = urllib.request.Request(url, headers={"Range": f"bytes={existing}-"})
        try:
            response = urllib.request.urlopen(request, timeout=60)
        except (OSError, urllib.error.URLError) as error:
            raise ModelPackOperationError(f"Could not download {relative_path}: {error}") from error
        with response:
            status = getattr(response, "status", None)
            if existing and status != 206:
                existing = 0
                if safety_root is not None:
                    safe_managed_path(safety_root, destination, label="partial staging file")
                destination.unlink(missing_ok=True)
            mode = "ab" if existing else "wb"
            if safety_root is not None:
                safe_managed_path(safety_root, destination, label="partial staging file")
            with destination.open(mode) as output:
                downloaded = existing
                while chunk := response.read(4 * 1024 * 1024):
                    output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                    downloaded += len(chunk)
                    if downloaded > expected_size:
                        raise ModelPackOperationError(
                            f"Remote file {relative_path} exceeds its pinned size."
                        )
                    progress(downloaded)
