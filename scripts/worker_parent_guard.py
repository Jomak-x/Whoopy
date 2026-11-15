"""Standard-library parent watching and an owned-process guardian; no model imports."""

from __future__ import annotations

import argparse
import hashlib
import os
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

PARENT_PID = "WHOOPY_PARENT_PID"
PARENT_TOKEN = "WHOOPY_PARENT_CREATION_TOKEN"
HELPER_SHA256 = "WHOOPY_PARENT_GUARD_SHA256"


def _windows_parent(pid: int) -> tuple[Any, Any, str]:
    """Retain a kernel handle, so a reused PID cannot become the watched parent."""
    import ctypes
    from ctypes import wintypes

    kernel = vars(ctypes)["WinDLL"]("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000 | 0x1000, False, pid)
    if not handle:
        raise OSError("Cannot open the actual parent process for watching")
    creation, exit_time, system, user = (wintypes.FILETIME() for _ in range(4))
    if not kernel.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(system),
        ctypes.byref(user),
    ):
        kernel.CloseHandle(handle)
        raise OSError("Cannot verify the actual parent process creation identity")
    token = str((int(creation.dwHighDateTime) << 32) | int(creation.dwLowDateTime))
    return kernel, handle, token


def _validated_windows_parent(pid: int, expected_token: str | None) -> tuple[Any, Any]:
    kernel, handle, actual = _windows_parent(pid)
    if expected_token is not None and expected_token != actual:
        kernel.CloseHandle(handle)
        raise RuntimeError("Worker launch parent creation identity does not match")
    return kernel, handle


def launch_environment() -> dict[str, str]:
    """Call in the process that will actually create the guarded child."""
    token = ""
    if os.name == "nt":
        kernel, handle, token = _windows_parent(os.getpid())
        kernel.CloseHandle(handle)
    return {
        PARENT_PID: str(os.getpid()),
        PARENT_TOKEN: token,
        HELPER_SHA256: hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


class ParentWatch:
    """Exit only this worker when its actual launch parent disappears."""

    def __init__(
        self,
        parent_pid: int,
        creation_token: str | None = None,
        *,
        on_death: Callable[[], None] | None = None,
        poll_seconds: float = 0.1,
    ) -> None:
        if parent_pid <= 0 or parent_pid == os.getpid() or os.getppid() != parent_pid:
            raise RuntimeError("Worker launch parent identity does not match")
        self.parent_pid = parent_pid
        self._kernel: Any = None
        self._handle: Any = None
        if os.name == "nt":
            self._kernel, self._handle = _validated_windows_parent(parent_pid, creation_token)
        self._stop = threading.Event()
        self._on_death = on_death
        self._poll_seconds = poll_seconds
        self._thread = threading.Thread(target=self._run, name="whoopy-parent-watch", daemon=True)
        self._thread.start()

    def alive(self) -> bool:
        if self._handle is None:
            return os.getppid() == self.parent_pid
        state = self._kernel.WaitForSingleObject(self._handle, 0)
        if state not in (0, 258):
            raise OSError("Parent process handle observation failed")
        return bool(state == 258)

    def _run(self) -> None:
        try:
            while not self._stop.wait(self._poll_seconds):
                try:
                    alive = self.alive()
                except Exception:
                    alive = False
                if not alive:
                    with suppress(OSError):
                        os.write(
                            2, b"Whoopy parent guard: launch parent exited; stopping owned work.\n"
                        )
                    if self._on_death is not None:
                        self._on_death()
                    else:
                        os._exit(75)
                    return
        finally:
            if self._handle is not None:
                self._kernel.CloseHandle(self._handle)
                self._handle = None

    def close(self) -> None:
        self._stop.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)


def start_parent_watch(*, on_death: Callable[[], None] | None = None) -> ParentWatch:
    supplied = os.environ.get(PARENT_PID)
    token = os.environ.get(PARENT_TOKEN) or None
    if supplied is not None and os.name == "nt" and token is None:
        raise RuntimeError("Managed Windows worker has no launch-parent creation identity")
    return ParentWatch(
        int(supplied) if supplied is not None else os.getppid(), token, on_death=on_death
    )


def _stop_owned(process: subprocess.Popen[bytes]) -> None:
    """Use only the live child this guardian created, never persisted PID records."""
    if os.name == "posix":
        with suppress(ProcessLookupError):
            cast(Any, os).killpg(process.pid, signal.SIGTERM)
    elif process.poll() is None:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with suppress(ProcessLookupError):
                cast(Any, os).killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        else:
            process.kill()
        process.wait(timeout=2)
    # A child can exit while descendants in its still-owned process group remain.
    if os.name == "posix":
        with suppress(ProcessLookupError):
            cast(Any, os).killpg(process.pid, getattr(signal, "SIGKILL", signal.SIGTERM))


def guardian(command: list[str], timeout_seconds: float) -> int:
    """Supervise external llama without importing its runtime or altering its streams."""
    stopped = threading.Event()
    parent_lost = threading.Event()
    previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}

    def stop(_number: int, _frame: Any) -> None:
        stopped.set()

    for number in previous:
        signal.signal(number, stop)
    watch = start_parent_watch(on_death=parent_lost.set)
    process: subprocess.Popen[bytes] | None = None
    started = time.monotonic()
    try:
        if not watch.alive():
            return 75
        process = subprocess.Popen(
            command,
            start_new_session=os.name == "posix",
            creationflags=(
                int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200))
                if os.name == "nt"
                else 0
            ),
        )
        while process.poll() is None:
            if (
                parent_lost.is_set()
                or stopped.is_set()
                or time.monotonic() - started >= timeout_seconds
            ):
                _stop_owned(process)
                return 75 if parent_lost.is_set() else 130 if stopped.is_set() else 124
            time.sleep(0.02)
        return process.returncode
    finally:
        if process is not None:
            _stop_owned(process)
        watch.close()
        for number, handler in previous.items():
            signal.signal(number, handler)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guardian-timeout", type=float, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command or args.guardian_timeout <= 0:
        parser.error("A command and positive guardian timeout are required")
    return guardian(command, args.guardian_timeout)


if __name__ == "__main__":
    code = main()
    if code < 0 and os.name == "posix":
        signal.signal(-code, signal.SIG_DFL)
        os.kill(os.getpid(), -code)
    raise SystemExit(code)
