"""Cross-platform advisory locks for one local run directory."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, BinaryIO, cast


class RunLockUnavailable(RuntimeError):
    """Raised when another process already owns a run lock."""


class RunLock:
    """Hold an OS lock that the kernel releases automatically on process exit."""

    def __init__(self, run_directory: Path, *, require_directory: bool = True) -> None:
        self.run_directory = run_directory
        self.require_directory = require_directory
        self.path = run_directory.parent / ".locks" / f"{run_directory.name}.lock"
        self._file: BinaryIO | None = None

    def acquire(self) -> None:
        """Acquire without waiting so duplicate workers fail predictably."""

        if self._file is not None:
            raise RuntimeError(f"Run lock is already held: {self.path}")
        lock_file: BinaryIO | None = None
        try:
            self.path.parent.mkdir(exist_ok=True)
            if self.path.parent.is_symlink() or self.path.is_symlink():
                raise OSError("Run lock paths must not be symbolic links")
            lock_file = self.path.open("a+b")
            if os.name == "nt":
                self._acquire_windows(lock_file)
            else:
                self._acquire_posix(lock_file)
            if self.require_directory and not self.run_directory.is_dir():
                raise FileNotFoundError(f"Run directory not found: {self.run_directory}")
        except (OSError, BlockingIOError) as error:
            if lock_file is not None:
                lock_file.close()
            raise RunLockUnavailable(
                f"Another worker is processing {self.run_directory.name}, "
                "or its run directory is unavailable"
            ) from error
        assert lock_file is not None
        self._file = lock_file

    @contextmanager
    def reuse(self, run_directory: Path) -> Iterator[RunLock]:
        if self._file is None or self.run_directory != run_directory:
            raise RuntimeError("The caller does not hold this run's lock.")
        yield self

    def release(self) -> None:
        """Release the advisory lock while leaving the harmless lock file."""

        lock_file = self._file
        if lock_file is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                lock_file.seek(0)
                cast(Any, msvcrt).locking(
                    lock_file.fileno(),
                    cast(Any, msvcrt).LK_UNLCK,
                    1,
                )
            else:
                import fcntl

                cast(Any, fcntl).flock(
                    lock_file.fileno(),
                    cast(Any, fcntl).LOCK_UN,
                )
        finally:
            self._file = None
            lock_file.close()

    @staticmethod
    def _acquire_posix(lock_file: BinaryIO) -> None:
        import fcntl

        cast(Any, fcntl).flock(
            lock_file.fileno(),
            cast(Any, fcntl).LOCK_EX | cast(Any, fcntl).LOCK_NB,
        )

    @staticmethod
    def _acquire_windows(lock_file: BinaryIO) -> None:
        import msvcrt

        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        cast(Any, msvcrt).locking(
            lock_file.fileno(),
            cast(Any, msvcrt).LK_NBLCK,
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
