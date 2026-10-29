#!/usr/bin/env python3
"""Render the exact authored meditation with native Chatterbox V3 audio.

The only samples not returned by the model are zero-valued samples inserted for
the script's explicit silence markers. No generated samples are resampled,
normalized, trimmed, faded, denoised, pitch-shifted, or time-stretched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING or __package__:
    from scripts.raw_voice_bakeoff import parse_script
else:
    from raw_voice_bakeoff import parse_script


def parse_authored_script(script: str) -> tuple[tuple[str, int], ...]:
    """Split exact prose from typed silence without inventing any timing."""

    parsed = parse_script(script)
    if len(parsed) != 38:
        raise ValueError(f"Expected 38 alternating authored events, found {len(parsed)}.")
    paired: list[tuple[str, int]] = []
    for index in range(0, len(parsed), 2):
        speech, silence = parsed[index : index + 2]
        if speech.kind != "speech" or not speech.text:
            raise ValueError("Every authored silence must follow a non-empty speech block.")
        if silence.kind != "silence" or silence.seconds is None:
            raise ValueError("Every speech block must have an authored silence duration.")
        if not silence.seconds.is_integer():
            raise ValueError("This approved script must use whole-second authored silences.")
        paired.append((speech.text, int(silence.seconds)))
    return tuple(paired)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--weights", type=Path, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--pkuseg-home", type=Path, required=True)
    parser.add_argument("--device", choices=("mps", "cpu"), default="mps")
    parser.add_argument("--seed", type=int, default=1742)
    parser.add_argument("--resume", action="store_true")
    return parser


def main() -> None:
    args = _parser().parse_args()
    script_path = args.script.expanduser().resolve(strict=True)
    reference_path = args.reference.expanduser().resolve(strict=True)
    weights_path = args.weights.expanduser().resolve(strict=True)
    pkuseg_home = args.pkuseg_home.expanduser().resolve(strict=True)
    output_directory = args.output_directory.expanduser().resolve()
    segments_directory = output_directory / "segments"
    segments_directory.mkdir(parents=True, exist_ok=True)

    # Keep runtime resources isolated and make accidental model downloads fail.
    os.environ["PKUSEG_HOME"] = str(pkuseg_home)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    import torch  # type: ignore[import-not-found]
    import torchaudio  # type: ignore[import-not-found]
    from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # type: ignore[import-not-found]

    if args.device == "mps" and not torch.backends.mps.is_available():
        raise RuntimeError("Apple MPS was requested but is unavailable in this runtime.")

    events = parse_authored_script(script_path.read_text(encoding="utf-8"))
    started = time.monotonic()
    model = ChatterboxMultilingualTTS.from_local(
        weights_path,
        device=args.device,
        t3_model="v3",
    )
    loaded = time.monotonic()

    # Prepare the approved deep-male reference once. Every speech event then uses
    # the same frozen conditionals and the official generate() defaults.
    model.prepare_conditionals(str(reference_path))

    native_parts = []
    segment_reports = []
    try:
        for index, (spoken_text, silence_seconds) in enumerate(events, start=1):
            segment_seed = args.seed + index - 1
            torch.manual_seed(segment_seed)
            segment_started = time.monotonic()
            segment_path = segments_directory / f"speech-{index:02d}.wav"
            segment_report_path = segments_directory / f"speech-{index:02d}.json"
            expected_identity = {
                "model_revision": "5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18",
                "reference_sha256": sha256(reference_path),
                "text_sha256": hashlib.sha256(spoken_text.encode("utf-8")).hexdigest(),
                "seed": segment_seed,
                "sample_rate_hz": model.sr,
                "controls": "official generate() defaults",
                "postprocessing": [],
            }
            resumed = False
            if args.resume and segment_path.is_file() and segment_report_path.is_file():
                prior = json.loads(segment_report_path.read_text(encoding="utf-8"))
                identity_matches = all(
                    prior.get(key) == value for key, value in expected_identity.items()
                )
                if identity_matches and prior.get("sha256") == sha256(segment_path):
                    info = torchaudio.info(str(segment_path))
                    if (
                        info.sample_rate == model.sr
                        and info.num_channels == 1
                        and info.encoding == "PCM_F"
                        and info.bits_per_sample == 32
                    ):
                        waveform, persisted_rate = torchaudio.load(str(segment_path))
                        resumed = persisted_rate == model.sr
            if resumed:
                segment_finished = segment_started
            else:
                waveform = model.generate(spoken_text, language_id="en")
                segment_finished = time.monotonic()
            if waveform.ndim != 2 or waveform.shape[0] != 1:
                raise RuntimeError(f"Speech event {index} returned an unexpected waveform shape.")
            if not bool(torch.isfinite(waveform).all()):
                raise RuntimeError(f"Speech event {index} returned non-finite samples.")

            if not resumed:
                torchaudio.save(str(segment_path), waveform, model.sr)
                persisted, persisted_rate = torchaudio.load(str(segment_path))
                if persisted_rate != model.sr or not torch.equal(waveform, persisted):
                    raise RuntimeError(f"Speech event {index} changed during WAV persistence.")
            native_parts.append(waveform)

            silence_samples = silence_seconds * model.sr
            native_parts.append(torch.zeros((1, silence_samples), dtype=waveform.dtype))
            segment_report = {
                **expected_identity,
                "index": index,
                "spoken_text": spoken_text,
                "speech_samples": int(waveform.shape[-1]),
                "speech_seconds": waveform.shape[-1] / model.sr,
                "authored_silence_seconds": silence_seconds,
                "synthesis_seconds": segment_finished - segment_started,
                "path": str(segment_path),
                "sha256": sha256(segment_path),
                "resumed": resumed,
            }
            if not resumed:
                segment_report_path.write_text(
                    json.dumps(segment_report, indent=2, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            segment_reports.append(segment_report)
            action = "reused" if resumed else "generated"
            print(f"{action} speech {index}/19", flush=True)

        master = torch.cat(native_parts, dim=-1)
        master_path = output_directory / "chatterbox-v3-deep-male-meditation.wav"
        torchaudio.save(str(master_path), master, model.sr)
        persisted_master, persisted_rate = torchaudio.load(str(master_path))
        if persisted_rate != model.sr or not torch.equal(master, persisted_master):
            raise RuntimeError("Native samples changed during final meditation persistence.")
        finished = time.monotonic()
        report = {
            "model": "Chatterbox Multilingual V3",
            "model_revision": "5bb1f6ee58e50c3b8d408bc82a6d3740c2db6e18",
            "source_revision": "5de7a54aa4e5e2baadb0182dde554908b48b85c2",
            "device": args.device,
            "seed": args.seed,
            "language": "en",
            "reference": str(reference_path),
            "reference_sha256": sha256(reference_path),
            "script": str(script_path),
            "script_sha256": sha256(script_path),
            "sample_rate_hz": model.sr,
            "channels": 1,
            "wav_subtype": "FLOAT",
            "speech_event_count": len(events),
            "authored_silence_seconds": sum(silence for _, silence in events),
            "total_samples": int(master.shape[-1]),
            "total_duration_seconds": master.shape[-1] / model.sr,
            "load_seconds": loaded - started,
            "render_seconds": finished - loaded,
            "controls": {
                "exaggeration": 0.5,
                "cfg_weight": 0.5,
                "temperature": 0.8,
                "repetition_penalty": 1.2,
                "min_p": 0.05,
                "top_p": 1.0,
                "provenance": "official generate() defaults",
            },
            "seed_strategy": "base seed plus zero-based speech index",
            "postprocessing": [],
            "assembly": "unaltered native speech tensors plus authored zero-valued silence only",
            "segments": segment_reports,
            "output": str(master_path),
            "sha256": sha256(master_path),
        }
        report_path = output_directory / "chatterbox-v3-deep-male-meditation.json"
        report_path.write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, indent=2, sort_keys=True))
    finally:
        del model
        if args.device == "mps":
            torch.mps.empty_cache()


if __name__ == "__main__":
    main()
