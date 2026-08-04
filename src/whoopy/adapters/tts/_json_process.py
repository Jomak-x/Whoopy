"""Bounded lifecycle controller for isolated JSON-lines model workers."""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import TextIO


class WorkerProcessError(RuntimeError):
    """Base class for failures at the private worker-process boundary."""


class WorkerStartupError(WorkerProcessError):
    """The worker could not establish a usable protocol session."""


class WorkerRequestError(WorkerProcessError):
    """An established worker failed while serving one request."""


class WorkerProtocolError(WorkerProcessError):
    """The worker emitted malformed or mismatched protocol data."""


class WorkerTimeoutError(WorkerProcessError):
    """A bounded worker operation did not finish in time."""


@dataclass(frozen=True)
class _StreamClosed:
    returncode: int | None


class BoundedDiagnostics:
    """Keep recent lifecycle and stderr lines across worker restarts."""

    def __init__(self, *, maximum_lines: int = 64, maximum_line_characters: int = 1_000) -> None:
        if maximum_lines < 1 or maximum_line_characters < 1:
            raise ValueError("diagnostic bounds must be positive")
        self._lines: deque[str] = deque(maxlen=maximum_lines)
        self._maximum_line_characters = maximum_line_characters
        self._lock = threading.Lock()

    def append(self, source: str, message: str) -> None:
        normalized = " ".join(message.split()) or "<empty>"
        line = f"{source}: {normalized}"[: self._maximum_line_characters]
        with self._lock:
            self._lines.append(line)

    def snapshot(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._lines)

    def summary(self, *, maximum_characters: int = 2_000) -> str:
        lines = self.snapshot()
        if not lines:
            return ""
        summary = " | ".join(lines)
        if len(summary) <= maximum_characters:
            return summary
        return "..." + summary[-(maximum_characters - 3) :]


class JsonLineProcessController:
    """Own one isolated worker and exchange one response per request."""

    def __init__(
        self,
        *,
        command: Sequence[str],
        label: str,
        startup_timeout_seconds: float,
        request_timeout_seconds: float,
        shutdown_timeout_seconds: float,
        diagnostics: BoundedDiagnostics,
    ) -> None:
        for name, value in (
            ("startup", startup_timeout_seconds),
            ("request", request_timeout_seconds),
            ("shutdown", shutdown_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} timeout must be positive")
        if not command:
            raise ValueError("worker command cannot be empty")
        self.command = tuple(command)
        self.label = label
        self.startup_timeout_seconds = startup_timeout_seconds
        self.request_timeout_seconds = request_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._diagnostics = diagnostics
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[dict[str, object] | WorkerProtocolError | _StreamClosed] = (
            queue.Queue(maxsize=8)
        )
        self._stop_readers = threading.Event()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._io_lock = threading.Lock()
        self._next_request_id = 1

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _offer(
        self,
        item: dict[str, object] | WorkerProtocolError | _StreamClosed,
    ) -> None:
        try:
            self._responses.put_nowait(item)
        except queue.Full:
            with suppress(queue.Empty):
                self._responses.get_nowait()
            overflow = WorkerProtocolError(f"{self.label} emitted too many protocol responses")
            with suppress(queue.Full):
                self._responses.put_nowait(overflow)
            self._diagnostics.append("protocol", str(overflow))
            self._stop_readers.set()

    def _read_stdout(self, stream: TextIO, process: subprocess.Popen[str]) -> None:
        try:
            for line in stream:
                if self._stop_readers.is_set():
                    break
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict):
                        raise TypeError("JSON response must be an object")
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    failure = WorkerProtocolError(
                        f"{self.label} emitted invalid JSON: {type(error).__name__}: {error}"
                    )
                    self._diagnostics.append("protocol", str(failure))
                    self._offer(failure)
                    return
                self._offer(value)
        except (OSError, ValueError) as error:
            failure = WorkerProtocolError(f"{self.label} stdout reader failed: {error}")
            self._diagnostics.append("protocol", str(failure))
            self._offer(failure)
            return
        self._offer(_StreamClosed(process.poll()))

    def _read_stderr(self, stream: TextIO) -> None:
        try:
            for line in stream:
                if self._stop_readers.is_set():
                    break
                self._diagnostics.append("stderr", line)
        except (OSError, ValueError) as error:
            self._diagnostics.append("stderr-reader", str(error))

    def _wait_response(
        self,
        *,
        timeout_seconds: float,
        stage: str,
    ) -> dict[str, object]:
        try:
            item = self._responses.get(timeout=timeout_seconds)
        except queue.Empty as error:
            self._diagnostics.append("timeout", f"{stage} exceeded {timeout_seconds:g}s")
            raise WorkerTimeoutError(
                f"{self.label} {stage} timed out after {timeout_seconds:g} seconds"
            ) from error
        if isinstance(item, WorkerProtocolError):
            raise item
        if isinstance(item, _StreamClosed):
            detail = self._diagnostics.summary()
            suffix = f"; diagnostics: {detail}" if detail else ""
            raise WorkerRequestError(
                f"{self.label} exited before {stage} completed (code {item.returncode}){suffix}"
            )
        return item

    def start(self) -> dict[str, object]:
        if self.running:
            raise WorkerStartupError(f"{self.label} is already running")
        start_new_session = os.name == "posix"
        creationflags = (
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            if os.name == "nt"
            else 0
        )
        try:
            process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                start_new_session=start_new_session,
                creationflags=creationflags,
            )
        except OSError as error:
            self._diagnostics.append("startup", str(error))
            raise WorkerStartupError(f"Could not start {self.label}: {error}") from error
        self._process = process
        assert process.stdout is not None and process.stderr is not None
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout, process),
            name=f"{self.label}-stdout",
            daemon=True,
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr,),
            name=f"{self.label}-stderr",
            daemon=True,
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        self._diagnostics.append("lifecycle", f"started pid {process.pid}")
        try:
            return self._wait_response(
                timeout_seconds=self.startup_timeout_seconds,
                stage="startup",
            )
        except WorkerTimeoutError:
            self.close()
            raise
        except WorkerProcessError as error:
            self.close()
            raise WorkerStartupError(str(error)) from error

    def request(self, payload: Mapping[str, object]) -> dict[str, object]:
        with self._io_lock:
            process = self._process
            if process is None or process.poll() is not None:
                raise WorkerRequestError(f"{self.label} is not running")
            request_id = str(self._next_request_id)
            self._next_request_id += 1
            message = dict(payload)
            message["request_id"] = request_id
            try:
                assert process.stdin is not None
                process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                self._diagnostics.append("request", f"write failed: {error}")
                raise WorkerRequestError(f"{self.label} request write failed: {error}") from error
            response = self._wait_response(
                timeout_seconds=self.request_timeout_seconds,
                stage=f"request {request_id}",
            )
            if response.get("request_id") != request_id:
                raise WorkerProtocolError(
                    f"{self.label} returned a mismatched response for request {request_id}"
                )
            return response

    def _signal_group(self, sig: int) -> None:
        process = self._process
        if process is None:
            return
        try:
            if os.name == "posix":
                os.killpg(process.pid, sig)
            elif process.poll() is not None:
                return
            elif sig == signal.SIGTERM:
                process.terminate()
            else:
                process.kill()
        except (OSError, ProcessLookupError) as error:
            self._diagnostics.append("shutdown", f"signal failed: {error}")

    def _wait(self, timeout_seconds: float) -> bool:
        process = self._process
        if process is None:
            return True
        try:
            process.wait(timeout=timeout_seconds)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _group_running(self) -> bool:
        process = self._process
        if process is None:
            return False
        if os.name != "posix":
            return process.poll() is None
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _wait_group(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while self._group_running() and time.monotonic() < deadline:
            time.sleep(0.01)
        return not self._group_running()

    def close(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None and process.stdin is not None:
            try:
                process.stdin.write('{"action":"close","request_id":"close"}\n')
                process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass
        parent_closed = self._wait(self.shutdown_timeout_seconds)
        if not parent_closed:
            self._diagnostics.append("shutdown", "graceful close timed out; terminating group")
            self._signal_group(signal.SIGTERM)
        elif self._group_running():
            self._diagnostics.append("shutdown", "terminating remaining worker descendants")
            self._signal_group(signal.SIGTERM)
        if not self._wait_group(self.shutdown_timeout_seconds):
            self._diagnostics.append("shutdown", "termination timed out; killing group")
            self._signal_group(signal.SIGKILL)
            self._wait_group(self.shutdown_timeout_seconds)
        self._wait(self.shutdown_timeout_seconds)
        self._stop_readers.set()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                with suppress(OSError):
                    stream.close()
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=0.2)
        self._diagnostics.append("lifecycle", f"closed pid {process.pid} code {process.poll()}")
        self._process = None
