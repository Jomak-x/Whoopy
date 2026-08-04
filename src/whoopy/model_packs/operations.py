"""Durable, cross-process operations for managed local model packs.

This module deliberately knows nothing about download URLs or model-specific
runtime code.  It supplies the safety primitives used by the model-pack
registry, installers, smoke tests, and adapters:

* one OS-backed slot for heavyweight runtimes;
* monotonic, byte-based installation progress;
* portable runtime resource measurements; and
* recoverable removal into managed trash.

All persistent writes use a temporary file followed by ``replace`` so a power
loss cannot leave a half-written JSON document.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, Literal, cast

import psutil
from pydantic import BaseModel, ConfigDict, Field, model_validator

_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_LOCAL_LOCKS_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}


class ModelPackOperationError(ValueError):
    """Base error for an unsafe or failed local model-pack operation."""


class HeavyweightModelSlotUnavailable(ModelPackOperationError):
    """Another Fish, MOSS, or Qwen runtime already owns the local slot."""


class ProgressConflictError(ModelPackOperationError):
    """A stale or non-monotonic installation-progress update was refused."""


class ManagedTrashError(ModelPackOperationError):
    """A model-pack trash operation failed its safety checks."""


class PackInstallUnavailable(ModelPackOperationError):
    """Another live process already owns a pack's complete install operation."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _safe_identifier(value: str, *, label: str) -> str:
    if _SAFE_ID.fullmatch(value) is None:
        raise ModelPackOperationError(f"Invalid {label}: {value!r}")
    return value


def models_root_for_runtime(runtime_directory: Path) -> Path:
    """Find the shared ``models`` ancestor used by every heavyweight adapter.

    Existing experimental installs and future managed packs have different
    depths, so deriving the slot from the immediate parent would accidentally
    create multiple independent locks.
    """

    for candidate in (runtime_directory, *runtime_directory.parents):
        if candidate.name == "models":
            return candidate
    return runtime_directory.parent


def safe_managed_path(root: Path, candidate: Path, *, label: str = "managed path") -> Path:
    """Return a lexical path below ``root`` only when no component is a symlink.

    ``Path.resolve()`` is intentionally not used for the containment decision:
    resolving first would hide the very symlink boundary this guard needs to
    reject. Missing components are allowed so callers can validate a path
    immediately before creating it. Every existing component from the trusted
    root through the leaf is checked with ``lstat`` so dangling links are also
    refused.
    """

    trusted_root = Path(os.path.abspath(root))
    checked = Path(os.path.abspath(candidate))
    try:
        relative = checked.relative_to(trusted_root)
    except ValueError as error:
        raise ModelPackOperationError(
            f"unsafe {label} escapes its managed root: {candidate}"
        ) from error

    components = (
        trusted_root,
        *(
            trusted_root / Path(*relative.parts[:index])
            for index in range(1, len(relative.parts) + 1)
        ),
    )
    for index, component in enumerate(components):
        try:
            mode = component.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ModelPackOperationError(
                f"Could not inspect {label} {component}: {error}"
            ) from error
        if stat.S_ISLNK(mode):
            raise ModelPackOperationError(f"unsafe {label} contains a symlink: {component}")
        if index < len(components) - 1 and not stat.S_ISDIR(mode):
            raise ModelPackOperationError(f"unsafe {label} has a non-directory parent: {component}")
    return checked


def _atomic_json(
    path: Path,
    document: Mapping[str, object],
    *,
    safety_root: Path | None = None,
) -> None:
    if safety_root is not None:
        path = safe_managed_path(safety_root, path, label="JSON destination")
        safe_managed_path(safety_root, path.parent, label="JSON parent")
    path.parent.mkdir(parents=True, exist_ok=True)
    if safety_root is not None:
        safe_managed_path(safety_root, path.parent, label="JSON parent")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as destination:
            json.dump(document, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        if safety_root is not None:
            safe_managed_path(safety_root, path, label="JSON destination")
        temporary.replace(path)
        # Persist the directory entry too where the platform supports it.
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class _AdvisoryFileLock:
    """Portable exclusive lock with process and thread contention semantics."""

    def __init__(self, path: Path, *, safety_root: Path | None = None) -> None:
        self.path = path
        self.safety_root = safety_root
        self._file: BinaryIO | None = None
        self._local_lock: threading.Lock | None = None

    @staticmethod
    def _local_for(path: Path) -> threading.Lock:
        key = str(path.resolve(strict=False))
        with _LOCAL_LOCKS_GUARD:
            return _LOCAL_LOCKS.setdefault(key, threading.Lock())

    def acquire(self, *, timeout_seconds: float = 0, poll_seconds: float = 0.05) -> bool:
        if timeout_seconds < 0 or poll_seconds <= 0:
            raise ValueError("lock timeout must be non-negative and poll interval positive")
        if self._file is not None:
            raise RuntimeError(f"Lock is already held: {self.path}")
        deadline = time.monotonic() + timeout_seconds
        local_lock = self._local_for(self.path)
        remaining = max(0.0, deadline - time.monotonic())
        acquired_local = (
            local_lock.acquire(timeout=remaining) if timeout_seconds else local_lock.acquire(False)
        )
        if not acquired_local:
            return False
        self._local_lock = local_lock
        if self.safety_root is not None:
            safe_managed_path(self.safety_root, self.path, label="lock path")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_file: BinaryIO | None = None
        try:
            if self.safety_root is not None:
                safe_managed_path(self.safety_root, self.path, label="lock path")
            lock_file = self.path.open("a+b")
            while True:
                try:
                    if os.name == "nt":
                        self._acquire_windows(lock_file)
                    else:
                        self._acquire_posix(lock_file)
                    self._file = lock_file
                    return True
                except (OSError, BlockingIOError):
                    if time.monotonic() >= deadline:
                        lock_file.close()
                        self._release_local()
                        return False
                    time.sleep(min(poll_seconds, max(0.0, deadline - time.monotonic())))
        except BaseException:
            if lock_file is not None and not lock_file.closed:
                lock_file.close()
            self._release_local()
            raise

    @staticmethod
    def _acquire_posix(lock_file: BinaryIO) -> None:
        import fcntl

        cast(Any, fcntl).flock(
            lock_file.fileno(), cast(Any, fcntl).LOCK_EX | cast(Any, fcntl).LOCK_NB
        )

    @staticmethod
    def _acquire_windows(lock_file: BinaryIO) -> None:
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        cast(Any, msvcrt).locking(lock_file.fileno(), cast(Any, msvcrt).LK_NBLCK, 1)

    def _release_local(self) -> None:
        local_lock, self._local_lock = self._local_lock, None
        if local_lock is not None:
            local_lock.release()

    def release(self) -> None:
        lock_file, self._file = self._file, None
        if lock_file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                cast(Any, msvcrt).locking(lock_file.fileno(), cast(Any, msvcrt).LK_UNLCK, 1)
            else:
                import fcntl

                cast(Any, fcntl).flock(lock_file.fileno(), cast(Any, fcntl).LOCK_UN)
        finally:
            lock_file.close()
            self._release_local()


class PackInstallLock:
    """Hold one pack's entire install transaction across processes.

    The kernel lock is authoritative and is released automatically after a
    crash or ``SIGKILL``. A durable progress record can therefore be resumed by
    the next owner without allowing two live writers into the same staging
    files.
    """

    def __init__(self, models_root: Path, pack_id: str) -> None:
        self.models_root = Path(os.path.abspath(models_root))
        self.pack_id = _safe_identifier(pack_id, label="pack ID")
        path = self.models_root / "managed" / ".locks" / "model-packs" / f"{pack_id}.lock"
        self._lock = _AdvisoryFileLock(path, safety_root=self.models_root)

    def acquire(self, *, timeout_seconds: float = 0) -> None:
        if not self._lock.acquire(timeout_seconds=timeout_seconds):
            raise PackInstallUnavailable(
                f"Another process is already installing model pack {self.pack_id}."
            )

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> PackInstallLock:
        self.acquire()
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


class HeavyweightSlotOwner(BaseModel):
    """Human-readable diagnostic record for the current runtime-slot owner."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    token: str
    pack_id: str
    pid: int = Field(gt=0)
    acquired_at: datetime


class HeavyweightSlotStatus(BaseModel):
    """Authoritative OS-lock state with optional diagnostic owner metadata."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    in_use: bool
    owner: HeavyweightSlotOwner | None = None


class HeavyweightModelSlot:
    """Allow only one heavyweight local speech runtime to load at a time.

    The JSON owner record is diagnostic only. Exclusivity comes from the OS
    lock, which the kernel releases even if Python or the worker is killed.
    """

    def __init__(self, managed_root: Path, pack_id: str) -> None:
        self.managed_root = Path(os.path.abspath(managed_root))
        self.root = self.managed_root / "runtime-slot"
        self.pack_id = _safe_identifier(pack_id, label="pack ID")
        self._lock = _AdvisoryFileLock(
            self.root / "heavyweight.lock", safety_root=self.managed_root
        )
        self._token: str | None = None

    @property
    def owner_path(self) -> Path:
        return self.root / "owner.json"

    def _read_owner(self) -> HeavyweightSlotOwner | None:
        try:
            return HeavyweightSlotOwner.model_validate_json(
                self.owner_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def status(self) -> HeavyweightSlotStatus:
        """Probe the kernel lock and discard an owner record left by a crash.

        ``owner.json`` is intentionally never treated as proof of ownership.
        If a non-blocking probe obtains the OS lock, no live process owns the
        slot and any record is stale.  If the lock is busy, owner metadata is
        returned only as a diagnostic and may be absent or malformed.
        """

        if self._token is not None:
            return HeavyweightSlotStatus(in_use=True, owner=self._read_owner())
        probe = _AdvisoryFileLock(self.root / "heavyweight.lock", safety_root=self.managed_root)
        if probe.acquire(timeout_seconds=0):
            try:
                self.owner_path.unlink(missing_ok=True)
            finally:
                probe.release()
            return HeavyweightSlotStatus(in_use=False)
        return HeavyweightSlotStatus(in_use=True, owner=self._read_owner())

    def current_owner(self) -> HeavyweightSlotOwner | None:
        """Return a live lock owner's diagnostics, never stale JSON."""

        return self.status().owner

    def acquire(self, *, timeout_seconds: float = 0) -> HeavyweightSlotOwner:
        if self._token is not None:
            raise RuntimeError(f"Runtime slot is already held by {self.pack_id}")
        if not self._lock.acquire(timeout_seconds=timeout_seconds):
            owner = self.current_owner()
            detail = f" by {owner.pack_id} (pid {owner.pid})" if owner is not None else ""
            raise HeavyweightModelSlotUnavailable(
                f"The heavyweight-model slot is already in use{detail}."
            )
        token = str(uuid.uuid4())
        owner = HeavyweightSlotOwner(
            token=token,
            pack_id=self.pack_id,
            pid=os.getpid(),
            acquired_at=_utc_now(),
        )
        try:
            _atomic_json(
                self.owner_path,
                owner.model_dump(mode="json"),
                safety_root=self.managed_root,
            )
        except BaseException:
            self._lock.release()
            raise
        self._token = token
        return owner

    def release(self) -> None:
        token, self._token = self._token, None
        if token is None:
            return
        try:
            owner = self.current_owner()
            if owner is not None and owner.token == token:
                self.owner_path.unlink(missing_ok=True)
        finally:
            self._lock.release()

    def __enter__(self) -> HeavyweightModelSlot:
        self.acquire()
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()


class TransferState(StrEnum):
    """Durable lifecycle for a pack installation or verification operation."""

    PENDING = "pending"
    TRANSFERRING = "transferring"
    VERIFYING = "verifying"
    INSTALLING = "installing"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ArtifactByteProgress(BaseModel):
    """Byte position for one immutable file in a model pack."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str
    bytes_downloaded: int = Field(ge=0)
    bytes_total: int = Field(gt=0)

    @model_validator(mode="after")
    def _download_fits_total(self) -> ArtifactByteProgress:
        if self.bytes_downloaded > self.bytes_total:
            raise ValueError("downloaded bytes cannot exceed total bytes")
        return self


class PackTransferProgress(BaseModel):
    """Atomic on-disk progress record that can survive process interruption."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    operation_id: str
    pack_id: str
    revision: str
    state: TransferState
    artifacts: tuple[ArtifactByteProgress, ...]
    bytes_downloaded: int = Field(ge=0)
    bytes_total: int = Field(ge=0)
    started_at: datetime
    updated_at: datetime
    message: str | None = None

    @model_validator(mode="after")
    def _totals_match_artifacts(self) -> PackTransferProgress:
        if self.bytes_downloaded != sum(item.bytes_downloaded for item in self.artifacts):
            raise ValueError("aggregate downloaded bytes must match artifact progress")
        if self.bytes_total != sum(item.bytes_total for item in self.artifacts):
            raise ValueError("aggregate total bytes must match artifact progress")
        if self.state is TransferState.COMPLETE and self.bytes_downloaded != self.bytes_total:
            raise ValueError("a complete transfer must have every byte")
        return self


class PackProgressStore:
    """Persist monotonic per-artifact progress below one pack's records folder."""

    def __init__(self, records_directory: Path, *, safety_root: Path | None = None) -> None:
        self.records_directory = records_directory
        self.safety_root = safety_root or records_directory
        self.path = records_directory / "install-progress.json"
        self._lock_path = records_directory / "install-progress.lock"

    def load(self) -> PackTransferProgress | None:
        try:
            return PackTransferProgress.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise ProgressConflictError(
                f"Could not read durable progress {self.path}: {error}"
            ) from error

    def start(
        self,
        *,
        pack_id: str,
        revision: str,
        artifacts: Mapping[str, int],
        operation_id: str | None = None,
    ) -> PackTransferProgress:
        _safe_identifier(pack_id, label="pack ID")
        if not revision or not artifacts or any(total <= 0 for total in artifacts.values()):
            raise ValueError("revision and positive artifact sizes are required")
        progress = PackTransferProgress(
            operation_id=operation_id or str(uuid.uuid4()),
            pack_id=pack_id,
            revision=revision,
            state=TransferState.PENDING,
            artifacts=tuple(
                ArtifactByteProgress(artifact_id=artifact_id, bytes_downloaded=0, bytes_total=total)
                for artifact_id, total in sorted(artifacts.items())
            ),
            bytes_downloaded=0,
            bytes_total=sum(artifacts.values()),
            started_at=_utc_now(),
            updated_at=_utc_now(),
        )
        lock = _AdvisoryFileLock(self._lock_path, safety_root=self.safety_root)
        if not lock.acquire(timeout_seconds=5):
            raise ProgressConflictError("Another process is updating installation progress.")
        try:
            current = self.load()
            if current is not None and current.state not in {
                TransferState.COMPLETE,
                TransferState.FAILED,
                TransferState.CANCELLED,
            }:
                raise ProgressConflictError(
                    f"Operation {current.operation_id} is already active for {current.pack_id}."
                )
            self._write(progress)
        finally:
            lock.release()
        return progress

    def update_artifact(
        self,
        operation_id: str,
        artifact_id: str,
        bytes_downloaded: int,
        *,
        state: TransferState = TransferState.TRANSFERRING,
        message: str | None = None,
    ) -> PackTransferProgress:
        if bytes_downloaded < 0:
            raise ValueError("downloaded bytes cannot be negative")
        lock = _AdvisoryFileLock(self._lock_path, safety_root=self.safety_root)
        if not lock.acquire(timeout_seconds=5):
            raise ProgressConflictError("Another process is updating installation progress.")
        try:
            current = self._require_operation(operation_id)
            if current.state in {
                TransferState.COMPLETE,
                TransferState.FAILED,
                TransferState.CANCELLED,
            }:
                raise ProgressConflictError(f"Operation {operation_id} is already terminal.")
            found = False
            artifacts: list[ArtifactByteProgress] = []
            for item in current.artifacts:
                if item.artifact_id != artifact_id:
                    artifacts.append(item)
                    continue
                found = True
                if bytes_downloaded < item.bytes_downloaded:
                    raise ProgressConflictError(
                        f"Progress for {artifact_id} cannot move backwards."
                    )
                artifacts.append(item.model_copy(update={"bytes_downloaded": bytes_downloaded}))
            if not found:
                raise ProgressConflictError(f"Unknown artifact {artifact_id!r}.")
            updated = current.model_copy(
                update={
                    "state": state,
                    "artifacts": tuple(artifacts),
                    "bytes_downloaded": sum(item.bytes_downloaded for item in artifacts),
                    "updated_at": _utc_now(),
                    "message": message,
                }
            )
            updated = PackTransferProgress.model_validate(updated.model_dump())
            self._write(updated)
            return updated
        finally:
            lock.release()

    def set_state(
        self,
        operation_id: str,
        state: TransferState,
        *,
        message: str | None = None,
    ) -> PackTransferProgress:
        lock = _AdvisoryFileLock(self._lock_path, safety_root=self.safety_root)
        if not lock.acquire(timeout_seconds=5):
            raise ProgressConflictError("Another process is updating installation progress.")
        try:
            current = self._require_operation(operation_id)
            updated = PackTransferProgress.model_validate(
                current.model_copy(
                    update={"state": state, "updated_at": _utc_now(), "message": message}
                ).model_dump()
            )
            self._write(updated)
            return updated
        finally:
            lock.release()

    def _require_operation(self, operation_id: str) -> PackTransferProgress:
        current = self.load()
        if current is None or current.operation_id != operation_id:
            raise ProgressConflictError(f"Stale or unknown operation ID: {operation_id}")
        return current

    def _write(self, progress: PackTransferProgress) -> None:
        _atomic_json(
            self.path,
            progress.model_dump(mode="json"),
            safety_root=self.safety_root,
        )


class AcceleratorUsage(BaseModel):
    """Accelerator selected by the runtime, not merely present on the machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["cpu", "metal", "cuda", "directml", "other"]
    device_name: str | None = None


class ModelPerformanceRecord(BaseModel):
    """Comparable startup, rendering, and unload measurements for one revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    measurement_id: str
    pack_id: str
    revision: str
    recorded_at: datetime
    startup_duration_seconds: float = Field(ge=0)
    render_duration_seconds: float = Field(ge=0)
    rendered_audio_seconds: float = Field(gt=0)
    render_seconds_per_audio_second: float = Field(ge=0)
    peak_memory_bytes: int = Field(ge=0)
    accelerator: AcceleratorUsage
    memory_before_load_bytes: int = Field(ge=0)
    memory_after_unload_bytes: int = Field(ge=0)
    available_memory_after_unload_bytes: int = Field(ge=0)
    unload_duration_seconds: float = Field(ge=0)
    unload_succeeded: bool


class PerformanceRecordStore:
    """Append immutable performance records without rewriting prior evidence."""

    def __init__(self, records_directory: Path) -> None:
        self.directory = records_directory / "performance"

    def write(self, record: ModelPerformanceRecord) -> Path:
        _safe_identifier(record.pack_id, label="pack ID")
        path = self.directory / f"{record.measurement_id}.json"
        if path.exists():
            raise ModelPackOperationError(f"Performance record already exists: {path.name}")
        _atomic_json(path, record.model_dump(mode="json"))
        return path

    def list(self) -> tuple[ModelPerformanceRecord, ...]:
        if not self.directory.is_dir():
            return ()
        records: list[ModelPerformanceRecord] = []
        for path in sorted(self.directory.glob("*.json")):
            try:
                records.append(
                    ModelPerformanceRecord.model_validate_json(path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError) as error:
                raise ModelPackOperationError(
                    f"Invalid performance record {path}: {error}"
                ) from error
        return tuple(records)


class ModelPerformanceRecorder:
    """Measure a runtime's process tree from before load until after unload."""

    def __init__(
        self,
        *,
        pack_id: str,
        revision: str,
        accelerator: AcceleratorUsage,
        process_id: int | None = None,
        clock: Callable[[], float] = time.monotonic,
        sample_interval_seconds: float = 0.1,
    ) -> None:
        if sample_interval_seconds <= 0:
            raise ValueError("sample interval must be positive")
        self.pack_id = _safe_identifier(pack_id, label="pack ID")
        self.revision = revision
        self.accelerator = accelerator
        self.process_id = process_id or os.getpid()
        self._clock = clock
        self._interval = sample_interval_seconds
        self._started = clock()
        self._ready_at: float | None = None
        self._render_started: float | None = None
        self._render_duration: float | None = None
        self._memory_before = self._rss_bytes()
        self._peak_memory = self._memory_before
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._sample_until_stopped,
            name=f"{pack_id}-resource-sampler",
            daemon=True,
        )
        self._thread.start()

    def _rss_bytes(self) -> int:
        try:
            process = psutil.Process(self.process_id)
            total = process.memory_info().rss if process.is_running() else 0
            # Some restricted environments deny the system-wide process scan
            # used by ``children(recursive=True)``. The known process RSS is
            # still valid and must not be discarded in that case.
            try:
                children = process.children(recursive=True)
            except (psutil.Error, OSError):
                children = []
            for child in children:
                try:
                    if child.is_running():
                        total += child.memory_info().rss
                except (psutil.Error, OSError):
                    continue
            return total
        except (psutil.Error, OSError):
            return 0

    def track_process(self, process_id: int) -> None:
        """Switch sampling to a just-started isolated model worker."""

        if process_id <= 0:
            raise ValueError("tracked process ID must be positive")
        self.process_id = process_id
        self._sample()

    def set_accelerator(self, accelerator: AcceleratorUsage) -> None:
        """Replace the preflight expectation with the runtime's actual device."""

        self.accelerator = accelerator

    def _sample(self) -> None:
        self._peak_memory = max(self._peak_memory, self._rss_bytes())

    def _sample_until_stopped(self) -> None:
        while not self._stop.wait(self._interval):
            self._sample()

    def mark_model_ready(self) -> None:
        if self._ready_at is not None:
            raise RuntimeError("model readiness was already recorded")
        self._sample()
        self._ready_at = self._clock()

    def begin_render(self) -> None:
        if self._ready_at is None:
            raise RuntimeError("mark the model ready before rendering")
        if self._render_started is not None:
            raise RuntimeError("render timing was already started")
        self._render_started = self._clock()

    def end_render(self) -> None:
        if self._render_started is None or self._render_duration is not None:
            raise RuntimeError("render timing is not active")
        self._sample()
        self._render_duration = self._clock() - self._render_started

    def finish_unload(
        self,
        *,
        rendered_audio_seconds: float,
        unload_started_at: float,
        unload_succeeded: bool,
    ) -> ModelPerformanceRecord:
        if rendered_audio_seconds <= 0:
            raise ValueError("rendered audio duration must be positive")
        if self._ready_at is None or self._render_duration is None:
            raise RuntimeError("startup and render timing must finish before unload")
        finished = self._clock()
        self._sample()
        self._stop.set()
        self._thread.join(timeout=max(1.0, self._interval * 5))
        memory_after = self._rss_bytes()
        return ModelPerformanceRecord(
            measurement_id=str(uuid.uuid4()),
            pack_id=self.pack_id,
            revision=self.revision,
            recorded_at=_utc_now(),
            startup_duration_seconds=self._ready_at - self._started,
            render_duration_seconds=self._render_duration,
            rendered_audio_seconds=rendered_audio_seconds,
            render_seconds_per_audio_second=self._render_duration / rendered_audio_seconds,
            peak_memory_bytes=self._peak_memory,
            accelerator=self.accelerator,
            memory_before_load_bytes=self._memory_before,
            memory_after_unload_bytes=memory_after,
            available_memory_after_unload_bytes=psutil.virtual_memory().available,
            unload_duration_seconds=max(0.0, finished - unload_started_at),
            unload_succeeded=unload_succeeded,
        )

    def close(self) -> None:
        """Stop sampling if measurement exits before producing a record."""

        self._stop.set()
        self._thread.join(timeout=max(1.0, self._interval * 5))


class TrashEntry(BaseModel):
    """Manifest for one recoverable pack move."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    trash_id: str
    pack_id: str
    original_relative_path: str
    moved_at: datetime


class ManagedPackTrash:
    """Move direct children of a managed root into recoverable local trash."""

    def __init__(self, managed_root: Path) -> None:
        self.managed_root = Path(os.path.abspath(managed_root))
        self.trash_root = self.managed_root / ".trash"
        self._lock_path = self.managed_root / ".trash.lock"

    @staticmethod
    def remove_confirmation(pack_id: str) -> str:
        return f"REMOVE {pack_id}"

    @staticmethod
    def restore_confirmation(trash_id: str) -> str:
        return f"RESTORE {trash_id}"

    def move_to_trash(self, pack_id: str, *, confirmation: str) -> TrashEntry:
        _safe_identifier(pack_id, label="pack ID")
        expected = self.remove_confirmation(pack_id)
        if confirmation != expected:
            raise ManagedTrashError(f"Explicit confirmation required: {expected}")
        source = self.managed_root / pack_id
        self._validate_direct_pack(source)
        lock = _AdvisoryFileLock(self._lock_path, safety_root=self.managed_root)
        if not lock.acquire(timeout_seconds=5):
            raise ManagedTrashError("Another managed-trash operation is active.")
        entry_directory: Path | None = None
        try:
            self._validate_direct_pack(source)
            self._validate_trash_root(allow_missing=True)
            trash_id = f"{pack_id}-{uuid.uuid4()}"
            entry = TrashEntry(
                trash_id=trash_id,
                pack_id=pack_id,
                original_relative_path=pack_id,
                moved_at=_utc_now(),
            )
            entry_directory = self.trash_root / trash_id
            safe_managed_path(self.managed_root, entry_directory, label="managed trash entry")
            entry_directory.mkdir(parents=True, exist_ok=False)
            safe_managed_path(self.managed_root, entry_directory, label="managed trash entry")
            _atomic_json(
                entry_directory / "trash.json",
                entry.model_dump(mode="json"),
                safety_root=self.managed_root,
            )
            safe_managed_path(self.managed_root, source, label="managed pack source")
            safe_managed_path(
                self.managed_root,
                entry_directory / "pack",
                label="managed trash payload",
            )
            source.replace(entry_directory / "pack")
            return entry
        except BaseException:
            if entry_directory is not None and entry_directory.is_dir():
                moved = entry_directory / "pack"
                if moved.exists() and not source.exists():
                    moved.replace(source)
                (entry_directory / "trash.json").unlink(missing_ok=True)
                with suppress(OSError):
                    entry_directory.rmdir()
            raise
        finally:
            lock.release()

    def restore(self, trash_id: str, *, confirmation: str) -> TrashEntry:
        _safe_identifier(trash_id, label="trash ID")
        expected = self.restore_confirmation(trash_id)
        if confirmation != expected:
            raise ManagedTrashError(f"Explicit confirmation required: {expected}")
        lock = _AdvisoryFileLock(self._lock_path, safety_root=self.managed_root)
        if not lock.acquire(timeout_seconds=5):
            raise ManagedTrashError("Another managed-trash operation is active.")
        try:
            self._validate_trash_root(allow_missing=False)
            entry_directory = self.trash_root / trash_id
            try:
                safe_managed_path(self.managed_root, entry_directory, label="managed trash entry")
            except ModelPackOperationError as error:
                raise ManagedTrashError(str(error)) from error
            if not entry_directory.is_dir() or entry_directory.is_symlink():
                raise ManagedTrashError(f"Trash entry {trash_id} is unsafe.")
            entry = self._read_entry(entry_directory)
            destination = self.managed_root / entry.original_relative_path
            try:
                safe_managed_path(
                    self.managed_root, destination, label="managed restore destination"
                )
            except ModelPackOperationError as error:
                raise ManagedTrashError(str(error)) from error
            if destination.exists() or destination.is_symlink():
                raise ManagedTrashError(f"Cannot restore over existing pack {entry.pack_id}.")
            payload = entry_directory / "pack"
            try:
                safe_managed_path(self.managed_root, payload, label="managed trash payload")
            except ModelPackOperationError as error:
                raise ManagedTrashError(str(error)) from error
            if not payload.exists() or payload.is_symlink():
                raise ManagedTrashError(f"Trash entry {trash_id} has no safe pack payload.")
            if not payload.is_dir():
                raise ManagedTrashError(f"Trash entry {trash_id} has no directory payload.")
            try:
                safe_managed_path(
                    self.managed_root, destination, label="managed restore destination"
                )
            except ModelPackOperationError as error:
                raise ManagedTrashError(str(error)) from error
            payload.replace(destination)
            (entry_directory / "trash.json").unlink(missing_ok=True)
            entry_directory.rmdir()
            return entry
        finally:
            lock.release()

    def list(self) -> tuple[TrashEntry, ...]:
        self._validate_trash_root(allow_missing=True)
        if not self.trash_root.exists():
            return ()
        return tuple(
            self._read_entry(path)
            for path in sorted(self.trash_root.iterdir())
            if path.is_dir() and not path.is_symlink()
        )

    def _validate_direct_pack(self, source: Path) -> None:
        try:
            checked = safe_managed_path(self.managed_root, source, label="managed pack source")
        except ModelPackOperationError as error:
            raise ManagedTrashError(str(error)) from error
        if checked.parent != self.managed_root:
            raise ManagedTrashError("Only a direct child of the managed root can be removed.")
        if not source.is_dir() or source.is_symlink():
            raise ManagedTrashError(f"Managed pack does not exist or is unsafe: {source.name}")
        if source.name.startswith("."):
            raise ManagedTrashError("Managed metadata directories cannot be removed as packs.")

    def _read_entry(self, directory: Path) -> TrashEntry:
        self._validate_trash_root(allow_missing=False)
        try:
            checked = safe_managed_path(self.managed_root, directory, label="managed trash entry")
        except ModelPackOperationError as error:
            raise ManagedTrashError(str(error)) from error
        if checked.parent != self.trash_root:
            raise ManagedTrashError("Trash entry escaped the managed trash root.")
        if not checked.is_dir() or checked.is_symlink():
            raise ManagedTrashError(f"Trash entry {directory.name} is unsafe.")
        manifest = checked / "trash.json"
        try:
            safe_managed_path(self.managed_root, manifest, label="managed trash manifest")
        except ModelPackOperationError as error:
            raise ManagedTrashError(str(error)) from error
        if manifest.is_symlink() or not manifest.is_file():
            raise ManagedTrashError(f"Trash entry {directory.name} has no safe manifest.")
        try:
            entry = TrashEntry.model_validate_json(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ManagedTrashError(f"Invalid trash entry {directory.name}: {error}") from error
        if entry.trash_id != directory.name or entry.original_relative_path != entry.pack_id:
            raise ManagedTrashError(f"Trash entry {directory.name} has unsafe path metadata.")
        _safe_identifier(entry.pack_id, label="pack ID")
        return entry

    def _validate_trash_root(self, *, allow_missing: bool) -> None:
        try:
            safe_managed_path(self.managed_root, self.trash_root, label="managed trash root")
        except ModelPackOperationError as error:
            raise ManagedTrashError(str(error)) from error
        if self.trash_root.exists() and not self.trash_root.is_dir():
            raise ManagedTrashError("Managed trash root is not a directory.")
        if not allow_missing and not self.trash_root.is_dir():
            raise ManagedTrashError("Managed trash root does not exist.")
