#!/usr/bin/env python3
"""Generate one untouched native Chatterbox Multilingual V3 WAV sample.

This intentionally performs no resampling, normalization, time stretching,
pitch shifting, filtering, trimming, or other waveform post-processing.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import torch  # type: ignore[import-not-found]
import torchaudio  # type: ignore[import-not-found]
from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # type: ignore[import-not-found]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--language", default="en")
    parser.add_argument("--reference", type=Path)
    parser.add_argument("--device", choices=("mps", "cpu"), default="mps")
    parser.add_argument("--seed", type=int, default=20260805)
    return parser


def main() -> None:
    args = _parser().parse_args()
    weights = args.weights.expanduser().resolve(strict=True)
    reference = args.reference.expanduser().resolve(strict=True) if args.reference else None
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    if args.device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("Apple MPS was requested but is unavailable in this runtime.")

    torch.manual_seed(args.seed)
    started = time.monotonic()
    model = ChatterboxMultilingualTTS.from_local(
        weights,
        device=args.device,
        t3_model="v3",
    )
    loaded = time.monotonic()
    waveform = model.generate(
        args.text,
        language_id=args.language,
        audio_prompt_path=str(reference) if reference else None,
    )
    generated = time.monotonic()

    # Save the model tensor exactly at the model's native sample rate.
    torchaudio.save(str(output), waveform, model.sr)
    audio_info = torchaudio.info(str(output))
    frames = audio_info.num_frames
    sample_rate = audio_info.sample_rate
    channels = audio_info.num_channels

    print(
        json.dumps(
            {
                "model": "Chatterbox Multilingual V3",
                "device": args.device,
                "seed": args.seed,
                "reference": str(reference) if reference else "official built-in conditionals",
                "sample_rate_hz": sample_rate,
                "channels": channels,
                "frames": frames,
                "duration_seconds": frames / sample_rate,
                "load_seconds": loaded - started,
                "generation_seconds": generated - loaded,
                "output": str(output),
                "waveform_postprocessing": "none",
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
