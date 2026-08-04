"""Cross-platform advisory locks for one local run directory."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import BinaryIO


class RunLockUnavailable(RuntimeError):
    """Raised when another process already owns a run lock."""


class RunLock:
    """Hold an OS lock that the kernel releases automatically on process exit."""

    def __init__(self, run_directory: Path) -> None:
        self.path = run_directory / "worker.lock"
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        """Acquire without waiting so duplicate workers fail predictably."""

        if self._file is not None:
            raise RuntimeError(f"Run lock is already held: {self.path}")
        lock_file: BinaryIO | None = None
        try:
            lock_file = self.path.open("a+b")
            if os.name == "nt":
                self._acquire_windows(lock_file)
            else:
                self._acquire_posix(lock_file)
        except (OSError, BlockingIOError) as error:
            if lock_file is not None:
                lock_file.close()
            raise RunLockUnavailable(
                f"Another worker is processing {self.path.parent.name}"
            ) from error
        assert lock_file is not None
        self._file = lock_file

    def release(self) -> None:
        """Release the advisory lock while leaving the harmless lock file."""

        lock_file = self._file
        if lock_file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                msvcrt.locking(  # type: ignore[attr-defined]
                    lock_file.fileno(),
                    msvcrt.LK_UNLCK,  # type: ignore[attr-defined]
                    1,
                )
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file = None
            lock_file.close()

    @staticmethod
    def _acquire_posix(lock_file: BinaryIO) -> None:
        import fcntl

        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _acquire_windows(lock_file: BinaryIO) -> None:
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(  # type: ignore[attr-defined]
            lock_file.fileno(),
            msvcrt.LK_NBLCK,  # type: ignore[attr-defined]
            1,
        )

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(
        self,
        _error_type: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.release()
