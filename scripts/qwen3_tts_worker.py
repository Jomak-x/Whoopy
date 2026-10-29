# mypy: disable_error_code = import-not-found
"""Offline JSON-lines bridge for one pinned Qwen3-TTS checkpoint."""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Literal, TextIO

_PROTOCOL_OUTPUT: TextIO = sys.stdout
_GENERATION_POLICY = "sampled_then_greedy_on_invalid_probabilities@1"
_TOKEN_BUDGET_POLICY = "text_units_x12_plus96_clamped_192_2048@1"
_INVALID_PROBABILITY_ERROR = "probability tensor contains either `inf`, `nan` or element < 0"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument(
        "--task", choices=("custom_voice", "voice_design", "voice_clone"), required=True
    )
    parser.add_argument("--reference-audio", type=Path)
    parser.add_argument("--reference-text", type=Path)
    return parser.parse_args()


def _reply(payload: dict[str, object]) -> None:
    _PROTOCOL_OUTPUT.write(json.dumps(payload, separators=(",", ":")) + "\n")
    _PROTOCOL_OUTPUT.flush()


def _isolate_protocol_output() -> None:
    """Reserve the original stdout pipe and divert dependency chatter to stderr.

    Some optional native tools invoked while importing Qwen write directly to file
    descriptor 1, bypassing Python logging and stream redirects.  The parent process
    requires stdout to contain JSON lines only, so retain a duplicate specifically
    for protocol replies before redirecting fd 1 to the diagnostic stream.
    """

    global _PROTOCOL_OUTPUT
    sys.stdout.flush()
    protocol_fd = os.dup(sys.stdout.fileno())
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    _PROTOCOL_OUTPUT = os.fdopen(
        protocol_fd,
        mode="w",
        buffering=1,
        encoding="utf-8",
        closefd=True,
    )


def _device(torch: Any) -> tuple[str, object]:
    if torch.cuda.is_available():
        return "cuda:0", torch.bfloat16
    if torch.backends.mps.is_available():
        return "mps", torch.float16
    return "cpu", torch.float32


def _pcm_24khz(waveform: object, native_rate: int, *, torch: Any) -> bytes:
    tensor = torch.as_tensor(waveform, dtype=torch.float32).flatten()
    if native_rate != 24_000:
        frames = max(1, round(tensor.numel() * 24_000 / native_rate))
        tensor = torch.nn.functional.interpolate(
            tensor.view(1, 1, -1), size=frames, mode="linear", align_corners=False
        ).flatten()
    pcm = tensor.clamp(-1, 1).mul(32_767).round().to(torch.int16).cpu().numpy()
    return bytes(pcm.astype("<i2", copy=False).tobytes())


def _max_new_tokens(text: str) -> int:
    """Bound codec generation from reviewed text size, including unspaced languages."""

    word_units = len(text.split())
    character_units = math.ceil(len(text.strip()) / 4)
    text_units = max(1, word_units, character_units)
    return min(2_048, max(192, 96 + (12 * text_units)))


def _generate_with_reviewed_fallback(
    generate: Any,
    *,
    generation_arguments: dict[str, object],
    seed: int,
    torch: Any,
) -> tuple[Any, int, dict[str, object]]:
    """Sample once, then retry greedily only for PyTorch's invalid-probability failure."""

    sampled_arguments = {
        **generation_arguments,
        "do_sample": True,
        "subtalker_dosample": True,
    }
    try:
        waveforms, native_rate = generate(**sampled_arguments)
    except RuntimeError as error:
        if _INVALID_PROBABILITY_ERROR not in str(error):
            raise
        torch.manual_seed(seed)
        waveforms, native_rate = generate(
            **generation_arguments,
            do_sample=False,
            subtalker_dosample=False,
        )
        return (
            waveforms,
            int(native_rate),
            {
                "policy": _GENERATION_POLICY,
                "mode": "greedy_invalid_probability_retry",
                "fallback_used": True,
                "primary_error": f"{type(error).__name__}: {error}"[:1_000],
            },
        )
    return (
        waveforms,
        int(native_rate),
        {
            "policy": _GENERATION_POLICY,
            "mode": "sampled",
            "fallback_used": False,
            "primary_error": None,
        },
    )


def main() -> int:
    args = _arguments()
    _isolate_protocol_output()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

    import torch
    from qwen_tts import Qwen3TTSModel

    device, dtype = _device(torch)
    model = Qwen3TTSModel.from_pretrained(
        str(args.model.resolve()),
        device_map=device,
        dtype=dtype,
        attn_implementation="sdpa" if device == "cuda:0" else "eager",
        local_files_only=True,
    )
    clone_prompt = None
    if args.task == "voice_clone":
        if args.reference_audio is None or args.reference_text is None:
            raise ValueError("voice_clone worker requires reference audio and transcript")
        reference_text = args.reference_text.read_text(encoding="utf-8").strip()
        if not reference_text:
            raise ValueError("reference transcript cannot be empty")
        clone_prompt = model.create_voice_clone_prompt(
            ref_audio=str(args.reference_audio.resolve()),
            ref_text=reference_text,
            x_vector_only_mode=False,
        )
    _reply({"status": "ready", "sample_rate": 24_000, "device": device, "task": args.task})

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
            if request.get("task") != args.task:
                raise ValueError("request task does not match loaded checkpoint")
            text = str(request["text"]).strip()
            if not text:
                raise ValueError("text cannot be empty")
            language = str(request.get("language") or "English")
            seed = int(request.get("seed", 42))
            torch.manual_seed(seed)
            max_new_tokens = _max_new_tokens(text)
            task: Literal["custom_voice", "voice_design", "voice_clone"] = args.task
            if task == "custom_voice":
                waveforms, native_rate, generation = _generate_with_reviewed_fallback(
                    model.generate_custom_voice,
                    generation_arguments={
                        "text": text,
                        "language": language,
                        "speaker": str(request["speaker"]),
                        "instruct": str(request.get("instruction") or "") or None,
                        "max_new_tokens": max_new_tokens,
                    },
                    seed=seed,
                    torch=torch,
                )
            elif task == "voice_design":
                instruction = str(request.get("instruction") or "").strip()
                if not instruction:
                    raise ValueError("voice design instruction cannot be empty")
                waveforms, native_rate, generation = _generate_with_reviewed_fallback(
                    model.generate_voice_design,
                    generation_arguments={
                        "text": text,
                        "language": language,
                        "instruct": instruction,
                        "max_new_tokens": max_new_tokens,
                    },
                    seed=seed,
                    torch=torch,
                )
            else:
                waveforms, native_rate, generation = _generate_with_reviewed_fallback(
                    model.generate_voice_clone,
                    generation_arguments={
                        "text": text,
                        "language": language,
                        "voice_clone_prompt": clone_prompt,
                        "max_new_tokens": max_new_tokens,
                    },
                    seed=seed,
                    torch=torch,
                )
            if not waveforms:
                raise RuntimeError("Qwen3-TTS returned no audio")
            pcm = _pcm_24khz(waveforms[0], int(native_rate), torch=torch)
            if not pcm:
                raise RuntimeError("Qwen3-TTS returned empty audio")
            _reply(
                {
                    "status": "ok",
                    "request_id": request_id,
                    "pcm_s16le": base64.b64encode(pcm).decode("ascii"),
                    "generation": {
                        **generation,
                        "max_new_tokens": max_new_tokens,
                        "token_budget_policy": _TOKEN_BUDGET_POLICY,
                    },
                }
            )
        except Exception as error:
            _reply(
                {
                    "status": "error",
                    "request_id": request_id,
                    "error": f"{type(error).__name__}: {error}"[:2_000],
                }
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
