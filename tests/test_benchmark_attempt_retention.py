from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import psutil
import pytest

from scripts import (
    benchmark_writing_methods,
    chatterbox_v3_raw_meditation,
    raw_qwen_meditation,
    verify_benchmark_audio,
)
from whoopy.adapters.llm.llama_cpp import LlamaCppScriptGenerator, LlamaProcessResult


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


@pytest.mark.parametrize("returncode", [0, 7])
@pytest.mark.parametrize("with_telemetry", [False, True])
def test_writing_capture_preserves_each_actual_process_invocation(
    tmp_path: Path, returncode: int, with_telemetry: bool
) -> None:
    inputs = {
        "--file": ("prompt.txt", b"Original request.\n"),
        "--system-prompt-file": ("system.txt", b"System rules.\n"),
        "--json-schema-file": ("response-schema.json", b'{"type":"object"}\n'),
    }
    command = ["mock-llama"]
    for flag, (name, data) in inputs.items():
        path = tmp_path / name
        path.write_bytes(data)
        command.extend([flag, str(path)])
    output = tmp_path / "output.txt"
    command.extend(["--output-file", str(output), "--seed", "42"])
    completed = subprocess.CompletedProcess(command, returncode, "raw stdout\n", "raw stderr\n")
    expected = LlamaProcessResult(completed) if with_telemetry else completed

    def run(actual_command: Sequence[str], timeout: float) -> Any:
        assert list(actual_command) == command
        assert timeout == 300
        output.write_bytes(b'{"untrusted":"exact raw output"}\n')
        return expected

    root = tmp_path / "evidence"
    capture = benchmark_writing_methods.CapturingProcessRunner(root, run)
    assert capture(command, 300) is expected
    before = {path.name: path.read_bytes() for path in (root / "001").iterdir()}
    assert capture(command, 300) is expected
    assert before == {path.name: path.read_bytes() for path in (root / "001").iterdir()}
    assert sorted(path.name for path in root.iterdir()) == ["001", "002"]
    for number in ("001", "002"):
        folder = root / number
        assert json.loads((folder / "command.json").read_text()) == command
        for name, data in inputs.values():
            assert (folder / name).read_bytes() == data
        assert (folder / "output.txt").read_bytes() == output.read_bytes()
        assert (folder / "stdout.txt").read_text() == "raw stdout\n"
        assert (folder / "stderr.txt").read_text() == "raw stderr\n"
        assert json.loads((folder / "process.json").read_text())["returncode"] == returncode

    collision = benchmark_writing_methods.CapturingProcessRunner(root, run)
    with pytest.raises(FileExistsError):
        collision(command, 300)
    assert before == {path.name: path.read_bytes() for path in (root / "001").iterdir()}


@pytest.mark.parametrize("interrupted", [False, True])
def test_writing_capture_keeps_partial_output_and_propagates_process_failure(
    tmp_path: Path, interrupted: bool
) -> None:
    output = tmp_path / "output.txt"
    command = ["mock-llama", "--output-file", str(output)]
    failure = (
        KeyboardInterrupt()
        if interrupted
        else subprocess.TimeoutExpired(command, 300, output=b"partial stdout", stderr=b"timeout")
    )

    def run(actual_command: Sequence[str], timeout: float) -> Any:
        output.write_bytes(b"partial model output")
        raise failure

    capture = benchmark_writing_methods.CapturingProcessRunner(tmp_path / "evidence", run)
    with pytest.raises(type(failure)) as caught:
        capture(command, 300)
    assert caught.value is failure
    folder = tmp_path / "evidence/001"
    receipt = json.loads((folder / "process.json").read_text())
    assert receipt["returncode"] is None
    assert receipt["exception_type"] == type(failure).__name__
    assert (folder / "output.txt").read_bytes() == b"partial model output"
    if interrupted:
        assert "stdout_unavailable_reason" in receipt
        assert not (folder / "stdout.txt").exists()
    else:
        assert (folder / "stdout.txt").read_bytes() == b"partial stdout"
        assert (folder / "stderr.txt").read_bytes() == b"timeout"


@pytest.mark.parametrize("custom_lock", [False, True])
def test_writing_cli_passes_explicit_model_storage_and_keeps_both_failed_methods(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, custom_lock: bool
) -> None:
    output = tmp_path / "fresh"
    models = tmp_path / "existing-models"
    lock = (
        tmp_path / "experimental-artifacts.yaml" if custom_lock else Path("config/artifacts.yaml")
    )
    if custom_lock:
        lock.write_bytes(b"fixture custom artifact lock\n")
    loaded_locks: list[Path] = []
    sentinel_lock = object()

    def load_lock(path: Path) -> object:
        loaded_locks.append(path)
        return sentinel_lock

    calls: list[dict[str, Any]] = []

    def refuse_model(**kwargs: Any) -> Any:
        calls.append(kwargs)
        raise RuntimeError("fixture adapter failure before inference")

    monkeypatch.setattr(LlamaCppScriptGenerator, "from_artifact_store", refuse_model)
    monkeypatch.setattr(benchmark_writing_methods, "load_artifact_lock", load_lock)
    monkeypatch.setattr(benchmark_writing_methods, "_lock_exclusively", lambda _: None)
    monkeypatch.setattr(psutil, "swap_memory", lambda: SimpleNamespace(used=0))
    monkeypatch.setattr(
        psutil,
        "virtual_memory",
        lambda: SimpleNamespace(available=32 * 1024**3),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "benchmark",
            "--output",
            str(output),
            "--models-dir",
            str(models),
            "--case",
            "grounding-3m",
            "--method",
            "plan_first",
            "--method",
            "recipe_whole_session",
            "--seed",
            "42",
            "--profile",
            "high",
        ]
        + (["--artifact-lock", str(lock)] if custom_lock else []),
    )

    assert benchmark_writing_methods.main() == 1
    assert len(calls) == 2
    assert loaded_locks == [lock, lock]
    for call in calls:
        assert call["artifact_lock"] is sentinel_lock
        assert call["store"].root == models
        assert call["profile_name"] == "high"
        settings = call["settings"]
        assert (settings.context_tokens, settings.temperature, settings.top_p) == (32768, 0.7, 0.9)
    report = json.loads((output / "report.json").read_text())
    assert report["models_dir"] == str(models.resolve())
    assert report["artifact_lock"] == str(lock.resolve())
    assert report["artifact_lock_sha256"] == hashlib.sha256(lock.read_bytes()).hexdigest()
    assert [result["writing_method"] for result in report["results"]] == [
        "plan_first",
        "recipe_whole_session",
    ]
    assert all(result["status"] == "failed" for result in report["results"])
    assert (output / ".gitignore").read_text() == "*\n"


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
