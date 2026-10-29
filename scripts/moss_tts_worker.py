# mypy: disable_error_code = import-not-found
"""Persistent JSON-lines bridge for optional MOSS-TTS v1.5 checkpoints.

Both the 5B Local Transformer and 8B flagship expose the same processor API,
but use different model architectures internally. Keeping that distinction in
this isolated worker lets Whoopy present one honest interface without adding
PyTorch and Transformers to its small portable core.
"""

from __future__ import annotations

import argparse
import base64
import inspect
import json
import os
import sys
import wave
from pathlib import Path
from typing import Any

MOSS_DURATION_BUDGET_STRATEGY = "moss-text-pace-v1"
MOSS_MAX_DURATION_TOKENS = 1_500
MOSS_MIN_GENERATION_TOKENS = 96
MOSS_MAX_GENERATION_TOKENS = 1_536


def _validated_generation_budget(request: dict[str, object]) -> tuple[int, int]:
    """Fail closed unless the caller supplies the reviewed bounded policy."""

    if request.get("duration_budget_strategy") != MOSS_DURATION_BUDGET_STRATEGY:
        raise ValueError(f"duration_budget_strategy must be {MOSS_DURATION_BUDGET_STRATEGY}")
    duration_value = request.get("duration_tokens")
    if not isinstance(duration_value, int) or isinstance(duration_value, bool):
        raise ValueError("duration_tokens must be an integer")
    duration_tokens = duration_value
    if not 1 <= duration_tokens <= MOSS_MAX_DURATION_TOKENS:
        raise ValueError("duration_tokens must be between 1 and 1500")
    maximum_value = request.get("max_new_tokens")
    if not isinstance(maximum_value, int) or isinstance(maximum_value, bool):
        raise ValueError("max_new_tokens must be an integer")
    max_new_tokens = maximum_value
    if not MOSS_MIN_GENERATION_TOKENS <= max_new_tokens <= MOSS_MAX_GENERATION_TOKENS:
        raise ValueError("max_new_tokens must be between 96 and 1536")
    maximum_for_duration = min(
        MOSS_MAX_GENERATION_TOKENS,
        max(MOSS_MIN_GENERATION_TOKENS, (duration_tokens * 5 + 3) // 4 + 64),
    )
    if max_new_tokens > maximum_for_duration:
        raise ValueError("max_new_tokens exceeds the reviewed duration-derived ceiling")
    return duration_tokens, max_new_tokens


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--codec", type=Path, required=True)
    parser.add_argument("--reference-audio", type=Path)
    return parser.parse_args()


def _reply(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _load_pcm16_wav(path: Path) -> tuple[Any, int]:
    """Load Whoopy's validated PCM16 reference without TorchCodec/FFmpeg.

    PyTorch 2.9 routes ``torchaudio.load`` through optional TorchCodec. Voice
    references entering this worker are already bounded PCM16 WAV artifacts,
    so requiring a system multimedia decoder would add a large, unrelated
    runtime dependency and weaken offline portability.
    """

    import numpy as np
    import torch

    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        sample_rate = source.getframerate()
        compression = source.getcomptype()
        frames = source.readframes(source.getnframes())
    if channels not in {1, 2} or sample_width != 2 or compression != "NONE":
        raise ValueError("MOSS reference must be uncompressed mono or stereo PCM16 WAV")
    pcm = np.frombuffer(frames, dtype="<i2")
    if pcm.size == 0 or pcm.size % channels:
        raise ValueError("MOSS reference WAV contains invalid or empty PCM frames")
    waveform = pcm.reshape(-1, channels).T.copy()
    return torch.from_numpy(waveform).float().div_(32_768), int(sample_rate)


def main() -> int:
    args = _arguments()

    # MOSS supplies custom Transformers code whose processor does not accept a
    # ``local_files_only`` keyword. The official offline environment switches
    # enforce the same no-network promise without leaking that keyword into the
    # custom processor constructor.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")

    import numpy as np
    import torch
    import torchaudio
    from transformers import AutoModel, AutoProcessor

    model_path = str(args.model.resolve())
    try:
        model_type = str(
            json.loads((args.model / "config.json").read_text(encoding="utf-8"))["model_type"]
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        model_type = "unknown"
    use_cuda = torch.cuda.is_available()
    # PyTorch 2.9's Metal backend is currently unstable for the 8B delay
    # architecture: float16 yields invalid probabilities and float32 can hit
    # an empty-placeholder MPS assertion. Use its verified CPU path on macOS;
    # the 5B Local Transformer remains stable and much faster on Metal.
    use_mps = torch.backends.mps.is_available() and model_type != "moss_tts_delay"
    if use_cuda:
        device = torch.device("cuda")
    elif use_mps:
        device = torch.device("mps")
    else:
        device = torch.device("cpu")
    dtype = torch.float16 if use_cuda or use_mps else torch.float32

    processor = AutoProcessor.from_pretrained(
        model_path,
        trust_remote_code=True,
        codec_path=str(args.codec.resolve()),
    )
    processor.audio_tokenizer = processor.audio_tokenizer.to(device)
    model = AutoModel.from_pretrained(
        model_path,
        trust_remote_code=True,
        attn_implementation="eager",
        dtype=dtype,
        local_files_only=True,
    ).to(device)
    model.eval()

    native_rate = int(processor.model_config.sampling_rate)
    reference_codes = None
    if args.reference_audio is not None:
        # Encoding the same reference for every sentence is expensive. MOSS v1.5
        # uses the stereo Audio Tokenizer v2, while the flagship processor's helper
        # currently downmixes before calling that tokenizer. Prepare the required
        # channel count explicitly so a normal mono consented reference remains
        # usable without changing the original recording on disk.
        reference_waveform, reference_rate = _load_pcm16_wav(args.reference_audio.resolve())
        if int(reference_rate) != native_rate:
            reference_waveform = torchaudio.functional.resample(
                reference_waveform,
                int(reference_rate),
                native_rate,
            )
        required_channels = int(getattr(processor.audio_tokenizer, "number_channels", 1))
        if reference_waveform.shape[0] == 1 and required_channels == 2:
            reference_waveform = reference_waveform.repeat(2, 1)
        elif reference_waveform.shape[0] != required_channels:
            reference_waveform = reference_waveform[:required_channels]
        prepared_reference = processor.loudness_normalize(reference_waveform).to(device)
        encoded_reference = processor.audio_tokenizer.batch_encode(
            [prepared_reference],
            num_quantizers=processor.model_config.n_vq,
        )
        reference_length = int(encoded_reference.audio_codes_lengths[0].item())
        reference_codes = (
            encoded_reference.audio_codes[:, 0, :reference_length]
            .transpose(0, 1)
            .contiguous()
            .cpu()
            .long()
        )
    _reply(
        {
            "status": "ready",
            "sample_rate": 24_000,
            "native_rate": native_rate,
            "device": str(device),
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
            text = str(request["text"]).strip()
            if not text:
                raise ValueError("text cannot be empty")
            seed = int(request.get("seed", 42))
            torch.manual_seed(seed)
            use_reference = bool(request.get("use_reference", True))
            if use_reference and reference_codes is None:
                raise ValueError("reference mode requires --reference-audio at worker startup")
            references = [reference_codes] if use_reference else None
            duration_tokens, max_new_tokens = _validated_generation_budget(request)
            message_arguments: dict[str, object] = {
                "text": text,
                "reference": references,
                "instruction": str(request.get("instruction") or "").strip() or None,
                "language": str(request.get("language") or "English"),
            }
            message_arguments["tokens"] = duration_tokens
            user_message = processor.build_user_message(**message_arguments)
            batch = processor([[user_message]], mode="generation")
            with torch.no_grad():
                # The 5B Local Transformer follows the broader Transformers
                # generation signature and accepts ``do_sample``. The 8B
                # delay model exposes its own explicit sampler and derives the
                # sampling mode from temperature instead. Filter optional
                # controls against the loaded model's real signature so both
                # official checkpoints share this worker without pretending
                # their APIs are identical.
                generation_options: dict[str, object] = {
                    "input_ids": batch["input_ids"].to(device),
                    "attention_mask": batch["attention_mask"].to(device),
                    "max_new_tokens": max_new_tokens,
                    "do_sample": True,
                    "audio_temperature": 1.7,
                    "audio_top_p": 0.8,
                    "audio_top_k": 25,
                    "audio_repetition_penalty": 1.0,
                }
                accepted_options = inspect.signature(model.generate).parameters
                outputs = model.generate(
                    **{
                        name: value
                        for name, value in generation_options.items()
                        if name in accepted_options
                    }
                )
                messages = [message for message in processor.decode(outputs) if message]
            if not messages or not messages[0].audio_codes_list:
                raise RuntimeError("MOSS-TTS returned no audio")
            waveform = messages[0].audio_codes_list[0].float().cpu()
            if waveform.ndim == 1:
                waveform = waveform.unsqueeze(0)
            if waveform.shape[0] > 1:
                waveform = waveform.mean(dim=0, keepdim=True)
            waveform = torchaudio.functional.resample(
                waveform,
                processor.model_config.sampling_rate,
                24_000,
            ).squeeze(0)
            pcm = waveform.clamp(-1, 1).mul(32_767).round().to(torch.int16).numpy()
            _reply(
                {
                    "status": "ok",
                    "request_id": request_id,
                    "pcm_s16le": base64.b64encode(np.asarray(pcm, dtype="<i2").tobytes()).decode(
                        "ascii"
                    ),
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
