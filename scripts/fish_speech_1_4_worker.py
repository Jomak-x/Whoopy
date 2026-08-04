# mypy: disable_error_code = import-not-found
"""Persistent JSON-lines bridge for the optional local Fish Speech 1.4 runtime.

This script runs inside Fish's isolated Python 3.10 environment. Whoopy keeps
the process alive for a complete render so the two neural networks are loaded
once rather than once per sentence. Protocol messages use stdout; model logs
stay on stderr.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--reference-audio", type=Path, required=True)
    parser.add_argument("--reference-text", type=Path, required=True)
    return parser.parse_args()


def _reply(payload: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    args = _arguments()
    runtime = args.runtime.resolve()
    sys.path.insert(0, str(runtime))

    import numpy as np
    import torch
    import torchaudio
    from fish_speech.utils import set_seed
    from tools.llama.generate import generate_long, load_model
    from tools.vqgan.inference import load_model as load_decoder

    if not torch.backends.mps.is_available():
        raise RuntimeError("Fish Speech 1.4 currently requires Apple Metal in Whoopy")
    device = torch.device("mps")
    checkpoint = args.checkpoint.resolve()
    decoder_checkpoint = checkpoint / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth"

    text_model, decode_one_token = load_model(
        checkpoint,
        device=device,
        precision=torch.float32,
        compile=False,
    )
    with torch.device(device):
        text_model.setup_caches(
            max_batch_size=1,
            max_seq_len=text_model.config.max_seq_len,
            dtype=next(text_model.parameters()).dtype,
        )
    decoder = load_decoder(
        "firefly_gan_vq",
        decoder_checkpoint,
        device=str(device),
    )

    reference, source_rate = torchaudio.load(str(args.reference_audio))
    if reference.shape[0] > 1:
        reference = reference.mean(dim=0, keepdim=True)
    reference = torchaudio.functional.resample(
        reference,
        source_rate,
        decoder.spec_transform.sample_rate,
    )
    reference = reference[None].to(device)
    reference_lengths = torch.tensor([reference.shape[2]], device=device, dtype=torch.long)
    prompt_tokens = decoder.encode(reference, reference_lengths)[0][0]
    prompt_text = args.reference_text.read_text(encoding="utf-8").strip()
    if not prompt_text:
        raise RuntimeError("Fish reference transcript is empty")

    _reply({"status": "ready", "sample_rate": 24_000, "device": str(device)})
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
            set_seed(int(request.get("seed", 42)))
            chunks: list[torch.Tensor] = []
            for result in generate_long(
                model=text_model,
                device=device,
                decode_one_token=decode_one_token,
                text=text,
                max_new_tokens=0,
                top_p=0.7,
                repetition_penalty=1.2,
                temperature=0.7,
                compile=False,
                iterative_prompt=False,
                chunk_length=0,
                max_length=4_096,
                prompt_text=prompt_text,
                prompt_tokens=prompt_tokens,
            ):
                if result.action != "sample" or result.codes is None:
                    continue
                feature_lengths = torch.tensor([result.codes.shape[1]], device=device)
                decoded, _ = decoder.decode(
                    indices=result.codes[None],
                    feature_lengths=feature_lengths,
                )
                chunks.append(decoded[0, 0].float().cpu())
            if not chunks:
                raise RuntimeError("Fish returned no audio")
            waveform = torch.cat(chunks).unsqueeze(0)
            waveform = torchaudio.functional.resample(
                waveform,
                decoder.spec_transform.sample_rate,
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
