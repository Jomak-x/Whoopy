from __future__ import annotations

import os
import sys
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from whoopy.adapters.tts._json_process import (
    BoundedDiagnostics,
    JsonLineProcessController,
    WorkerProtocolError,
    WorkerTimeoutError,
)
from whoopy.adapters.tts.fish_speech import FishSpeech14Adapter, FishSpeechSettings
from whoopy.adapters.tts.moss_tts import MossTTSAdapter, MossTTSSettings
from whoopy.audio.synthesis import InvalidSynthesisOutput, TransientSynthesisError
from whoopy.hardware import HardwareSnapshot
from whoopy.model_packs import HardwareRequirement
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


def _moss_runtime(
    tmp_path: Path,
    worker_source: str,
    *,
    variant: str,
) -> MossTTSSettings:
    runtime = tmp_path / "runtime"
    python = runtime / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    model = tmp_path / "model"
    codec = tmp_path / "codec"
    for directory in (model, codec):
        directory.mkdir()
        (directory / "config.json").write_text("{}", encoding="utf-8")
        (directory / "model.safetensors").write_bytes(b"checkpoint")
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    worker = tmp_path / "worker.py"
    worker.write_text(textwrap.dedent(worker_source), encoding="utf-8")
    return MossTTSSettings(
        runtime_directory=runtime,
        worker_script=worker,
        model_directory=model,
        codec_directory=codec,
        variant=variant,  # type: ignore[arg-type]
        reference_audio=reference,
        startup_timeout_seconds=1,
        request_timeout_seconds=0.05,
        shutdown_timeout_seconds=0.05,
    )


def _blocked_hardware() -> HardwareSnapshot:
    return HardwareSnapshot(
        operating_system="darwin",
        architecture="arm64",
        cpu_count=8,
        total_ram_gb=16,
        available_ram_gb=1,
        free_disk_gb=100,
        accelerators=["cpu", "metal"],
    )


def _heavy_requirement() -> HardwareRequirement:
    return HardwareRequirement(
        min_total_ram_gb=12,
        min_available_ram_gb=6,
        min_free_disk_gb=3,
        accelerator_any_of=["metal"],
    )


def test_fish_rechecks_live_ram_immediately_before_worker_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _fish_runtime(
        tmp_path,
        'print("worker must not start", flush=True)',
    )
    settings = replace(settings, hardware_requirement=_heavy_requirement())
    monkeypatch.setattr(
        "whoopy.adapters.tts.fish_speech.inspect_hardware", lambda _path: _blocked_hardware()
    )
    adapter = FishSpeech14Adapter(settings)

    with pytest.raises(TransientSynthesisError, match=r"immediately before load.*6 GB"):
        adapter.prepare()

    assert adapter.runtime_process_id is None


def test_moss_rechecks_live_ram_immediately_before_worker_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _moss_runtime(
        tmp_path,
        'print("worker must not start", flush=True)',
        variant="moss-local-v1.5",
    )
    settings = replace(settings, hardware_requirement=_heavy_requirement())
    monkeypatch.setattr(
        "whoopy.adapters.tts.moss_tts.inspect_hardware", lambda _path: _blocked_hardware()
    )
    adapter = MossTTSAdapter(settings)

    with pytest.raises(TransientSynthesisError, match=r"immediately before load.*6 GB"):
        adapter.prepare()

    assert adapter.runtime_process_id is None


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


@pytest.mark.skipif(os.name != "posix", reason="fixture uses POSIX process termination")
@pytest.mark.parametrize("variant", ["moss-local-v1.5", "moss-v1.5"])
def test_each_moss_adapter_restarts_and_releases_its_slot_after_request_timeout(
    tmp_path: Path,
    variant: str,
) -> None:
    settings = _moss_runtime(
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
        print(json.dumps({"status": "ready", "sample_rate": 24000, "device": "cpu"}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                break
            if first_process:
                print("moss request stalled", file=sys.stderr, flush=True)
                time.sleep(10)
                continue
            print(json.dumps({
                "status": "ok",
                "request_id": request["request_id"],
                "pcm_s16le": base64.b64encode(b"\\x01\\x00").decode("ascii"),
            }), flush=True)
        """,
        variant=variant,
    )
    adapter = MossTTSAdapter(settings)
    segment = SpeechSegment(id="speech-1", text="Welcome.")

    with pytest.raises(TransientSynthesisError, match="timed out"):
        adapter.synthesize(segment)

    assert adapter.synthesize(segment).pcm_s16le == b"\x01\x00"
    adapter.close()
    assert any("moss request stalled" in line for line in adapter.diagnostics())
    assert any("timed out" in line for line in adapter.diagnostics())


@pytest.mark.skipif(os.name != "posix", reason="fixture uses POSIX process termination")
@pytest.mark.parametrize("variant", ["moss-local-v1.5", "moss-v1.5"])
def test_each_moss_adapter_restarts_after_empty_audio(
    tmp_path: Path,
    variant: str,
) -> None:
    settings = _moss_runtime(
        tmp_path,
        """
        import base64
        import json
        import sys
        from pathlib import Path

        marker = Path(__file__).with_suffix(".started")
        first_process = not marker.exists()
        marker.touch()
        print(json.dumps({"status": "ready", "sample_rate": 24000, "device": "cpu"}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("action") == "close":
                break
            pcm = b"" if first_process else b"\\x01\\x00"
            print(json.dumps({
                "status": "ok",
                "request_id": request["request_id"],
                "pcm_s16le": base64.b64encode(pcm).decode("ascii"),
            }), flush=True)
        """,
        variant=variant,
    )
    adapter = MossTTSAdapter(settings)
    segment = SpeechSegment(id="speech-1", text="Welcome.")

    with pytest.raises(InvalidSynthesisOutput, match="empty audio"):
        adapter.synthesize(segment)

    assert adapter.synthesize(segment).pcm_s16le == b"\x01\x00"
    adapter.close()


class _DeadController:
    running = False

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def close(self) -> None:
        self.events.append("close-dead")


class _ReadyController:
    running = False
    process_id = 123

    def __init__(self, events: list[str], **_arguments: object) -> None:
        self.events = events

    def start(self) -> dict[str, object]:
        self.events.append("start-new")
        self.running = True
        return {"status": "ready", "sample_rate": 24_000, "device": "cpu"}

    def close(self) -> None:
        self.running = False
        self.events.append("close-new")


class _RecordingSlot:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def acquire(self) -> None:
        self.events.append("acquire-slot")

    def release(self) -> None:
        self.events.append("release-slot")


def test_fish_dead_controller_releases_slot_before_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _fish_runtime(tmp_path, "# worker is replaced by the test controller")
    adapter = FishSpeech14Adapter(settings)
    events: list[str] = []
    adapter._controller = _DeadController(events)  # type: ignore[assignment]
    adapter._runtime_slot = _RecordingSlot(events)  # type: ignore[assignment]
    monkeypatch.setattr(
        "whoopy.adapters.tts.fish_speech.JsonLineProcessController",
        lambda **arguments: _ReadyController(events, **arguments),
    )

    adapter.prepare()

    assert events[:4] == ["close-dead", "release-slot", "acquire-slot", "start-new"]
    adapter.close()


def test_moss_dead_controller_releases_slot_before_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    model = tmp_path / "model"
    codec = tmp_path / "codec"
    for directory in (model, codec):
        directory.mkdir()
        (directory / "config.json").write_text("{}", encoding="utf-8")
        (directory / "model.safetensors").write_bytes(b"checkpoint")
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")
    worker = tmp_path / "worker.py"
    worker.write_text("# worker is replaced by the test controller", encoding="utf-8")
    adapter = MossTTSAdapter(
        MossTTSSettings(
            runtime_directory=runtime,
            worker_script=worker,
            model_directory=model,
            codec_directory=codec,
            variant="moss-local-v1.5",
            reference_audio=reference,
        )
    )
    events: list[str] = []
    adapter._controller = _DeadController(events)  # type: ignore[assignment]
    adapter._runtime_slot = _RecordingSlot(events)  # type: ignore[assignment]
    monkeypatch.setattr(
        "whoopy.adapters.tts.moss_tts.JsonLineProcessController",
        lambda **arguments: _ReadyController(events, **arguments),
    )

    adapter.prepare()

    assert events[:4] == ["close-dead", "release-slot", "acquire-slot", "start-new"]
    adapter.close()


def test_moss_direct_voice_worker_starts_without_reference_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = replace(
        _moss_runtime(
            tmp_path,
            "# worker is replaced by the test controller",
            variant="moss-local-v1.5",
        ),
        reference_audio=None,
        use_reference=False,
    )
    captured: dict[str, object] = {}
    events: list[str] = []

    def controller(**arguments: object) -> _ReadyController:
        captured.update(arguments)
        return _ReadyController(events, **arguments)

    monkeypatch.setattr(
        "whoopy.adapters.tts.moss_tts.JsonLineProcessController",
        controller,
    )
    adapter = MossTTSAdapter(settings)
    adapter._runtime_slot = _RecordingSlot(events)  # type: ignore[assignment]

    adapter.prepare()

    command = captured["command"]
    assert isinstance(command, list)
    assert "--reference-audio" not in command
    adapter.close()
