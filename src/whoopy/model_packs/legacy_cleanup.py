"""Recoverable cleanup for explicitly reviewed legacy experimental runtimes.

The cleanup service deliberately cannot discover arbitrary "junk".  It only
quarantines paths named by a checked-in allowlist, never follows symlinks, and
never selects a checkpoint directory.  A durable receipt is written before
the first rename so an interrupted transaction remains inspectable and
restorable.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from whoopy.model_packs.operations import (
    HeavyweightModelSlot,
    HeavyweightModelSlotUnavailable,
    ModelPackOperationError,
    _AdvisoryFileLock,
    _atomic_json,
    safe_managed_path,
)


class LegacyCleanupError(ModelPackOperationError):
    """A legacy cleanup request was unsafe, stale, or failed integrity checks."""


class LegacyCleanupTarget(BaseModel):
    """One exact path or tightly constrained stale-download glob."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: Literal["virtualenv", "source", "git", "stale-download"]
    path: str | None = None
    glob: str | None = None

    @model_validator(mode="after")
    def _validate_selector(self) -> LegacyCleanupTarget:
        if (self.path is None) == (self.glob is None):
            raise ValueError("cleanup target must declare exactly one of path or glob")
        selector = self.path or self.glob or ""
        parsed = PurePosixPath(selector)
        if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
            raise ValueError("cleanup selectors must be relative and cannot contain '..'")
        if self.glob is not None:
            if self.category != "stale-download":
                raise ValueError("only stale-download targets may use a glob")
            if not (
                selector.startswith("checkpoints/")
                and "/.cache/huggingface/download/" in selector
                and selector.endswith("*.incomplete")
            ):
                raise ValueError("stale-download globs must target Hugging Face *.incomplete files")
        elif self.category == "stale-download":
            raise ValueError("stale-download targets must use a constrained glob")
        elif parsed.parts[0] == "checkpoints":
            raise ValueError("exact cleanup targets may never select checkpoint content")
        return self


class LegacyCleanupRoot(BaseModel):
    """One reviewed legacy runtime root beneath the models directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    relative_path: str
    targets: tuple[LegacyCleanupTarget, ...]

    @model_validator(mode="after")
    def _validate_root(self) -> LegacyCleanupRoot:
        parsed = PurePosixPath(self.relative_path)
        if (
            parsed.is_absolute()
            or not parsed.parts
            or ".." in parsed.parts
            or parsed.parts[0] != "experimental"
        ):
            raise ValueError("legacy roots must be relative descendants of experimental/")
        if not self.targets:
            raise ValueError("legacy cleanup roots need at least one allowlisted target")
        return self


class LegacyCleanupManifest(BaseModel):
    """Versioned allowlist for recoverable legacy cleanup."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    roots: tuple[LegacyCleanupRoot, ...]

    @model_validator(mode="after")
    def _unique_roots(self) -> LegacyCleanupManifest:
        ids = [root.root_id for root in self.roots]
        paths = [root.relative_path for root in self.roots]
        if len(ids) != len(set(ids)) or len(paths) != len(set(paths)):
            raise ValueError("legacy cleanup root IDs and paths must be unique")
        return self


class CleanupSnapshot(BaseModel):
    """Content identity computed without following filesystem links."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["file", "directory"]
    size_bytes: int = Field(ge=0)
    file_count: int = Field(ge=0)
    symlink_count: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CleanupCandidate(BaseModel):
    """One currently present manifest-approved cleanup candidate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_id: str
    category: str
    source_relative_path: str
    snapshot: CleanupSnapshot


class CleanupPlan(BaseModel):
    """Deterministic dry-run report and explicit apply token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    plan_id: str
    manifest_sha256: str
    candidates: tuple[CleanupCandidate, ...]
    reclaimable_bytes: int = Field(ge=0)
    file_count: int = Field(ge=0)
    confirmation: str
    blocked_reason: str | None = None


class CleanupReceiptItem(BaseModel):
    """Integrity trail for one atomic source-to-quarantine move."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    root_id: str
    category: str
    source_relative_path: str
    quarantine_relative_path: str
    before: CleanupSnapshot
    quarantined: CleanupSnapshot | None = None
    restored: CleanupSnapshot | None = None
    purged: bool = False


class CleanupReceipt(BaseModel):
    """Durable and recoverable cleanup transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    receipt_id: str
    plan_id: str
    manifest_sha256: str
    state: Literal["moving", "quarantined", "restoring", "restored", "purging", "purged"]
    created_at: datetime
    restored_at: datetime | None = None
    purged_at: datetime | None = None
    purged_bytes: int = Field(default=0, ge=0)
    items: tuple[CleanupReceiptItem, ...]


def load_legacy_cleanup_manifest(path: Path) -> LegacyCleanupManifest:
    """Load a strict cleanup allowlist from YAML."""

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return LegacyCleanupManifest.model_validate(raw)
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise LegacyCleanupError(f"Invalid legacy cleanup manifest {path}: {error}") from error


def _hash_entry(path: Path) -> CleanupSnapshot:
    """Hash a file tree lexically without dereferencing any contained link."""

    if path.is_symlink():
        raise LegacyCleanupError(f"Refusing selected symlink cleanup target: {path}")
    try:
        root_mode = path.lstat().st_mode
    except OSError as error:
        raise LegacyCleanupError(f"Could not inspect cleanup target {path}: {error}") from error
    digest = hashlib.sha256()
    size_bytes = 0
    file_count = 0
    symlink_count = 0

    def add(label: str, payload: bytes) -> None:
        encoded = label.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    if stat.S_ISREG(root_mode):
        file_count = 1
        try:
            with path.open("rb") as source:
                content_digest = hashlib.sha256()
                while chunk := source.read(1024 * 1024):
                    size_bytes += len(chunk)
                    content_digest.update(chunk)
            add("file:.", f"{size_bytes}:{content_digest.hexdigest()}".encode())
        except OSError as error:
            raise LegacyCleanupError(f"Could not hash cleanup target {path}: {error}") from error
        return CleanupSnapshot(
            kind="file",
            size_bytes=size_bytes,
            file_count=file_count,
            symlink_count=0,
            sha256=digest.hexdigest(),
        )
    if not stat.S_ISDIR(root_mode):
        raise LegacyCleanupError(f"Cleanup target is not a regular file or directory: {path}")

    add("directory:.", b"")
    try:
        for current, directories, files in os.walk(path, topdown=True, followlinks=False):
            current_path = Path(current)
            relative_current = current_path.relative_to(path)
            safe_directories: list[str] = []
            for name in sorted(directories):
                child = current_path / name
                relative = (relative_current / name).as_posix()
                if child.is_symlink():
                    raise LegacyCleanupError(f"Refusing cleanup target containing symlink: {child}")
                else:
                    add(f"directory:{relative}", b"")
                    safe_directories.append(name)
            directories[:] = safe_directories
            for name in sorted(files):
                child = current_path / name
                relative = (relative_current / name).as_posix()
                if child.is_symlink():
                    raise LegacyCleanupError(f"Refusing cleanup target containing symlink: {child}")
                mode = child.lstat().st_mode
                if not stat.S_ISREG(mode):
                    raise LegacyCleanupError(f"Unsupported filesystem entry in target: {child}")
                content_digest = hashlib.sha256()
                child_size = 0
                with child.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        child_size += len(chunk)
                        content_digest.update(chunk)
                size_bytes += child_size
                file_count += 1
                add(f"file:{relative}", f"{child_size}:{content_digest.hexdigest()}".encode())
    except OSError as error:
        raise LegacyCleanupError(f"Could not hash cleanup target {path}: {error}") from error
    return CleanupSnapshot(
        kind="directory",
        size_bytes=size_bytes,
        file_count=file_count,
        symlink_count=symlink_count,
        sha256=digest.hexdigest(),
    )


class LegacyRuntimeCleanup:
    """Inspect, quarantine, and restore only manifest-approved legacy data."""

    def __init__(
        self,
        manifest: LegacyCleanupManifest,
        *,
        models_root: Path,
        runs_root: Path | None = None,
    ) -> None:
        self.manifest = manifest
        self.models_root = Path(os.path.abspath(models_root))
        self.runs_root = None if runs_root is None else Path(os.path.abspath(runs_root))
        self.records_root = self.models_root / ".whoopy-cleanup"
        self.quarantine_root = self.records_root / "quarantine"
        self.receipts_root = self.records_root / "receipts"
        self._lock_path = self.records_root / "cleanup.lock"
        self._manifest_sha256 = hashlib.sha256(
            json.dumps(
                manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_path(
        cls,
        manifest_path: Path,
        *,
        models_root: Path,
        runs_root: Path | None = None,
    ) -> LegacyRuntimeCleanup:
        return cls(
            load_legacy_cleanup_manifest(manifest_path),
            models_root=models_root,
            runs_root=runs_root,
        )

    @staticmethod
    def apply_confirmation(plan_id: str) -> str:
        return f"CLEANUP {plan_id}"

    @staticmethod
    def restore_confirmation(receipt_id: str) -> str:
        return f"RESTORE {receipt_id}"

    @staticmethod
    def purge_confirmation(receipt_id: str) -> str:
        return f"PURGE {receipt_id}"

    def inspect(self) -> CleanupPlan:
        """Hash current candidates and return a non-mutating dry-run plan."""

        return self._build_plan(check_active=True)

    def _build_plan(self, *, check_active: bool) -> CleanupPlan:
        """Build a plan, optionally probing the heavyweight runtime slot."""

        candidates: list[CleanupCandidate] = []
        selected_paths: set[Path] = set()
        for root in self.manifest.roots:
            runtime_root = self._safe_source(root.relative_path, label="legacy runtime root")
            if not runtime_root.exists():
                continue
            if not runtime_root.is_dir() or runtime_root.is_symlink():
                raise LegacyCleanupError(f"Legacy runtime root is unsafe: {runtime_root}")
            for target in root.targets:
                matches = (
                    [runtime_root / target.path]
                    if target.path is not None
                    else sorted(runtime_root.glob(target.glob or ""))
                )
                for candidate in matches:
                    if not candidate.exists() and not candidate.is_symlink():
                        continue
                    checked = self._safe_source(
                        candidate.relative_to(self.models_root).as_posix(),
                        label="cleanup candidate",
                    )
                    if checked in selected_paths:
                        continue
                    # Never admit a nested target when its ancestor is already
                    # selected. Manifest validation separately protects checkpoints.
                    if any(parent in selected_paths for parent in checked.parents):
                        continue
                    snapshot = _hash_entry(checked)
                    selected_paths.add(checked)
                    candidates.append(
                        CleanupCandidate(
                            root_id=root.root_id,
                            category=target.category,
                            source_relative_path=checked.relative_to(self.models_root).as_posix(),
                            snapshot=snapshot,
                        )
                    )
        candidates.sort(key=lambda item: item.source_relative_path)
        identity = {
            "manifest_sha256": self._manifest_sha256,
            "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
        }
        plan_id = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        blocked_reason = self._active_use_reason(check_heavyweight=check_active)
        return CleanupPlan(
            plan_id=plan_id,
            manifest_sha256=self._manifest_sha256,
            candidates=tuple(candidates),
            reclaimable_bytes=sum(item.snapshot.size_bytes for item in candidates),
            file_count=sum(item.snapshot.file_count for item in candidates),
            confirmation=self.apply_confirmation(plan_id),
            blocked_reason=blocked_reason,
        )

    def apply(self, *, confirmation: str) -> CleanupReceipt:
        """Atomically quarantine the exact current dry-run candidates."""

        lock = _AdvisoryFileLock(self._lock_path, safety_root=self.models_root)
        if not lock.acquire(timeout_seconds=5):
            raise LegacyCleanupError("Another legacy cleanup operation is active.")
        slot = HeavyweightModelSlot(self.models_root, "legacy-cleanup")
        try:
            try:
                slot.acquire(timeout_seconds=0)
            except HeavyweightModelSlotUnavailable as error:
                raise LegacyCleanupError(
                    "Cleanup is blocked while a heavyweight model runtime is active."
                ) from error
            plan = self._build_plan(check_active=False)
            expected = self.apply_confirmation(plan.plan_id)
            if confirmation != expected:
                raise LegacyCleanupError(f"Explicit confirmation required: {expected}")
            if plan.blocked_reason is not None:
                raise LegacyCleanupError(plan.blocked_reason)
            if not plan.candidates:
                raise LegacyCleanupError("No manifest-approved legacy cleanup candidates exist.")
            receipt_id = str(uuid.uuid4())
            items = tuple(
                CleanupReceiptItem(
                    root_id=candidate.root_id,
                    category=candidate.category,
                    source_relative_path=candidate.source_relative_path,
                    quarantine_relative_path=(
                        Path(".whoopy-cleanup")
                        / "quarantine"
                        / receipt_id
                        / "payload"
                        / candidate.source_relative_path
                    ).as_posix(),
                    before=candidate.snapshot,
                )
                for candidate in plan.candidates
            )
            receipt = CleanupReceipt(
                receipt_id=receipt_id,
                plan_id=plan.plan_id,
                manifest_sha256=plan.manifest_sha256,
                state="moving",
                created_at=datetime.now(UTC),
                items=items,
            )
            self._write_receipt(receipt)
            for index, item in enumerate(receipt.items):
                source = self._safe_source(item.source_relative_path, label="cleanup source")
                destination = self._safe_quarantine(
                    item.quarantine_relative_path, label="cleanup quarantine destination"
                )
                current = _hash_entry(source)
                if current != item.before:
                    raise LegacyCleanupError(
                        f"Cleanup source changed after confirmation: {item.source_relative_path}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                safe_managed_path(self.models_root, destination.parent, label="quarantine parent")
                if destination.exists() or destination.is_symlink():
                    raise LegacyCleanupError(
                        f"Quarantine destination already exists: {destination}"
                    )
                source.replace(destination)
                after = _hash_entry(destination)
                if after != item.before:
                    if not source.exists():
                        destination.replace(source)
                    raise LegacyCleanupError(
                        f"Integrity check failed after quarantining {item.source_relative_path}"
                    )
                updated_items = list(receipt.items)
                updated_items[index] = item.model_copy(update={"quarantined": after})
                receipt = receipt.model_copy(update={"items": tuple(updated_items)})
                self._write_receipt(receipt)
            receipt = receipt.model_copy(update={"state": "quarantined"})
            self._write_receipt(receipt)
            return receipt
        finally:
            slot.release()
            lock.release()

    def restore(self, receipt_id: str, *, confirmation: str) -> CleanupReceipt:
        """Restore every quarantined item and verify its original identity."""

        expected = self.restore_confirmation(receipt_id)
        if confirmation != expected:
            raise LegacyCleanupError(f"Explicit confirmation required: {expected}")
        lock = _AdvisoryFileLock(self._lock_path, safety_root=self.models_root)
        if not lock.acquire(timeout_seconds=5):
            raise LegacyCleanupError("Another legacy cleanup operation is active.")
        slot = HeavyweightModelSlot(self.models_root, "legacy-cleanup")
        try:
            try:
                slot.acquire(timeout_seconds=0)
            except HeavyweightModelSlotUnavailable as error:
                raise LegacyCleanupError(
                    "Cleanup is blocked while a heavyweight model runtime is active."
                ) from error
            reason = self._active_use_reason(check_heavyweight=False)
            if reason is not None:
                raise LegacyCleanupError(reason)
            receipt = self._read_receipt(receipt_id)
            if receipt.state in {"purging", "purged"}:
                raise LegacyCleanupError(
                    f"Cleanup receipt {receipt_id} has been irreversibly purged."
                )
            if receipt.state == "restored":
                return receipt
            receipt = receipt.model_copy(update={"state": "restoring"})
            self._write_receipt(receipt)
            for index, item in enumerate(receipt.items):
                if item.restored is not None:
                    continue
                if item.quarantined is None:
                    # A crash may have happened after rename but before receipt
                    # update. Presence and the original digest resolve that state.
                    possible = self._safe_quarantine(
                        item.quarantine_relative_path, label="cleanup quarantine payload"
                    )
                    if not possible.exists():
                        continue
                source = self._safe_quarantine(
                    item.quarantine_relative_path, label="cleanup quarantine payload"
                )
                destination = self._safe_source(
                    item.source_relative_path, label="cleanup restore destination"
                )
                if destination.exists() or destination.is_symlink():
                    raise LegacyCleanupError(
                        f"Cannot restore over existing path: {item.source_relative_path}"
                    )
                current = _hash_entry(source)
                if current != item.before:
                    raise LegacyCleanupError(
                        "Quarantined content failed receipt verification: "
                        f"{item.source_relative_path}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                safe_managed_path(self.models_root, destination.parent, label="restore parent")
                source.replace(destination)
                restored = _hash_entry(destination)
                if restored != item.before:
                    if not source.exists():
                        destination.replace(source)
                    raise LegacyCleanupError(
                        f"Integrity check failed after restoring {item.source_relative_path}"
                    )
                updated_items = list(receipt.items)
                updated_items[index] = item.model_copy(update={"restored": restored})
                receipt = receipt.model_copy(update={"items": tuple(updated_items)})
                self._write_receipt(receipt)
            receipt = receipt.model_copy(
                update={"state": "restored", "restored_at": datetime.now(UTC)}
            )
            self._write_receipt(receipt)
            return receipt
        finally:
            slot.release()
            lock.release()

    def purge(self, receipt_id: str, *, confirmation: str) -> CleanupReceipt:
        """Irreversibly delete a verified quarantine payload and retain its receipt."""

        expected = self.purge_confirmation(receipt_id)
        if confirmation != expected:
            raise LegacyCleanupError(
                f"Explicit irreversible purge confirmation required: {expected}"
            )
        lock = _AdvisoryFileLock(self._lock_path, safety_root=self.models_root)
        if not lock.acquire(timeout_seconds=5):
            raise LegacyCleanupError("Another legacy cleanup operation is active.")
        slot = HeavyweightModelSlot(self.models_root, "legacy-cleanup")
        try:
            try:
                slot.acquire(timeout_seconds=0)
            except HeavyweightModelSlotUnavailable as error:
                raise LegacyCleanupError(
                    "Cleanup is blocked while a heavyweight model runtime is active."
                ) from error
            reason = self._active_use_reason(check_heavyweight=False)
            if reason is not None:
                raise LegacyCleanupError(reason)
            receipt = self._read_receipt(receipt_id)
            if receipt.state == "purged":
                return receipt
            if receipt.state not in {"quarantined", "purging"}:
                raise LegacyCleanupError(
                    "Only a fully quarantined cleanup receipt can be purged; "
                    f"receipt {receipt_id} is {receipt.state}."
                )
            if any(item.quarantined is None for item in receipt.items):
                raise LegacyCleanupError(
                    f"Cleanup receipt {receipt_id} is missing quarantine integrity evidence."
                )
            if receipt.state == "quarantined":
                # Verify the entire transaction before deleting its first byte.
                for item in receipt.items:
                    payload = self._safe_quarantine(
                        item.quarantine_relative_path,
                        label="cleanup purge payload",
                    )
                    if _hash_entry(payload) != item.before:
                        raise LegacyCleanupError(
                            "Quarantined content failed receipt verification: "
                            f"{item.source_relative_path}"
                        )
                receipt = receipt.model_copy(update={"state": "purging"})
                self._write_receipt(receipt)
            for index, item in enumerate(receipt.items):
                if item.purged:
                    continue
                payload = self._safe_quarantine(
                    item.quarantine_relative_path,
                    label="cleanup purge payload",
                )
                if payload.exists() or payload.is_symlink():
                    if _hash_entry(payload) != item.before:
                        raise LegacyCleanupError(
                            "Quarantined content failed receipt verification: "
                            f"{item.source_relative_path}"
                        )
                    if payload.is_dir():
                        shutil.rmtree(payload)
                    else:
                        payload.unlink()
                # In a durable `purging` transaction, a missing item means the
                # prior process stopped after deletion but before receipt update.
                if payload.exists() or payload.is_symlink():
                    raise LegacyCleanupError(f"Could not purge quarantine payload: {payload}")
                updated_items = list(receipt.items)
                updated_items[index] = item.model_copy(update={"purged": True})
                receipt = receipt.model_copy(update={"items": tuple(updated_items)})
                self._write_receipt(receipt)
            receipt = receipt.model_copy(
                update={
                    "state": "purged",
                    "purged_at": datetime.now(UTC),
                    "purged_bytes": sum(item.before.size_bytes for item in receipt.items),
                }
            )
            self._write_receipt(receipt)
            return receipt
        finally:
            slot.release()
            lock.release()

    def receipts(self) -> tuple[CleanupReceipt, ...]:
        """List all durable receipts without touching quarantined content."""

        if not self.receipts_root.exists():
            return ()
        safe_managed_path(self.models_root, self.receipts_root, label="cleanup receipts root")
        if not self.receipts_root.is_dir() or self.receipts_root.is_symlink():
            raise LegacyCleanupError("Cleanup receipts root is unsafe.")
        return tuple(
            self._read_receipt(path.stem) for path in sorted(self.receipts_root.glob("*.json"))
        )

    def _active_use_reason(self, *, check_heavyweight: bool = True) -> str | None:
        heavyweight = self.models_root / "runtime-slot" / "heavyweight.lock"
        if check_heavyweight and heavyweight.exists():
            probe = _AdvisoryFileLock(heavyweight, safety_root=self.models_root)
            if not probe.acquire(timeout_seconds=0):
                return "Cleanup is blocked while a heavyweight model runtime is active."
            probe.release()
        if self.runs_root is not None and self.runs_root.is_dir():
            safe_runs = Path(os.path.abspath(self.runs_root))
            for worker_lock in sorted(safe_runs.glob("*/worker.lock")):
                safe_managed_path(safe_runs, worker_lock, label="generation worker lock")
                probe = _AdvisoryFileLock(worker_lock, safety_root=safe_runs)
                if not probe.acquire(timeout_seconds=0):
                    return (
                        "Cleanup is blocked while generation run "
                        f"{worker_lock.parent.name} is active."
                    )
                probe.release()
        return None

    def _safe_source(self, relative: str, *, label: str) -> Path:
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or not parsed.parts or ".." in parsed.parts:
            raise LegacyCleanupError(f"Unsafe {label}: {relative}")
        try:
            return safe_managed_path(self.models_root, self.models_root / parsed, label=label)
        except ModelPackOperationError as error:
            raise LegacyCleanupError(str(error)) from error

    def _safe_quarantine(self, relative: str, *, label: str) -> Path:
        candidate = self._safe_source(relative, label=label)
        try:
            candidate.relative_to(self.quarantine_root)
        except ValueError as error:
            raise LegacyCleanupError(f"Unsafe {label}: {relative}") from error
        return candidate

    def _receipt_path(self, receipt_id: str) -> Path:
        try:
            uuid.UUID(receipt_id)
        except ValueError as error:
            raise LegacyCleanupError(f"Invalid cleanup receipt ID: {receipt_id}") from error
        return self.receipts_root / f"{receipt_id}.json"

    def _write_receipt(self, receipt: CleanupReceipt) -> None:
        _atomic_json(
            self._receipt_path(receipt.receipt_id),
            receipt.model_dump(mode="json"),
            safety_root=self.models_root,
        )

    def _read_receipt(self, receipt_id: str) -> CleanupReceipt:
        path = self._receipt_path(receipt_id)
        try:
            safe_managed_path(self.models_root, path, label="cleanup receipt")
            receipt = CleanupReceipt.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise LegacyCleanupError(f"Invalid cleanup receipt {receipt_id}: {error}") from error
        if receipt.receipt_id != receipt_id:
            raise LegacyCleanupError(f"Cleanup receipt identity mismatch: {receipt_id}")
        for item in receipt.items:
            self._safe_source(item.source_relative_path, label="receipt source")
            self._safe_quarantine(item.quarantine_relative_path, label="receipt quarantine path")
            expected_quarantine = (
                Path(".whoopy-cleanup")
                / "quarantine"
                / receipt_id
                / "payload"
                / item.source_relative_path
            ).as_posix()
            if item.quarantine_relative_path != expected_quarantine:
                raise LegacyCleanupError(
                    f"Cleanup receipt {receipt_id} has mismatched quarantine metadata."
                )
            if not self._receipt_item_is_allowlisted(item):
                raise LegacyCleanupError(
                    f"Cleanup receipt {receipt_id} contains a path outside the allowlist."
                )
        return receipt

    def _receipt_item_is_allowlisted(self, item: CleanupReceiptItem) -> bool:
        """Revalidate receipt paths against the current manifest's strict selectors."""

        for root in self.manifest.roots:
            if root.root_id != item.root_id:
                continue
            root_path = PurePosixPath(root.relative_path)
            source_path = PurePosixPath(item.source_relative_path)
            try:
                relative = source_path.relative_to(root_path)
            except ValueError:
                return False
            for target in root.targets:
                if target.category != item.category:
                    continue
                if target.path is not None and relative == PurePosixPath(target.path):
                    return True
                if target.glob is not None and relative.match(target.glob):
                    return True
            return False
        return False
