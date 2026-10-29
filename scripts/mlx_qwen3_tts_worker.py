# mypy: disable_error_code = import-not-found
"""Experimental, offline MLX Qwen3 JSON-lines worker; no runtime imports at import time."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import platform
import re
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, TextIO

_PROTOCOL_OUTPUT: TextIO = sys.stdout
_SAMPLE_RATE = 24_000
_GIB = 1024**3
_MLX_AUDIO_REVISION = "8c244a1faff44cfa69e3151c195f52a4c1c90be5"
_TOKEN_BUDGET_POLICY = "text_units_x12_plus96_clamped_192_2048@1"


def _require_supported_host(platform_name: str, architecture: str) -> None:
    """Reject unsupported runtimes before importing the optional MLX stack."""
    if platform_name != "darwin" or architecture != "arm64":
        raise RuntimeError(
            "Experimental MLX Qwen requires macOS Apple Silicon (darwin/arm64); "
            f"unsupported host: {platform_name}/{architecture}"
        )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--task", choices=("custom_voice", "voice_design"), required=True)
    parser.add_argument("--raw-output-dir", type=Path, required=True)
    return parser.parse_args()


def _reply(payload: dict[str, object]) -> None:
    _PROTOCOL_OUTPUT.write(json.dumps(payload, separators=(",", ":")) + "\n")
    _PROTOCOL_OUTPUT.flush()


def _isolate_protocol_output() -> None:
    """Keep the original pipe for JSON while redirecting even native fd-1 chatter."""
    global _PROTOCOL_OUTPUT
    sys.stdout.flush()
    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    _PROTOCOL_OUTPUT = os.fdopen(protocol_fd, "w", buffering=1, encoding="utf-8")


def _offline_environment() -> None:
    # Override inherited settings: this experimental worker must never fetch weights.
    for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_HUB_DISABLE_PROGRESS_BARS"):
        os.environ[key] = "1"


def _max_new_tokens(text: str) -> int:
    units = max(1, len(text.split()), math.ceil(len(text.strip()) / 4))
    return min(2048, max(192, 96 + 12 * units))


def _safe_component(value: object, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise ValueError(f"{field} must be a safe nonempty artifact identifier")
    return value


def _request_arguments(request: dict[str, Any], task: str) -> dict[str, Any]:
    if request.get("task") != task:
        raise ValueError("request task does not match loaded checkpoint")
    text = request.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a nonempty string")
    if request.get("language", "English") != "English":
        raise ValueError("experimental MLX Qwen supports English only")
    seed = request.get("seed", 42)
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**32:
        raise ValueError("seed must be an unsigned 32-bit integer")
    _safe_component(request.get("segment_id"), "segment_id")
    _safe_component(request.get("native_artifact_key"), "native_artifact_key")
    instruction = request.get("instruction", "")
    if not isinstance(instruction, str):
        raise ValueError("instruction must be a string")
    kwargs: dict[str, Any] = {
        "text": text.strip(),
        "lang_code": "English",
        "instruct": instruction.strip() or None,
        "max_tokens": _max_new_tokens(text),
        "stream": False,
        "verbose": False,
    }
    if task == "custom_voice":
        if request.get("speaker") not in {"Ryan", "Aiden", "Serena"}:
            raise ValueError("speaker must be Ryan, Aiden, or Serena")
        kwargs["voice"] = request["speaker"]
    else:
        if request.get("speaker") not in (None, ""):
            raise ValueError("voice_design does not accept a speaker")
        if not instruction.strip():
            raise ValueError("voice design instruction cannot be empty")
    return kwargs


def _generator_stop_evidence(iterator: Any) -> dict[str, Any]:
    """Read native generator locals at the nonstreaming yield, as in the benchmark."""
    generator = iterator
    while getattr(generator, "gi_yieldfrom", None) is not None:
        generator = generator.gi_yieldfrom
    frame = getattr(generator, "gi_frame", None)
    values = frame.f_locals if frame is not None else {}
    required = {"is_eos", "generated_codes", "max_tokens"}
    if not required.issubset(values):
        return {
            "codec_eos_observed": False,
            "completion_valid": False,
            "evidence": "native EOS/code-count/budget locals not exposed",
        }
    try:
        eos = values["is_eos"].item()
        steps = len(values["generated_codes"])
        budget = values["max_tokens"]
        # Pinned upstream checks EOS before appending a code. EOS on the final
        # allowed iteration is valid and leaves steps == budget - 1.
        valid = (
            isinstance(eos, bool)
            and eos
            and isinstance(budget, int)
            and not isinstance(budget, bool)
            and 0 < steps < budget
        )
    except (AttributeError, TypeError, ValueError):
        return {
            "codec_eos_observed": False,
            "completion_valid": False,
            "evidence": "native EOS/code-count/budget locals are malformed",
        }
    return {
        "codec_eos_observed": eos is True,
        "completion_valid": valid,
        "generated_codec_steps": steps,
        "max_tokens": budget,
        "evidence": "native generator EOS before code append at nonstreaming yield",
    }


def _admission_limit(psutil: Any) -> int:
    available = int(psutil.virtual_memory().available)
    if available < 16 * _GIB:
        raise RuntimeError("MLX Qwen requires at least 16 GiB available RAM before loading")
    return min(24 * _GIB, available - 6 * _GIB)


class ResourceWatchdog:
    """Stop the entire worker on pressure, including a blocked native Metal call."""

    def __init__(self, psutil: Any) -> None:
        self.psutil = psutil
        self.baseline_swap = int(psutil.swap_memory().used)
        self.low_since: float | None = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._poll, daemon=True)

    def observe(self, available: int, swap: int, now: float) -> str | None:
        if available < 2 * _GIB:
            if self.low_since is None:
                self.low_since = now
            if now - self.low_since >= 2:
                return "available RAM below 2 GiB sustained 2 seconds"
        else:
            self.low_since = None
        if swap - self.baseline_swap > 4 * _GIB:
            return "system swap grew more than 4 GiB above worker baseline"
        return None

    def _poll(self) -> None:
        while not self.stop.wait(0.5):
            try:
                reason = self.observe(
                    int(self.psutil.virtual_memory().available),
                    int(self.psutil.swap_memory().used),
                    time.monotonic(),
                )
            except Exception as error:
                reason = f"resource observer failed: {type(error).__name__}: {error}"
            if reason:
                os.write(2, ("MLX resource watchdog: " + reason + "\n").encode())
                os._exit(75)

    def close(self) -> None:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=1)


def _gpu_lock() -> int:
    if os.name != "posix":
        raise RuntimeError("MLX GPU serialization requires POSIX flock; unsupported platform")
    import fcntl

    # The benchmark and ASR use this same advisory lock. Keep its descriptor until exit.
    fd = os.open("/tmp/whoopy-benchmark-gpu.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except BaseException:
        os.close(fd)
        raise
    return fd


class NativeOutputError(RuntimeError):
    def __init__(self, message: str, evidence: dict[str, Any]) -> None:
        super().__init__(message)
        self.evidence = evidence


def _preserve_invalid_native(
    outputs: list[tuple[Any, Any]],
    *,
    request: dict[str, Any],
    raw_dir: Path,
    model_path: Path,
    kwargs: dict[str, Any],
    stops: list[dict[str, Any]],
    elapsed: float,
    reason: str,
    np: Any,
) -> dict[str, Any]:
    key = _safe_component(request["native_artifact_key"], "native_artifact_key")
    attempt = uuid.uuid4().hex
    folder = raw_dir / f"{key}-{attempt}"
    folder.mkdir(mode=0o700)
    arrays = []
    for index, (audio, sample_rate) in enumerate(outputs):
        returned: dict[str, Any] = {
            "shape": list(audio.shape),
            "dtype": str(audio.dtype),
            "sample_rate_returned": repr(sample_rate),
        }
        if audio.dtype.kind in "biufc":
            path = folder / f"returned-{index:04d}.npy"
            with path.open("xb") as handle:
                np.save(handle, audio, allow_pickle=False)
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(0o444)
            returned.update(
                array_path=str(path),
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            )
        arrays.append(returned)
    sidecar_path = folder / "metadata.json"
    evidence: dict[str, Any] = {
        "status": "invalid",
        "playable": False,
        "artifact_format": "numpy_npy",
        "rejection_reason": reason,
        "returned_outputs": arrays,
        "sidecar_path": str(sidecar_path),
        "inference_seconds": elapsed,
        "attempt_id": attempt,
        "native_artifact_key": key,
        "segment_id": request["segment_id"],
    }
    metadata = {
        **evidence,
        "experimental": True,
        "model_path": str(model_path),
        "request": request,
        "mlx_audio_revision": _MLX_AUDIO_REVISION,
        "generation_arguments": kwargs,
        "stop_evidence": stops,
        "token_budget_policy": _TOKEN_BUDGET_POLICY,
    }
    with sidecar_path.open("x", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    sidecar_path.chmod(0o444)
    evidence["sidecar_sha256"] = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
    return evidence


def _preserve_native(
    audio: Any,
    *,
    request: dict[str, Any],
    raw_dir: Path,
    model_path: Path,
    kwargs: dict[str, Any],
    stop: dict[str, Any],
    elapsed: float,
    pcm_sha256: str,
    sf: Any,
) -> dict[str, Any]:
    key = _safe_component(request["native_artifact_key"], "native_artifact_key")
    attempt = uuid.uuid4().hex
    folder = raw_dir / f"{key}-{attempt}"
    folder.mkdir(mode=0o700)  # exclusive per attempt, never reuse an earlier artifact
    wav_path, sidecar_path = folder / "native.wav", folder / "metadata.json"
    with wav_path.open("xb") as handle:
        sf.write(handle, audio, _SAMPLE_RATE, format="WAV", subtype="FLOAT")
        handle.flush()
        os.fsync(handle.fileno())
    wav_path.chmod(0o444)
    evidence: dict[str, Any] = {
        "wav_path": str(wav_path),
        "sidecar_path": str(sidecar_path),
        "sha256": hashlib.sha256(wav_path.read_bytes()).hexdigest(),
        "frames": int(audio.shape[0]),
        "sample_rate": _SAMPLE_RATE,
        "seed": request.get("seed", 42),
        "eos_observed": stop["codec_eos_observed"],
        "inference_seconds": elapsed,
        "attempt_id": attempt,
        "native_artifact_key": key,
        "segment_id": request["segment_id"],
        "subtype": "FLOAT",
        "pcm_s16le_sha256": pcm_sha256,
        "pcm_conversion": "float32_clip_scale_rint_even_s16le@1",
    }
    metadata = {
        **evidence,
        "experimental": True,
        "task": request["task"],
        "model_path": str(model_path),
        "text": request["text"],
        "language": request.get("language", "English"),
        "speaker": request.get("speaker"),
        "instruction": request.get("instruction", ""),
        "mlx_audio_revision": _MLX_AUDIO_REVISION,
        "generation_arguments": kwargs,
        "stop_evidence": stop,
        "token_budget_policy": _TOKEN_BUDGET_POLICY,
    }
    with sidecar_path.open("x", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    sidecar_path.chmod(0o444)
    evidence["sidecar_sha256"] = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
    return evidence


def _synthesize(
    request: dict[str, Any],
    *,
    task: str,
    model: Any,
    model_path: Path,
    raw_dir: Path,
    mx: Any,
    np: Any,
    sf: Any,
) -> dict[str, Any]:
    kwargs = _request_arguments(request, task)
    mx.random.seed(request.get("seed", 42))
    started = time.monotonic()
    chunks = []
    stops = []
    outputs = []
    iterator = model.generate(**kwargs)
    for result in iterator:
        mx.eval(result.audio)
        audio = np.asarray(result.audio)
        outputs.append((audio, result.sample_rate))
        stops.append(_generator_stop_evidence(iterator))
        if (
            result.sample_rate != _SAMPLE_RATE
            or audio.ndim != 1
            or not audio.size
            or audio.dtype.kind != "f"
            or not np.isfinite(audio).all()
        ):
            reason = "native output must be nonempty finite mono float audio at 24000 Hz"
            evidence = _preserve_invalid_native(
                outputs,
                request=request,
                raw_dir=raw_dir,
                model_path=model_path,
                kwargs=kwargs,
                stops=stops,
                elapsed=time.monotonic() - started,
                reason=reason,
                np=np,
            )
            raise NativeOutputError(reason, evidence)
        chunks.append(audio.astype(np.float32, copy=False))
    elapsed = time.monotonic() - started
    if not chunks:
        raise RuntimeError("MLX Qwen returned no audio")
    audio = np.concatenate(chunks)
    # Multiple nonstreaming yields must each prove EOS; accepting only the last hides truncation.
    stop = {
        "codec_eos_observed": all(item["codec_eos_observed"] for item in stops),
        "completion_valid": all(item["completion_valid"] for item in stops),
        "segments": stops,
        "evidence": "native nonstreaming generator yields",
    }
    pcm = np.rint(np.clip(audio, -1, 1) * 32767).astype("<i2").tobytes()
    evidence = _preserve_native(
        audio,
        request=request,
        raw_dir=raw_dir,
        model_path=model_path,
        kwargs=kwargs,
        stop=stop,
        elapsed=elapsed,
        pcm_sha256=hashlib.sha256(pcm).hexdigest(),
        sf=sf,
    )
    if not stop["completion_valid"]:
        raise NativeOutputError("MLX Qwen output has no valid codec EOS within budget", evidence)
    return {
        "status": "ok",
        "request_id": request.get("request_id"),
        "pcm_s16le": base64.b64encode(pcm).decode("ascii"),
        "raw_evidence": evidence,
        "generation": {
            "mode": "mlx_native_sampled",
            "fallback_used": False,
            "max_new_tokens": kwargs["max_tokens"],
            "token_budget_policy": _TOKEN_BUDGET_POLICY,
        },
    }


def main() -> int:
    args = _arguments()
    _isolate_protocol_output()
    _offline_environment()
    lock_fd: int | None = None
    watchdog: ResourceWatchdog | None = None
    try:
        _require_supported_host(sys.platform, platform.machine())
        if not args.model.is_absolute() or not args.model.is_dir():
            raise ValueError("--model must be an existing absolute local checkpoint directory")
        if not args.raw_output_dir.is_absolute() or not args.raw_output_dir.is_dir():
            raise ValueError("--raw-output-dir must be an existing absolute run directory")
        model_path = args.model.resolve(strict=True)
        raw_dir = args.raw_output_dir.resolve(strict=True)
        lock_fd = _gpu_lock()
        import psutil

        limit = _admission_limit(psutil)
        watchdog = ResourceWatchdog(psutil)
        watchdog.thread.start()
        import mlx.core as mx
        import numpy as np
        import soundfile as sf
        from mlx_audio.tts.utils import load_model

        mx.set_memory_limit(limit)
        model = load_model(str(model_path))
        if getattr(model.config, "tts_model_type", None) != args.task:
            raise ValueError("checkpoint task does not match requested task")
        if int(model.sample_rate) != _SAMPLE_RATE:
            raise ValueError("checkpoint native sample rate must be 24000 Hz")
        _reply(
            {
                "status": "ready",
                "sample_rate": _SAMPLE_RATE,
                "device": "mlx-metal",
                "task": args.task,
                "experimental": True,
                "allocator_limit_bytes": limit,
            }
        )
        for line in sys.stdin:
            request_id: object = None
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise TypeError("request must be a JSON object")
                request_id = request.get("request_id")
                if request.get("action") == "close":
                    _reply({"status": "closing", "request_id": request_id})
                    return 0
                _reply(
                    _synthesize(
                        request,
                        task=args.task,
                        model=model,
                        model_path=model_path,
                        raw_dir=raw_dir,
                        mx=mx,
                        np=np,
                        sf=sf,
                    )
                )
            except Exception as error:
                payload: dict[str, object] = {
                    "status": "error",
                    "request_id": request_id,
                    "error": f"{type(error).__name__}: {error}"[:2000],
                }
                if isinstance(error, NativeOutputError):
                    payload["raw_evidence"] = error.evidence
                _reply(payload)
        return 0
    except Exception as error:
        _reply(
            {
                "status": "error",
                "request_id": None,
                "error": f"{type(error).__name__}: {error}"[:2000],
            }
        )
        return 1
    finally:
        if watchdog is not None:
            watchdog.close()
        if lock_fd is not None:
            os.close(lock_fd)


if __name__ == "__main__":
    raise SystemExit(main())
