from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from threading import Thread
from typing import Any

import psutil
import pytest

from whoopy.adapters.llm import llama_cpp
from whoopy.adapters.llm.llama_cpp import CapturingProcessRunner, _run_process


@pytest.mark.parametrize("drain", ["complete", "blocked", "read_error"])
@pytest.mark.parametrize("interrupted", [False, True])
def test_failure_capture_waits_for_readers_and_records_incomplete_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drain: str, interrupted: bool
) -> None:
    class DelayedStream:
        def __init__(self, name: str) -> None:
            self.name = name
            self.pending = threading.Event()
            self.release = threading.Event()
            self.calls = 0
            self.closed = False

        def readline(self) -> str:
            self.calls += 1
            if self.calls == 1:
                return f"{self.name} prefix\n"
            if self.calls == 2:
                self.pending.set()
                assert self.release.wait(timeout=10)
                if drain == "read_error":
                    raise OSError("private reader detail")
                return f"{self.name} trailing bytes"
            return ""

        def close(self) -> None:
            self.closed = True

    streams = {name: DelayedStream(name) for name in ("stdout", "stderr")}
    readers: list[Thread] = []

    class ControlledThread(Thread):
        def join(self, timeout: float | None = None) -> None:
            for name, stream in streams.items():
                if self.name == f"whoopy-llama-{name}-reader":
                    readers.append(self)
                    assert timeout == 1
                    if drain == "blocked":
                        return
                    stream.release.set()
            super().join(timeout)

    class TimedOutProcess:
        pid = os.getpid()
        stdout = streams["stdout"]
        stderr = streams["stderr"]
        returncode: int | None = None

        def wait(self, timeout: float) -> None:
            assert all(stream.pending.wait(timeout=5) for stream in streams.values())
            if interrupted:
                raise KeyboardInterrupt
            raise subprocess.TimeoutExpired(["local-writer"], timeout)

    process = TimedOutProcess()

    def start(*args: Any, **kwargs: Any) -> TimedOutProcess:
        return process

    def stop(actual: TimedOutProcess) -> None:
        assert actual is process
        actual.returncode = -15

    monkeypatch.setattr(llama_cpp.subprocess, "Popen", start)
    monkeypatch.setattr(llama_cpp.threading, "Thread", ControlledThread)
    monkeypatch.setattr(llama_cpp, "_stop_process_tree", stop)
    capture = CapturingProcessRunner(tmp_path / "attempts", _run_process)
    try:
        failure_type = KeyboardInterrupt if interrupted else subprocess.TimeoutExpired
        with pytest.raises(failure_type) as caught:
            capture(["local-writer"], 0.05)
        assert process.returncode == -15
        folder = tmp_path / "attempts/001"
        receipt = json.loads((folder / "process.json").read_text())
        assert receipt["status"] == "failed"
        for name in streams:
            expected = f"{name} prefix\n"
            if drain == "complete":
                expected += f"{name} trailing bytes"
                assert f"{name}_incomplete_reason" not in receipt
            else:
                reason = receipt[f"{name}_incomplete_reason"]
                assert "bounded drain" in reason if drain == "blocked" else "OSError" in reason
                assert "private reader detail" not in reason
            assert (folder / f"{name}.txt").read_text() == expected
            assert getattr(caught.value, name) == expected
    finally:
        for stream in streams.values():
            stream.release.set()
        for reader in readers:
            Thread.join(reader, timeout=5)
        assert all(stream.closed for stream in streams.values())


@pytest.mark.skipif(os.name != "posix", reason="Uses POSIX SIGINT process interruption")
def test_sigint_retains_real_child_streams_in_immutable_invocation_records(tmp_path: Path) -> None:
    ready = tmp_path / "ready.json"
    root = tmp_path / "attempts"
    child_script = (
        "import json, os, sys, time; from pathlib import Path; "
        "print('partial draft', flush=True); "
        "print('writer diagnostic', file=sys.stderr, flush=True); "
        "sys.stdout.write('unfinished prose'); sys.stdout.flush(); "
        "sys.stderr.write('unfinished diagnostic'); sys.stderr.flush(); "
        f"Path({str(ready)!r}).write_text(json.dumps(os.getpid())); "
        "time.sleep(120)"
    )
    command = [sys.executable, "-c", child_script]
    runner_script = (
        "from pathlib import Path\n"
        "from whoopy.adapters.llm.llama_cpp import CapturingProcessRunner, _run_process\n"
        f"capture = CapturingProcessRunner(Path({str(root)!r}), _run_process)\n"
        "try:\n"
        f"    capture({command!r}, 60)\n"
        "except KeyboardInterrupt:\n"
        "    raise SystemExit(130)\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", runner_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        child_pid = json.loads(ready.read_text())
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, (stdout, stderr)
        assert not psutil.pid_exists(child_pid)
        folder = root / "001"
        receipt = json.loads((folder / "process.json").read_text())
        assert receipt["status"] == "failed"
        assert receipt["exception_type"] == "KeyboardInterrupt"
        assert receipt["returncode"] is None
        for name, expected in (
            ("stdout", "partial draft\nunfinished prose"),
            ("stderr", "writer diagnostic\nunfinished diagnostic"),
        ):
            assert (folder / f"{name}.txt").read_text() == expected
            assert f"{name}_unavailable_reason" not in receipt
            assert f"{name}_incomplete_reason" not in receipt
        before = {path.name: path.read_bytes() for path in folder.iterdir()}
        capture = CapturingProcessRunner(root, _run_process)
        with pytest.raises(FileExistsError):
            capture(command, 1)
        assert before == {path.name: path.read_bytes() for path in folder.iterdir()}
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=10)
        if ready.exists():
            child_pid = json.loads(ready.read_text())
            try:
                child = psutil.Process(child_pid)
            except psutil.NoSuchProcess:
                pass
            else:
                child.kill()
                child.wait(timeout=5)
