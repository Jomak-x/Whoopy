"""Persistent JSON-lines bridge for optional MOSS-TTS v1.5 checkpoints.

Both the 5B Local Transformer and 8B flagship expose the same processor API,
but use different model architectures internally. Keeping that distinction in
this isolated worker lets Whoopy present one honest interface without adding
PyTorch and Transformers to its small portable core.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--codec", type=Path, required=True)
    parser.add_argument("--reference-audio", type=Path, required=True)
    return parser.parse_args()


def _reply(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


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

    # Prefer Apple Metal on a capable Mac, but retain the official CPU path so
    # the optional model fails gracefully into slower execution on other
    # laptops instead of being needlessly tied to one operating system.
    use_mps = torch.backends.mps.is_available()
    device = torch.device("mps" if use_mps else "cpu")
    dtype = torch.float16 if use_mps else torch.float32
    model_path = str(args.model.resolve())

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

    # Encoding the same reference for every sentence is expensive. The MOSS
    # processor accepts its token matrix directly, so compute it once.
    reference_codes = processor.encode_audios_from_path(
        [str(args.reference_audio.resolve())],
        n_vq=processor.model_config.n_vq,
    )[0]
    _reply(
        {
            "status": "ready",
            "sample_rate": 24_000,
            "native_rate": processor.model_config.sampling_rate,
            "device": str(device),
        }
    )

    for line in sys.stdin:
        try:
            request = json.loads(line)
            text = str(request["text"]).strip()
            if not text:
                raise ValueError("text cannot be empty")
            seed = int(request.get("seed", 42))
            torch.manual_seed(seed)
            references = [reference_codes] if request.get("use_reference", True) else None
            user_message = processor.build_user_message(
                text=text,
                reference=references,
                instruction=str(request.get("instruction") or "").strip() or None,
                language=str(request.get("language") or "English"),
            )
            batch = processor([[user_message]], mode="generation")
            with torch.no_grad():
                outputs = model.generate(
                    input_ids=batch["input_ids"].to(device),
                    attention_mask=batch["attention_mask"].to(device),
                    max_new_tokens=2_048,
                    do_sample=True,
                    audio_temperature=1.7,
                    audio_top_p=0.8,
                    audio_top_k=25,
                    audio_repetition_penalty=1.0,
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
                    "pcm_s16le": base64.b64encode(np.asarray(pcm, dtype="<i2").tobytes()).decode(
                        "ascii"
                    ),
                }
            )
        except Exception as error:
            _reply({"status": "error", "error": f"{type(error).__name__}: {error}"})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
