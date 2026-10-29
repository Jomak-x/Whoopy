#!/usr/bin/env python3
"""Generate one unprocessed VoxCPM2 sample from an already-downloaded model.

This is deliberately separate from Whoopy's production audio pipeline.  It
writes the model waveform at the model's native sample rate as float WAV: no
resampling, time stretching, loudness normalization, denoising, or trimming.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import time
from pathlib import Path

import numpy as np  # type: ignore[import-not-found]
import soundfile as sf  # type: ignore[import-not-found]
import torch  # type: ignore[import-not-found]
from voxcpm import VoxCPM  # type: ignore[import-not-found]

EXPECTED_SAMPLE_RATE = 48_000


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--text", required=True)
    parser.add_argument(
        "--control",
        help="VoxCPM2 voice-design instruction; passed to the model in parentheses.",
    )
    parser.add_argument("--device", default="mps", choices=("mps", "cpu", "auto"))
    parser.add_argument("--seed", default=1742, type=int)
    parser.add_argument("--cfg-value", default=2.0, type=float)
    parser.add_argument("--inference-timesteps", default=10, type=int)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    model_path = args.model.resolve(strict=True)
    if not (model_path / "config.json").is_file():
        raise SystemExit(f"Not a VoxCPM model directory: {model_path}")
    if args.output.suffix.lower() != ".wav":
        raise SystemExit("--output must end in .wav")
    if args.device == "mps" and not torch.backends.mps.is_available():
        raise SystemExit("Apple MPS is unavailable in this process")

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    generated_text = args.text.strip()
    if not generated_text:
        raise SystemExit("--text cannot be empty")
    if args.control:
        control = args.control.strip().strip("()")
        if not control:
            raise SystemExit("--control cannot be blank")
        generated_text = f"({control}){generated_text}"

    load_started = time.monotonic()
    model = VoxCPM.from_pretrained(
        str(model_path),
        load_denoiser=False,
        local_files_only=True,
        optimize=False,
        device=args.device,
    )
    load_seconds = time.monotonic() - load_started

    synthesis_started = time.monotonic()
    waveform = np.asarray(
        model.generate(
            text=generated_text,
            cfg_value=args.cfg_value,
            inference_timesteps=args.inference_timesteps,
            normalize=False,
            denoise=False,
        ),
        dtype=np.float32,
    ).squeeze()
    synthesis_seconds = time.monotonic() - synthesis_started

    sample_rate = int(model.tts_model.sample_rate)
    if sample_rate != EXPECTED_SAMPLE_RATE:
        raise RuntimeError(f"Expected native 48 kHz output, got {sample_rate} Hz")
    if waveform.ndim != 1 or waveform.size == 0:
        raise RuntimeError(f"VoxCPM2 returned invalid waveform shape {waveform.shape}")
    if not np.isfinite(waveform).all():
        raise RuntimeError("VoxCPM2 returned NaN or infinite samples")
    peak = float(np.max(np.abs(waveform)))
    rms = float(math.sqrt(float(np.mean(np.square(waveform, dtype=np.float64)))))
    if peak <= 1e-5 or rms <= 1e-6:
        raise RuntimeError("VoxCPM2 returned silent audio")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # FLOAT stores the generated float32 waveform without PCM quantization.
    sf.write(args.output, waveform, sample_rate, format="WAV", subtype="FLOAT")
    output_bytes = args.output.read_bytes()
    report = {
        "model_path": str(model_path),
        "runtime": "voxcpm==2.0.3",
        "device": str(model.tts_model.device),
        "dtype": str(model.tts_model.config.dtype),
        "seed": args.seed,
        "cfg_value": args.cfg_value,
        "inference_timesteps": args.inference_timesteps,
        "control": args.control,
        "spoken_text": args.text.strip(),
        "sample_rate": sample_rate,
        "samples": int(waveform.size),
        "duration_seconds": waveform.size / sample_rate,
        "load_seconds": load_seconds,
        "synthesis_seconds": synthesis_seconds,
        "peak": peak,
        "rms": rms,
        "wav_subtype": "FLOAT",
        "postprocessing": [],
        "sha256": hashlib.sha256(output_bytes).hexdigest(),
    }
    report_path = args.output.with_suffix(".json")
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
