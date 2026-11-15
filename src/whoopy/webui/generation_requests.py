"""Durable identity and cancellation for optional browser Create request UUIDs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from whoopy.model_packs.operations import safe_managed_path
from whoopy.pipeline import RunRecord, RunStore
from whoopy.pipeline.locks import RunLock, RunLockUnavailable

CLIENT_RECEIPT = "web-client-request.json"


def canonical(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)


class GenerationRequestError(ValueError):
    def __init__(self, request_id: str, code: str, message: str, *, status: int = 409) -> None:
        super().__init__(message)
        self.status = status
        self.payload: dict[str, Any] = {"error": message, "code": code, "request_id": request_id}
        if status == 410:
            self.payload["run_id"] = request_id


class GenerationRequests:
    """A small immutable ledger, independent of whether a run is later trashed."""

    def __init__(self, runs_root: Path) -> None:
        self.runs_root = runs_root
        self.root = runs_root / ".web-requests"

    def directory(self, request_id: str) -> Path:
        return safe_managed_path(self.runs_root, self.root / str(UUID(request_id)))

    @contextmanager
    def lock(self, request_id: str) -> Iterator[None]:
        safe_managed_path(self.runs_root, self.root).mkdir(parents=True, exist_ok=True)
        RunStore._fsync_directory(self.runs_root)
        lock = RunLock(self.directory(request_id), require_directory=False)
        deadline = time.monotonic() + 2
        while True:
            try:
                lock.acquire()
                break
            except RunLockUnavailable as error:
                if time.monotonic() >= deadline:
                    raise GenerationRequestError(
                        request_id,
                        "request_in_progress",
                        "This request is being registered; check it again.",
                    ) from error
                time.sleep(0.02)
        try:
            yield
        finally:
            lock.release()

    @staticmethod
    def read(path: Path) -> dict[str, Any]:
        if path.is_symlink() or path.stat().st_size > 256_000:
            raise ValueError("Unsafe or oversized browser request receipt.")
        value = json.loads(
            path.read_text(encoding="utf-8"),
            parse_constant=lambda _x: (_ for _ in ()).throw(ValueError("Nonfinite receipt")),
        )
        if not isinstance(value, dict):
            raise ValueError("Invalid browser request receipt.")
        return value

    @staticmethod
    def write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        RunStore._fsync_directory(path.parent.parent)
        # Callers own the per-request OS lock. Publish flushed complete bytes atomically.
        if path.exists() or path.is_symlink():
            raise FileExistsError(f"Browser request evidence already exists: {path}")
        RunStore._write_json(
            path,
            json.dumps(
                payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
            )
            + "\n",
        )
        path.chmod(0o444)

    def register(self, request_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.cancelled(request_id):
            raise GenerationRequestError(
                request_id,
                "request_cancelled",
                "This Create request was cancelled; use a new request ID.",
            )
        receipt = {
            "schema_version": 1,
            "request_id": request_id,
            "hash_version": 1,
            "payload_sha256": hashlib.sha256(canonical(payload).encode()).hexdigest(),
            "payload": payload,
        }
        path = self.directory(request_id) / "request.json"
        if path.exists():
            if self.read(path) != receipt:
                raise GenerationRequestError(
                    request_id,
                    "request_id_conflict",
                    "This request ID already belongs to different generation settings.",
                )
        else:
            self.write(path, receipt)
        return receipt

    def cancelled(self, request_id: str) -> bool:
        path = self.directory(request_id) / "cancel.json"
        return path.exists() or path.is_symlink()

    def cancel(self, request_id: str) -> bool:
        """Return whether this is the first acknowledgement, before any run is needed."""
        if self.cancelled(request_id):
            return False
        self.write(
            self.directory(request_id) / "cancel.json",
            {"schema_version": 1, "request_id": request_id, "cancelled": True},
        )
        return True

    def metadata(self, request_id: str) -> dict[str, Any]:
        path = self.directory(request_id) / "request.json"
        if not path.is_file():
            return {}
        receipt = self.read(path)
        payload = receipt.get("payload")
        if (
            receipt.get("schema_version") != 1
            or receipt.get("hash_version") != 1
            or receipt.get("request_id") != request_id
            or not isinstance(payload, dict)
            or receipt.get("payload_sha256")
            != hashlib.sha256(canonical(payload).encode()).hexdigest()
        ):
            raise ValueError("Browser request identity is inconsistent.")
        run_receipt = safe_managed_path(
            self.runs_root, self.runs_root / request_id / CLIENT_RECEIPT
        )
        if self.read(run_receipt) != receipt:
            raise ValueError("Run and browser request identities differ.")
        return {
            "request_id": request_id,
            "request_payload_sha256": receipt["payload_sha256"],
            "request_hash_version": 1,
        }

    def existing(self, receipt: dict[str, Any]) -> bool:
        request_id = receipt["request_id"]
        admitted = self.directory(request_id) / "admitted.json"
        run = safe_managed_path(self.runs_root, self.runs_root / request_id)
        if admitted.exists():
            value = self.read(admitted)
            if (
                value.get("schema_version") != 1
                or value.get("request_id") != request_id
                or value.get("run_id") != request_id
                or value.get("payload_sha256") != receipt["payload_sha256"]
            ):
                raise ValueError("Browser admission receipt is inconsistent.")
            if not (run / "run.json").is_file():
                raise GenerationRequestError(
                    request_id,
                    "request_run_deleted",
                    "The original run is no longer available; this request will not recreate it.",
                    status=410,
                )
        if not run.exists():
            return False
        if not (run / CLIENT_RECEIPT).is_file() or self.read(run / CLIENT_RECEIPT) != receipt:
            raise GenerationRequestError(
                request_id, "request_id_conflict", "This UUID already belongs to another run."
            )
        # Repair a crash after atomic run publication but before the mapping commit.
        if not admitted.exists():
            self.admit(receipt)
        return True

    def admit(self, receipt: dict[str, Any]) -> None:
        self.write(
            self.directory(receipt["request_id"]) / "admitted.json",
            {
                "schema_version": 1,
                "request_id": receipt["request_id"],
                "run_id": receipt["request_id"],
                "payload_sha256": receipt["payload_sha256"],
            },
        )

    def create_run(
        self, receipt: dict[str, Any], launch: dict[str, Any], *, audience: Literal["studio", "lab"]
    ) -> RunRecord:
        request_id = receipt["request_id"]
        staging = self.directory(request_id) / f".staging-{uuid4().hex}"
        try:
            record = RunStore(staging).create(
                launch["text"], run_id=UUID(request_id), audience=audience
            )
            directory = staging / request_id
            self.write(directory / CLIENT_RECEIPT, receipt)
            self.write(
                directory / "web-launch-request.json",
                {
                    "schema_version": 1,
                    "payload_sha256": hashlib.sha256(canonical(launch).encode()).hexdigest(),
                    "payload": launch,
                },
            )
            target = safe_managed_path(self.runs_root, self.runs_root / request_id)
            if target.exists():
                raise GenerationRequestError(
                    request_id, "request_id_conflict", "This UUID already belongs to another run."
                )
            directory.rename(target)
            RunStore._fsync_directory(self.runs_root)
            self.admit(receipt)
            return record
        finally:

            def remove_readonly(function: Any, path: str, _error: Any) -> None:
                # This is only the unpublished, owned transaction staging tree.
                os.chmod(path, 0o700)
                function(path)

            if staging.exists():
                shutil.rmtree(staging, onerror=remove_readonly)
