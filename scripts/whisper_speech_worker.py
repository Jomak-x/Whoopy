#!/usr/bin/env python3
"""Offline local Whisper leaf. It never receives the expected transcript."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import sys
import threading
import time
from pathlib import Path
from typing import Any

GPU_LOCK = Path("/tmp/whoopy-benchmark-gpu.lock")
OPTIONAL_SEGMENT_METRICS = frozenset(
    {"avg_logprob", "compression_ratio", "no_speech_prob", "temperature"}
)


def _supported_host() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_optional_metrics(segments: Any) -> tuple[Any, list[str]]:
    """Keep strict JSON except for documented optional confidence metric paths.

    Whisper can produce NaN confidence for an empty/quiet decoding window.
    Retain the transcript with null metrics and explicit paths for listener review;
    nonfinite timestamps, tokens, text, and unknown fields still fail serialization.
    """
    warnings: list[str] = []

    def visit(value: Any, keys: tuple[Any, ...], path: str) -> Any:
        optional = (
            len(keys) == 3
            and keys[0] == "segments"
            and isinstance(keys[1], int)
            and keys[2] in OPTIONAL_SEGMENT_METRICS
        ) or (
            len(keys) == 5
            and keys[0] == "segments"
            and isinstance(keys[1], int)
            and keys[2] == "words"
            and isinstance(keys[3], int)
            and keys[4] == "probability"
        )
        if optional and isinstance(value, float) and not math.isfinite(value):
            warnings.append(path)
            return None
        if isinstance(value, dict):
            return {key: visit(item, (*keys, key), f"{path}.{key}") for key, item in value.items()}
        if isinstance(value, list):
            return [
                visit(item, (*keys, index), f"{path}[{index}]") for index, item in enumerate(value)
            ]
        return value

    return visit(segments, ("segments",), ".segments"), warnings


def transcribe_local(
    audio: Path,
    model: Path,
    output: Path,
    *,
    weights_sha256: str | None = None,
    config_sha256: str | None = None,
    python_sha256: str | None = None,
    parent_pid: int | None = None,
) -> None:
    """Share the existing benchmark's decoding policy without importing app code."""

    if not _supported_host():
        raise RuntimeError("Local MLX Whisper screening requires macOS Apple Silicon.")
    if output.exists() or output.is_symlink():
        raise FileExistsError("ASR evidence already exists; use a fresh attempt.")
    actual_python_sha256 = file_sha256(Path(sys.executable))
    if python_sha256 is not None and actual_python_sha256 != python_sha256:
        raise ValueError("ASR interpreter differs from the pinned installation.")
    model = model.resolve(strict=True)
    weights, config = model / "weights.npz", model / "config.json"
    if not weights.is_file() or weights.stat().st_size < 1_000_000 or not config.is_file():
        raise ValueError(
            "Complete existing local Whisper weights.npz and config.json are required."
        )
    identities = {"weights": file_sha256(weights), "config": file_sha256(config)}
    if weights_sha256 is not None and identities["weights"] != weights_sha256:
        raise ValueError("Local Whisper weights differ from the pinned installation.")
    if config_sha256 is not None and identities["config"] != config_sha256:
        raise ValueError("Local Whisper config differs from the pinned installation.")
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"):
        os.environ[name] = "1"
    stop = threading.Event()

    def watch_parent() -> None:
        while not stop.wait(0.5):
            if parent_pid is not None and os.getppid() != parent_pid:
                os._exit(75)

    watcher = threading.Thread(target=watch_parent, daemon=True)
    if parent_pid is not None:
        if os.getppid() != parent_pid:
            raise RuntimeError("Speech-screen supervisor is no longer the parent.")
        watcher.start()
    lock_fd: int | None = None
    try:
        import fcntl

        lock_started = time.monotonic()
        lock_fd = os.open(GPU_LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        lock_wait_seconds = time.monotonic() - lock_started
        audio_digest = file_sha256(audio)
        import mlx_whisper  # type: ignore[import-not-found]

        started = time.monotonic()
        result = mlx_whisper.transcribe(
            str(audio.resolve()),
            path_or_hf_repo=str(model),
            language="en",
            temperature=0.0,
            condition_on_previous_text=False,
            word_timestamps=True,
            verbose=False,
        )
        elapsed = time.monotonic() - started
        if file_sha256(audio) != audio_digest:
            raise ValueError("Audio changed during ASR inference.")
        if (
            file_sha256(weights) != identities["weights"]
            or file_sha256(config) != identities["config"]
        ):
            raise ValueError("Whisper model files changed during inference.")
        versions = {}
        for package in ("mlx-whisper", "mlx", "mlx-metal", "numpy", "soundfile"):
            try:
                versions[package] = importlib.metadata.version(package)
            except importlib.metadata.PackageNotFoundError:
                versions[package] = "unavailable"
        segments, metric_warnings = _sanitize_optional_metrics(result.get("segments", []))
        report: dict[str, Any] = {
            "engine": "mlx-whisper",
            "model_path": str(model),
            "model_revision": model.name,
            "model_weights_sha256": identities["weights"],
            "model_config_sha256": identities["config"],
            "audio_sha256": audio_digest,
            "text": result["text"],
            "language": result.get("language"),
            "segments": segments,
            "metric_warnings": metric_warnings,
            "inference_seconds": elapsed,
            "versions": versions,
            "python": platform.python_version(),
            "python_executable": sys.executable,
            "python_sha256": actual_python_sha256,
            "platform": platform.platform(),
            "decoding": {
                "language": "en",
                "temperature": 0.0,
                "condition_on_previous_text": False,
                "expected_text_prompted": False,
            },
            "gpu_lock": str(GPU_LOCK),
            "gpu_lock_wait_seconds": lock_wait_seconds,
            "network": "offline; existing local snapshot only",
            "worker_source_sha256": file_sha256(Path(__file__)),
        }
        encoded = json.dumps(report, indent=2, allow_nan=False) + "\n"
        with output.open("x", encoding="utf-8") as destination:
            destination.write(encoded)
            destination.flush()
            os.fsync(destination.fileno())
        output.chmod(0o444)
    finally:
        stop.set()
        if watcher.is_alive():
            watcher.join(timeout=1)
        if lock_fd is not None:
            os.close(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--weights-sha256")
    parser.add_argument("--config-sha256")
    parser.add_argument("--python-sha256")
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    transcribe_local(
        args.audio,
        args.model,
        args.output,
        weights_sha256=args.weights_sha256,
        config_sha256=args.config_sha256,
        python_sha256=args.python_sha256,
        parent_pid=args.parent_pid,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
