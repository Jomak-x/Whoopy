from __future__ import annotations

import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

WORKER = Path("scripts/qwen3_tts_worker.py").resolve()


class _TorchSeedRecorder:
    def __init__(self) -> None:
        self.seeds: list[int] = []

    def manual_seed(self, seed: int) -> None:
        self.seeds.append(seed)


def test_qwen_worker_keeps_native_dependency_output_off_protocol_stdout() -> None:
    program = f"""
import importlib.util
import os

spec = importlib.util.spec_from_file_location("qwen3_tts_worker", {str(WORKER)!r})
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
module._isolate_protocol_output()
os.write(1, b"native dependency warning\\n")
module._reply({{"status": "ready"}})
"""

    completed = subprocess.run(
        [sys.executable, "-c", program],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 0
    assert json.loads(completed.stdout) == {"status": "ready"}
    assert "native dependency warning" not in completed.stdout
    assert "native dependency warning" in completed.stderr


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Notice the support beneath you.", 192),
        ("静かに呼吸を感じてください。" * 10, 516),
        ("word " * 1_000, 2_048),
    ],
)
def test_qwen_worker_uses_bounded_text_derived_token_budget(text: str, expected: int) -> None:
    maximum = runpy.run_path(str(WORKER))["_max_new_tokens"]

    assert maximum(text) == expected


def test_qwen_worker_retries_invalid_sampling_probabilities_once_with_greedy() -> None:
    fallback = runpy.run_path(str(WORKER))["_generate_with_reviewed_fallback"]
    torch = _TorchSeedRecorder()
    calls: list[dict[str, object]] = []

    def generate(**kwargs: object) -> tuple[list[list[float]], int]:
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("probability tensor contains either `inf`, `nan` or element < 0")
        return [[0.1, -0.1]], 24_000

    waveforms, rate, diagnostics = fallback(
        generate,
        generation_arguments={"text": "Settle here.", "max_new_tokens": 192},
        seed=42,
        torch=torch,
    )

    assert waveforms == [[0.1, -0.1]]
    assert rate == 24_000
    assert len(calls) == 2
    assert calls[0]["do_sample"] is True
    assert calls[0]["subtalker_dosample"] is True
    assert calls[1]["do_sample"] is False
    assert calls[1]["subtalker_dosample"] is False
    assert calls[1]["max_new_tokens"] == 192
    assert torch.seeds == [42]
    assert diagnostics["mode"] == "greedy_invalid_probability_retry"
    assert diagnostics["fallback_used"] is True


def test_qwen_worker_does_not_mask_unrelated_generation_failures() -> None:
    fallback = runpy.run_path(str(WORKER))["_generate_with_reviewed_fallback"]
    torch = _TorchSeedRecorder()
    calls = 0

    def generate(**_kwargs: object) -> tuple[list[list[float]], int]:
        nonlocal calls
        calls += 1
        raise RuntimeError("MPS backend ran out of memory")

    with pytest.raises(RuntimeError, match="out of memory"):
        fallback(
            generate,
            generation_arguments={"text": "Settle here.", "max_new_tokens": 192},
            seed=42,
            torch=torch,
        )

    assert calls == 1
    assert torch.seeds == []


def test_qwen_worker_successful_sampling_does_not_retry() -> None:
    fallback = runpy.run_path(str(WORKER))["_generate_with_reviewed_fallback"]
    torch = _TorchSeedRecorder()
    calls: list[dict[str, object]] = []

    def generate(**kwargs: object) -> tuple[list[list[float]], int]:
        calls.append(kwargs)
        return [[0.1]], 24_000

    _waveforms, _rate, diagnostics = fallback(
        generate,
        generation_arguments={"text": "Settle here.", "max_new_tokens": 192},
        seed=42,
        torch=torch,
    )

    assert len(calls) == 1
    assert diagnostics["mode"] == "sampled"
    assert diagnostics["fallback_used"] is False
    assert torch.seeds == []


def test_qwen_worker_invalid_probability_fallback_is_bounded_to_one_retry() -> None:
    fallback = runpy.run_path(str(WORKER))["_generate_with_reviewed_fallback"]
    torch = _TorchSeedRecorder()
    calls = 0

    def generate(**_kwargs: object) -> tuple[list[list[float]], int]:
        nonlocal calls
        calls += 1
        raise RuntimeError("probability tensor contains either `inf`, `nan` or element < 0")

    with pytest.raises(RuntimeError, match="probability tensor"):
        fallback(
            generate,
            generation_arguments={"text": "Settle here.", "max_new_tokens": 192},
            seed=42,
            torch=torch,
        )

    assert calls == 2
    assert torch.seeds == [42]
