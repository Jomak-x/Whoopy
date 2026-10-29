#!/usr/bin/env python3
"""Verify benchmark WAV speech locally; exits 0 pass, 2 review, 1 fail.

Run using models/experimental/verification-2026-10-28/.venv/bin/python.
Only an existing local model snapshot is accepted. Expected words are NEVER
passed to ASR as a prompt. Each inference takes the benchmark GPU flock.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Load the standalone stdlib verifier without importing the application's optional stack.
SPEC = importlib.util.spec_from_file_location(
    "whoopy_benchmark_verification", ROOT / "src/whoopy/evaluation/audio_verification.py"
)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)
file_sha256 = VERIFIER.file_sha256
verify_audio = VERIFIER.verify_audio
VERIFIER_SOURCE_SHA256 = file_sha256(ROOT / "src/whoopy/evaluation/audio_verification.py")
HARNESS_SOURCE_SHA256 = file_sha256(Path(__file__))

LOCK_PATH = Path("/tmp/whoopy-benchmark-gpu.lock")
DEFAULT_RUNTIME = ROOT / "models/experimental/verification-2026-10-28/.venv/bin/python"

if sys.platform == "win32":

    def _lock_exclusively(file_descriptor: int) -> None:
        raise RuntimeError("The benchmark GPU lock requires a POSIX runtime.")

else:
    import fcntl

    def _lock_exclusively(file_descriptor: int) -> None:
        fcntl.flock(file_descriptor, fcntl.LOCK_EX)


def default_model() -> Path:
    cache = Path.home() / ".cache/huggingface/hub/models--mlx-community--whisper-small-mlx"
    revision_file = cache / "refs/main"
    if revision_file.exists():
        return cache / "snapshots" / revision_file.read_text().strip()
    return cache / "snapshots/MISSING"


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def asr_worker(audio: Path, model: Path, output: Path) -> None:
    # Fail before importing Hugging Face clients if the local snapshot is absent.
    model = model.resolve(strict=True)
    weights = model / "weights.npz"
    if not weights.is_file() or weights.stat().st_size < 1_000_000:
        raise ValueError("Complete local Whisper weights.npz is required.")
    if not (model / "config.json").is_file():
        raise ValueError("Local Whisper config.json is required.")
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    lock_started = time.monotonic()
    with LOCK_PATH.open("a") as lock:
        _lock_exclusively(lock.fileno())
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
            raise ValueError("Audio changed during ASR inference; refusing stale evidence.")
    versions = {}
    for package in ("mlx-whisper", "mlx", "mlx-metal", "numpy", "soundfile"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    write_json(
        output,
        {
            "engine": "mlx-whisper",
            "model_path": str(model),
            "model_revision": model.name,
            "model_weights_sha256": file_sha256(weights),
            "model_config_sha256": file_sha256(model / "config.json"),
            "audio_sha256": audio_digest,
            "text": result["text"],
            "language": result.get("language"),
            "segments": result.get("segments", []),
            "inference_seconds": elapsed,
            "versions": versions,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "decoding": {
                "language": "en",
                "temperature": 0.0,
                "condition_on_previous_text": False,
                "expected_text_prompted": False,
            },
            "gpu_lock": str(LOCK_PATH),
            "gpu_lock_wait_seconds": lock_wait_seconds,
            "network": "offline; existing local snapshot only",
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    text = parser.add_mutually_exclusive_group()
    text.add_argument("--expected-text")
    text.add_argument("--expected-file", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--mode", choices=("speech", "narration"), default="speech")
    parser.add_argument("--model", type=Path, default=default_model())
    parser.add_argument("--asr-python", type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument(
        "--timeout", type=float, default=900, help="Includes waiting for another benchmark GPU job."
    )
    parser.add_argument("--asr-worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not args.asr_worker and args.expected_file is None and args.expected_text is None:
        parser.error("--expected-file or --expected-text is required")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    try:
        with args.report.open("x", encoding="utf-8") as receipt:
            receipt.write(
                json.dumps({"status": "incomplete", "reason": "Verification has not completed."})
                + "\n"
            )
    except FileExistsError as existing_report_error:
        raise SystemExit(
            "Evidence already exists; use a new report path."
        ) from existing_report_error
    if args.asr_worker:
        asr_worker(args.audio, args.model, args.report)
        return 0
    expected = (
        args.expected_file.read_text(encoding="utf-8") if args.expected_file else args.expected_text
    )
    asr, error = None, None
    with tempfile.TemporaryDirectory(prefix="whoopy-asr-") as temporary:
        raw_report = Path(temporary) / "asr.json"
        try:
            command = [
                str(args.asr_python.absolute()),
                str(Path(__file__).resolve()),
                str(args.audio.resolve()),
                "--model",
                str(args.model.resolve()),
                "--report",
                str(raw_report),
                "--asr-worker",
            ]
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=args.timeout, check=False
            )
            if process.returncode:
                error = f"ASR worker exited {process.returncode}: {process.stderr[-3000:]}"
            else:
                asr = json.loads(raw_report.read_text())
                if asr.get("audio_sha256") != file_sha256(args.audio):
                    raise ValueError("ASR result audio hash differs from verified input.")
        except (OSError, ValueError, subprocess.TimeoutExpired) as failure:
            asr, error = None, str(failure)
    report = verify_audio(args.audio, expected, asr_result=asr, asr_error=error, mode=args.mode)
    report["verifier_source_sha256"] = VERIFIER_SOURCE_SHA256
    report["harness_source_sha256"] = HARNESS_SOURCE_SHA256
    write_json(args.report, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(args.report.resolve()),
                "asr_text": asr.get("text") if asr else None,
                "content": {
                    k: v for k, v in (report["content"] or {}).items() if k not in {"alignment"}
                },
                "failure_reasons": report["failure_reasons"],
                "review_reasons": report["review_reasons"],
            },
            indent=2,
        )
    )
    return {"pass": 0, "review": 2, "fail": 1}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
