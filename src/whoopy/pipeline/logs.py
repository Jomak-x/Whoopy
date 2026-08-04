"""Thread-safe, size-bounded diagnostic files kept with a durable run."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Final
from uuid import UUID

from whoopy.pipeline.runs import RunStore

DEFAULT_LOG_MAX_BYTES: Final = 5 * 1024 * 1024
"""Maximum size of the active diagnostic file before it rotates."""

WORKER_LOG_FILENAME: Final = "worker.log"

_locks_guard = threading.Lock()
_path_locks: dict[Path, threading.RLock] = {}


def _path_lock(path: Path) -> threading.RLock:
    """Return the process-local lock shared by all writers for ``path``."""

    resolved = path.resolve()
    with _locks_guard:
        return _path_locks.setdefault(resolved, threading.RLock())


class RotatingRunLog:
    """Append complete lines and retain the immediately preceding file on rotation.

    Run workers hold an OS-level ``RunLock``.  This class additionally serializes
    writers in one Python process so a web request and worker thread cannot lose a
    line between reading and replacing the active file.
    """

    def __init__(
        self,
        store: RunStore,
        *,
        filename: str,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    ) -> None:
        if not filename or Path(filename).name != filename:
            raise ValueError("log filename must be a plain filename")
        if max_bytes < 1:
            raise ValueError("log size bound must be positive")
        self.store = store
        self.filename = filename
        self.max_bytes = max_bytes

    def path(self, run_id: UUID) -> Path:
        """Return the active diagnostic path without creating a run directory."""

        return self.store.run_directory(run_id) / self.filename

    def append_line(self, run_id: UUID, line: bytes) -> None:
        """Append one newline-terminated record, rotating before it exceeds the bound."""

        if not line.endswith(b"\n"):
            raise ValueError("log records must end with a newline")
        if len(line) > self.max_bytes:
            raise ValueError("one log record exceeds the configured size bound")

        path = self.path(run_id)
        if not path.parent.is_dir():
            raise FileNotFoundError(f"Run directory not found: {path.parent}")
        backup = path.with_name(f"{path.name}.1")
        with _path_lock(path):
            try:
                current = path.read_bytes()
            except FileNotFoundError:
                current = b""
            if current and len(current) + len(line) > self.max_bytes:
                # ``replace`` atomically promotes the complete active file to the
                # sole backup; replacing an older backup is intentional.
                path.replace(backup)
                self.store._write_bytes(path, line)
            else:
                self.store._write_bytes(path, current + line)


class WorkerLog:
    """Human-readable, bounded worker diagnostics for model failures and lifecycle events."""

    def __init__(
        self,
        store: RunStore,
        *,
        max_bytes: int = DEFAULT_LOG_MAX_BYTES,
        maximum_line_characters: int = 4_000,
    ) -> None:
        if maximum_line_characters < 1:
            raise ValueError("worker log line bound must be positive")
        self._log = RotatingRunLog(
            store,
            filename=WORKER_LOG_FILENAME,
            max_bytes=max_bytes,
        )
        self.maximum_line_characters = maximum_line_characters

    def path(self, run_id: UUID) -> Path:
        """Return the active worker log path for inspection or test assertions."""

        return self._log.path(run_id)

    def record(
        self,
        run_id: UUID,
        *,
        occurred_at: datetime,
        source: str,
        message: str,
        level: str = "INFO",
    ) -> None:
        """Append one timestamped diagnostic line with bounded, single-line text."""

        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("worker log timestamps must include a timezone")
        normalized_source = " ".join(source.split()) or "worker"
        normalized_level = " ".join(level.split()).upper() or "INFO"
        normalized_message = " ".join(message.split()) or "<empty>"
        line = (
            f"{occurred_at.astimezone(UTC).isoformat()} "
            f"{normalized_level} [{normalized_source}] {normalized_message}"
        )[: self.maximum_line_characters]
        self._log.append_line(run_id, (line + "\n").encode("utf-8"))
