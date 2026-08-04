from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest

from whoopy.adapters.tts._json_process import (
    BoundedDiagnostics,
    JsonLineProcessController,
    WorkerProtocolError,
    WorkerTimeoutError,
)
from whoopy.adapters.tts.fish_speech import FishSpeech14Adapter, FishSpeechSettings
from whoopy.audio.synthesis import TransientSynthesisError
from whoopy.timeline import SpeechSegment


def _controller(
    source: str,
    *,
    diagnostics: BoundedDiagnostics | None = None,
    startup_timeout: float = 1,
    request_timeout: float = 1,
    shutdown_timeout: float = 0.1,
) -> JsonLineProcessController:
    return JsonLineProcessController(
        command=[sys.executable, "-u", "-c", textwrap.dedent(source)],
        label="fixture worker",
        startup_timeout_seconds=startup_timeout,
        request_timeout_seconds=request_timeout,
        shutdown_timeout_seconds=shutdown_timeout,
        diagnostics=diagnostics or BoundedDiagnostics(),
    )


def test_controller_exchanges_correlated_messages_and_closes() -> None:
    controller = _controller(
        """
        import json
        import sys

        print(json.dumps({"status": "ready", "sample_rate": 24000}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                print(json.dumps({"status": "closing", "request_id": "close"}), flush=True)
                break
            print(json.dumps({
                "status": "ok",
                "request_id": request["request_id"],
                "value": request["value"],
            }), flush=True)
        """
    )

    assert controller.start()["status"] == "ready"
    assert controller.request({"value": "first"})["value"] == "first"
    assert controller.request({"value": "second"})["value"] == "second"

    controller.close()
    assert controller.running is False


def test_controller_bounds_startup_wait_and_cleans_up() -> None:
    controller = _controller(
        """
        import time
        time.sleep(10)
        """,
        startup_timeout=0.05,
        shutdown_timeout=0.05,
    )

    with pytest.raises(WorkerTimeoutError, match="startup timed out"):
        controller.start()

    assert controller.running is False


def test_controller_bounds_request_wait() -> None:
    controller = _controller(
        """
        import json
        import sys
        import time

        print(json.dumps({"status": "ready", "sample_rate": 24000}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                break
            time.sleep(10)
        """,
        request_timeout=0.05,
        shutdown_timeout=0.05,
    )
    controller.start()

    with pytest.raises(WorkerTimeoutError, match="request 1 timed out"):
        controller.request({"value": "never returned"})

    controller.close()
    assert controller.running is False


def test_controller_rejects_mismatched_responses() -> None:
    controller = _controller(
        """
        import json
        import sys

        print(json.dumps({"status": "ready", "sample_rate": 24000}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                break
            print(json.dumps({"status": "ok", "request_id": "stale"}), flush=True)
        """
    )
    controller.start()

    with pytest.raises(WorkerProtocolError, match="mismatched response"):
        controller.request({"value": "test"})

    controller.close()


def test_diagnostics_are_bounded_and_survive_close() -> None:
    diagnostics = BoundedDiagnostics(maximum_lines=5, maximum_line_characters=40)
    controller = _controller(
        """
        import json
        import sys

        for index in range(80):
            print(f"diagnostic-{index}-" + "x" * 100, file=sys.stderr, flush=True)
        print(json.dumps({"status": "ready", "sample_rate": 24000}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                break
        """,
        diagnostics=diagnostics,
    )
    controller.start()
    controller.close()

    saved = diagnostics.snapshot()
    assert 1 <= len(saved) <= 5
    assert all(len(line) <= 40 for line in saved)
    assert any("diagnostic-79" in line for line in saved)


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-specific")
def test_close_kills_worker_descendants_that_ignore_termination() -> None:
    diagnostics = BoundedDiagnostics()
    controller = _controller(
        """
        import json
        import signal
        import subprocess
        import sys
        import time

        subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(10)",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(0.1)
        print(json.dumps({"status": "ready", "sample_rate": 24000}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                break
        """,
        diagnostics=diagnostics,
        shutdown_timeout=0.05,
    )
    controller.start()
    controller.close()

    saved = diagnostics.snapshot()
    assert any("remaining worker descendants" in line for line in saved)
    assert any("killing group" in line for line in saved)


def _fish_runtime(tmp_path: Path, worker_source: str) -> FishSpeechSettings:
    runtime = tmp_path / "runtime"
    python = runtime / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    checkpoint = runtime / "checkpoints" / "fish-speech-1.4"
    checkpoint.mkdir(parents=True)
    (checkpoint / "model.pth").write_bytes(b"model")
    (checkpoint / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth").write_bytes(b"decoder")
    reference_audio = runtime / "whoopy-reference.wav"
    reference_text = runtime / "whoopy-reference.txt"
    reference_audio.write_bytes(b"audio")
    reference_text.write_text("Reference words.", encoding="utf-8")
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(worker_source), encoding="utf-8")
    return FishSpeechSettings(
        runtime_directory=runtime,
        worker_script=worker,
        reference_audio=reference_audio,
        reference_text=reference_text,
        startup_timeout_seconds=1,
        request_timeout_seconds=0.05,
        shutdown_timeout_seconds=0.05,
    )


@pytest.mark.skipif(os.name != "posix", reason="fixture uses POSIX process termination")
def test_fish_adapter_restarts_cleanly_after_request_timeout(tmp_path: Path) -> None:
    settings = _fish_runtime(
        tmp_path,
        """
        import base64
        import json
        import sys
        import time
        from pathlib import Path

        marker = Path(__file__).with_suffix(".started")
        first_process = not marker.exists()
        marker.touch()
        print(json.dumps({"status": "ready", "sample_rate": 24000}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                break
            if first_process:
                print("first process stalled", file=sys.stderr, flush=True)
                time.sleep(10)
                continue
            print(json.dumps({
                "status": "ok",
                "request_id": request["request_id"],
                "pcm_s16le": base64.b64encode(b"\\x00\\x00").decode("ascii"),
            }), flush=True)
        """,
    )
    adapter = FishSpeech14Adapter(settings)
    segment = SpeechSegment(id="speech-1", text="Welcome.")

    with pytest.raises(TransientSynthesisError, match="timed out"):
        adapter.synthesize(segment)

    assert adapter.synthesize(segment).pcm_s16le == b"\x00\x00"
    adapter.close()
    assert any("first process stalled" in line for line in adapter.diagnostics())
    assert any("timed out" in line for line in adapter.diagnostics())
