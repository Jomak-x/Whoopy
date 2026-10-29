from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from threading import Thread
from typing import Any

import pytest

from whoopy.adapters.llm import llama_cpp
from whoopy.adapters.llm.llama_cpp import CapturingProcessRunner, _run_process


@pytest.mark.parametrize("drain", ["complete", "blocked", "read_error"])
def test_timeout_capture_waits_for_readers_and_records_incomplete_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drain: str
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
        with pytest.raises(subprocess.TimeoutExpired) as caught:
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
