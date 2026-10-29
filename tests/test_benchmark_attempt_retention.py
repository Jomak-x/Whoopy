from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from scripts import (
    benchmark_writing_methods,
    chatterbox_v3_raw_meditation,
    raw_qwen_meditation,
    verify_benchmark_audio,
)


@pytest.mark.parametrize("worker", [False, True])
def test_verification_refuses_existing_receipt_before_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, worker: bool
) -> None:
    receipt = tmp_path / "receipt.json"
    original = b'{"status":"fail","asr_error":"runtime unavailable"}\n'
    receipt.write_bytes(original)
    argv = [
        "verify",
        str(tmp_path / "audio.wav"),
        "--report",
        str(receipt),
        "--expected-text",
        "Rest here.",
    ]
    if worker:
        argv.append("--asr-worker")
    monkeypatch.setattr(sys, "argv", argv)

    def unexpected_inference(*args: object, **kwargs: object) -> None:
        pytest.fail("An occupied receipt must be rejected before starting ASR")

    monkeypatch.setattr(verify_benchmark_audio, "asr_worker", unexpected_inference)
    monkeypatch.setattr(subprocess, "run", unexpected_inference)

    with pytest.raises(SystemExit, match="use a new report path"):
        verify_benchmark_audio.main()

    assert receipt.read_bytes() == original


def test_verification_retry_keeps_failed_receipt_and_requires_new_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = tmp_path / "first.json"
    argv = [
        "verify",
        str(tmp_path / "missing.wav"),
        "--report",
        str(receipt),
        "--expected-text",
        "Rest here.",
        "--asr-python",
        str(tmp_path / "missing-python"),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    assert verify_benchmark_audio.main() == 1
    original = receipt.read_bytes()
    assert json.loads(original)["status"] == "fail"

    with pytest.raises(SystemExit, match="use a new report path"):
        verify_benchmark_audio.main()

    second = tmp_path / "second.json"
    argv[3] = str(second)
    assert verify_benchmark_audio.main() == 1
    assert receipt.read_bytes() == original
    assert json.loads(second.read_bytes())["status"] == "fail"


@pytest.mark.parametrize("renderer", ["qwen", "chatterbox"])
@pytest.mark.parametrize("occupied", [False, True])
def test_raw_renderer_requires_unclaimed_attempt_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, renderer: str, occupied: bool
) -> None:
    output = tmp_path / "attempt"
    output.mkdir()
    prior = output / "speech.wav"
    entrypoint: Callable[[], int | None]
    if occupied:
        prior.write_bytes(b"native audio from the first attempt")
    if renderer == "qwen":
        argv = [
            "qwen",
            "--mode",
            "custom",
            "--model",
            str(tmp_path),
            "--output-directory",
            str(output),
        ]
        entrypoint = raw_qwen_meditation.main
    else:
        script = tmp_path / "script.md"
        reference = tmp_path / "reference.wav"
        script.write_text("Rest here.")
        reference.write_bytes(b"reference")
        argv = [
            "chatterbox",
            "--script",
            str(script),
            "--reference",
            str(reference),
            "--weights",
            str(tmp_path),
            "--pkuseg-home",
            str(tmp_path),
            "--output-directory",
            str(output),
        ]
        entrypoint = chatterbox_v3_raw_meditation.main
    monkeypatch.setattr(sys, "argv", argv)

    with pytest.raises(SystemExit, match="use a new output directory"):
        entrypoint()

    assert sorted(path.name for path in output.iterdir()) == (["speech.wav"] if occupied else [])
    if occupied:
        assert prior.read_bytes() == b"native audio from the first attempt"


def test_writing_benchmark_atomically_claims_output_for_one_invocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "attempt"
    barrier = threading.Barrier(2)
    original_mkdir = Path.mkdir

    def contested_mkdir(
        path: Path, mode: int = 0o777, parents: bool = False, exist_ok: bool = False
    ) -> None:
        if path == output:
            barrier.wait(timeout=5)
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    class ClaimedOutput(Exception):
        pass

    def record_claim(path: Path, value: Any) -> None:
        path.write_text(json.dumps(value))
        raise ClaimedOutput

    monkeypatch.setattr(Path, "mkdir", contested_mkdir)
    monkeypatch.setattr(benchmark_writing_methods, "write_json", record_claim)
    monkeypatch.setattr(sys, "argv", ["benchmark", "--output", str(output)])

    def invoke() -> str:
        try:
            benchmark_writing_methods.main()
        except ClaimedOutput:
            return "claimed"
        except SystemExit as error:
            assert "use a new output directory" in str(error)
            return "refused"
        pytest.fail("Benchmark unexpectedly reached inference")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert sorted(results) == ["claimed", "refused"]
    receipts = list(output.rglob("result.json"))
    assert len(receipts) == 1
    assert json.loads(receipts[0].read_text())["status"] == "waiting_for_gpu_lock"


@pytest.fixture
def vox_renderer(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setitem(sys.modules, "numpy", ModuleType("numpy"))
    monkeypatch.setitem(sys.modules, "soundfile", ModuleType("soundfile"))
    path = Path(__file__).resolve().parents[1] / "scripts/voxcpm2_raw_meditation.py"
    spec = importlib.util.spec_from_file_location("vox_retention_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("flag", ["--overwrite", "--resume"])
def test_vox_renderer_rejects_replacement_modes(vox_renderer: ModuleType, flag: str) -> None:
    with pytest.raises(SystemExit) as error:
        vox_renderer.build_parser().parse_args(
            ["render", "--script", "script.md", "--model", "model", "--output", "out.wav", flag]
        )
    assert error.value.code == 2


@pytest.mark.parametrize("artifact", ["wav", "manifest", "partial", "segments"])
def test_vox_renderer_preserves_occupied_output_before_runtime_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, vox_renderer: ModuleType, artifact: str
) -> None:
    script = tmp_path / "script.md"
    script.write_text("\n\n".join("Rest here.\n\n[Pause for 2 seconds]" for _ in range(19)))
    output = tmp_path / "voice.wav"
    paths = {
        "wav": output,
        "manifest": output.with_suffix(".manifest.json"),
        "partial": output.with_suffix(".wav.partial"),
        "segments": tmp_path / "voice-segments",
    }
    existing = paths[artifact]
    if artifact == "segments":
        existing.mkdir()
        existing = existing / "speech-00.wav"
    existing.write_bytes(b"previous native evidence")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vox",
            "render",
            "--script",
            str(script),
            "--model",
            str(tmp_path),
            "--output",
            str(output),
        ],
    )

    with pytest.raises(SystemExit, match="use a new output path"):
        vox_renderer.main()

    assert existing.read_bytes() == b"previous native evidence"
