from __future__ import annotations

import importlib.util
import json
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest


def leaf() -> Any:
    path = Path(__file__).resolve().parents[1] / "scripts/whisper_speech_worker.py"
    spec = importlib.util.spec_from_file_location("whisper_leaf_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, Path, Path]:
    worker = leaf()
    monkeypatch.setattr(worker, "_supported_host", lambda: True)
    monkeypatch.setattr(worker, "GPU_LOCK", tmp_path / "gpu.lock")
    model = tmp_path / "immutable-model-revision"
    model.mkdir()
    (model / "weights.npz").write_bytes(b"weights" * 200_000)
    (model / "config.json").write_text("{}")
    audio, output = tmp_path / "input.wav", tmp_path / "asr.json"
    audio.write_bytes(b"fake audio; no model imported")
    return worker, audio, model, output


def test_leaf_uses_exact_unprompted_offline_decoding_and_immutable_output(
    inputs: tuple[Any, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, audio, model, output = inputs
    calls = []

    def recognize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append((args, kwargs))
        return {"text": "Observed speech", "segments": [], "language": "en"}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=recognize))
    worker.transcribe_local(
        audio,
        model,
        output,
        weights_sha256=worker.file_sha256(model / "weights.npz"),
        config_sha256=worker.file_sha256(model / "config.json"),
        python_sha256=worker.file_sha256(Path(sys.executable)),
    )
    assert calls == [
        (
            (str(audio.resolve()),),
            {
                "path_or_hf_repo": str(model),
                "language": "en",
                "temperature": 0.0,
                "condition_on_previous_text": False,
                "word_timestamps": True,
                "verbose": False,
            },
        )
    ]
    report = json.loads(output.read_text())
    assert report["decoding"]["expected_text_prompted"] is False
    assert report["metric_warnings"] == []
    assert report["audio_sha256"] == worker.file_sha256(audio)
    assert report["python_sha256"] == worker.file_sha256(Path(sys.executable))
    assert report["model_revision"] == model.name
    assert report["gpu_lock_wait_seconds"] >= 0 and report["inference_seconds"] >= 0
    assert output.stat().st_mode & 0o222 == 0
    with pytest.raises(FileExistsError):
        worker.transcribe_local(audio, model, output)
    assert len(calls) == 1


@pytest.mark.parametrize("drift", ["audio", "weights", "config", "nonfinite"])
def test_leaf_refuses_changed_inputs_or_invalid_results(
    inputs: tuple[Any, Path, Path, Path], monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    worker, audio, model, output = inputs

    def recognize(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        target = {
            "audio": audio,
            "weights": model / "weights.npz",
            "config": model / "config.json",
        }.get(drift)
        if target is not None:
            target.write_bytes(target.read_bytes() + b"changed")
        return {"text": "words", "segments": [float("nan")] if drift == "nonfinite" else []}

    monkeypatch.setitem(sys.modules, "mlx_whisper", SimpleNamespace(transcribe=recognize))
    with pytest.raises(ValueError):
        worker.transcribe_local(audio, model, output)
    assert not output.exists()


def test_leaf_rejects_wrong_interpreter_and_platform_before_model_import(
    inputs: tuple[Any, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    worker, audio, model, output = inputs
    with pytest.raises(ValueError, match="interpreter"):
        worker.transcribe_local(audio, model, output, python_sha256="0" * 64)
    monkeypatch.setattr(worker, "_supported_host", lambda: False)
    with pytest.raises(RuntimeError, match="macOS"):
        worker.transcribe_local(audio, model, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "metric", ["avg_logprob", "compression_ratio", "no_speech_prob", "temperature", "probability"]
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf"), 0.125])
def test_leaf_only_nulls_nonfinite_optional_metrics_and_writes_strict_json(
    inputs: tuple[Any, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    metric: str,
    value: float,
) -> None:
    worker, audio, model, output = inputs
    segment: dict[str, Any] = {
        "start": 0.0,
        "end": 1.25,
        "text": " Observed speech",
        "tokens": [42, 17],
        "avg_logprob": -0.25,
        "compression_ratio": 1.1,
        "no_speech_prob": 0.01,
        "temperature": 0.0,
        "words": [{"word": " Observed", "start": 0.0, "end": 0.5, "probability": 0.9}],
    }
    target = segment["words"][0] if metric == "probability" else segment
    target[metric] = value
    result = {"text": "Observed speech", "segments": [segment], "language": "en"}
    monkeypatch.setitem(
        sys.modules, "mlx_whisper", SimpleNamespace(transcribe=lambda *_args, **_kwargs: result)
    )

    worker.transcribe_local(audio, model, output)

    def reject_constant(constant: str) -> None:
        raise AssertionError(f"Nonstandard JSON constant: {constant}")

    report = json.loads(output.read_text(), parse_constant=reject_constant)
    metric_path = f".segments[0].{'words[0].' if metric == 'probability' else ''}{metric}"
    assert report["metric_warnings"] == ([] if math.isfinite(value) else [metric_path])
    expected_segment = {**segment, "words": [dict(segment["words"][0])]}
    expected_target = expected_segment["words"][0] if metric == "probability" else expected_segment
    expected_target[metric] = value if math.isfinite(value) else None
    assert report["segments"] == [expected_segment]
    assert report["text"] == result["text"]
    # Sanitization must not mutate the recognizer's result in memory.
    assert target[metric] is value
    assert output.stat().st_mode & 0o222 == 0


@pytest.mark.parametrize(
    "segment",
    [
        {"start": float("nan")},
        {"end": float("inf")},
        {"text": float("nan")},
        {"tokens": [float("nan")]},
        {"unknown_metric": float("nan")},
        {"probability": float("nan")},
        {"nested": {"avg_logprob": float("nan")}},
        {"words": [{"word": float("nan")}]},
        {"words": [{"start": float("nan")}]},
        {"words": [{"end": float("inf")}]},
        {"words": [{"avg_logprob": float("nan")}]},
    ],
)
def test_leaf_rejects_nonfinite_mandatory_or_unknown_paths_before_publishing(
    inputs: tuple[Any, Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    segment: dict[str, Any],
) -> None:
    worker, audio, model, output = inputs
    monkeypatch.setitem(
        sys.modules,
        "mlx_whisper",
        SimpleNamespace(
            transcribe=lambda *_args, **_kwargs: {"text": "words", "segments": [segment]}
        ),
    )
    with pytest.raises(ValueError, match="Out of range float values"):
        worker.transcribe_local(audio, model, output)
    assert not output.exists()
