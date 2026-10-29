#!/usr/bin/env python3
"""Render an authored meditation with native VoxCPM2 float audio.

Speech arrays are written exactly as VoxCPM2 returns them.  Assembly adds only
the zero-valued silences explicitly authored in the script.  There is no
normalization, trimming, fading, denoising, resampling, speed or pitch change,
time stretching, or other audio processing.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import random
import time
from dataclasses import asdict
from pathlib import Path
from typing import cast

import numpy as np  # type: ignore[import-not-found]
import soundfile as sf  # type: ignore[import-not-found]

from scripts.raw_voice_bakeoff import Event, parse_script

MODEL_REVISION = "bffb3df5a29440629464e5e839f4d214c8714c3d"
RUNTIME_REVISION = "voxcpm==2.0.3"
NATIVE_SAMPLE_RATE = 48_000
DEFAULT_CONTROL = (
    "A deep adult male voice with a low resonant register, calm grounded warmth, "
    "relaxed natural pacing, gentle bedtime delivery, and clear intimate diction"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_float_array(waveform: np.ndarray) -> str:
    if waveform.dtype != np.float32 or waveform.ndim != 1:
        raise ValueError("array digest requires a one-dimensional float32 waveform")
    return hashlib.sha256(waveform.tobytes(order="C")).hexdigest()


def segment_seed(base_seed: int, speech_index: int) -> int:
    """Use a stable, distinct native sampling seed for every speech section."""

    if base_seed < 0:
        raise ValueError("base seed cannot be negative")
    if speech_index < 0:
        raise ValueError("speech index cannot be negative")
    return (base_seed + speech_index) % (2**31)


def validate_events(events: list[Event]) -> None:
    speech_count = sum(event.kind == "speech" for event in events)
    silence_count = sum(event.kind == "silence" for event in events)
    if speech_count != 19 or silence_count != 19:
        raise ValueError(
            "the approved meditation must contain exactly 19 speech and 19 silence events; "
            f"found {speech_count} speech and {silence_count} silence"
        )
    if events[0].kind != "speech" or events[-1].kind != "silence":
        raise ValueError("the approved meditation must begin with speech and end with silence")
    for event in events:
        if event.kind == "speech" and not (event.text or "").strip():
            raise ValueError("speech events cannot be empty")
        if event.kind == "silence" and (event.seconds is None or event.seconds <= 0):
            raise ValueError("silence events must have a positive authored duration")


def validate_native_waveform(waveform: np.ndarray) -> np.ndarray:
    """Validate without casting, scaling, reshaping samples, or changing length."""

    native = np.asarray(waveform).squeeze()
    if native.dtype != np.float32:
        raise ValueError(f"VoxCPM2 returned {native.dtype}; refusing to cast native audio")
    if native.ndim != 1 or native.size == 0:
        raise ValueError(f"VoxCPM2 returned invalid waveform shape {native.shape}")
    if not np.isfinite(native).all():
        raise ValueError("VoxCPM2 returned NaN or infinite samples")
    peak = float(np.max(np.abs(native)))
    rms = float(math.sqrt(float(np.mean(np.square(native, dtype=np.float64)))))
    if peak <= 1e-5 or rms <= 1e-6:
        raise ValueError("VoxCPM2 returned silent audio")
    return native


def write_exact_float_wav(path: Path, waveform: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, waveform, NATIVE_SAMPLE_RATE, format="WAV", subtype="FLOAT")
    persisted, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    if sample_rate != NATIVE_SAMPLE_RATE or persisted.dtype != np.float32:
        raise RuntimeError(f"native format changed while writing {path}")
    if not np.array_equal(waveform, persisted):
        raise RuntimeError(f"native float samples changed while writing {path}")


def load_segment(path: Path, *, expected_array_sha256: str) -> np.ndarray:
    waveform, sample_rate = sf.read(path, dtype="float32", always_2d=False)
    waveform = validate_native_waveform(waveform)
    if sample_rate != NATIVE_SAMPLE_RATE:
        raise ValueError(f"{path} is {sample_rate} Hz instead of native 48 kHz")
    if sha256_float_array(waveform) != expected_array_sha256:
        raise ValueError(f"native sample digest changed for {path}")
    return waveform


def segment_paths(segment_directory: Path, speech_index: int) -> tuple[Path, Path]:
    stem = f"speech-{speech_index:02d}"
    return segment_directory / f"{stem}.wav", segment_directory / f"{stem}.json"


def resume_segment(
    *,
    segment_directory: Path,
    speech_index: int,
    text: str,
    control: str,
    seed: int,
) -> tuple[np.ndarray, dict[str, object]] | None:
    audio_path, report_path = segment_paths(segment_directory, speech_index)
    if not audio_path.is_file() or not report_path.is_file():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    identity = {
        "runtime": RUNTIME_REVISION,
        "model_revision": MODEL_REVISION,
        "text_sha256": sha256_text(text),
        "control": control,
        "seed": seed,
        "cfg_value": 2.0,
        "inference_timesteps": 10,
        "sample_rate": NATIVE_SAMPLE_RATE,
        "postprocessing": [],
    }
    if any(report.get(key) != value for key, value in identity.items()):
        return None
    if report.get("wav_sha256") != sha256_file(audio_path):
        return None
    waveform = load_segment(audio_path, expected_array_sha256=str(report["array_sha256"]))
    return waveform, report


def assemble_native(
    *,
    events: list[Event],
    segment_records: list[dict[str, object]],
    output: Path,
) -> dict[str, object]:
    """Concatenate persisted native arrays and authored float32 zero silence."""

    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    speech_index = 0
    frame_cursor = 0
    event_records: list[dict[str, object]] = []
    zero_chunk = np.zeros(NATIVE_SAMPLE_RATE, dtype=np.float32)
    with sf.SoundFile(
        partial,
        mode="w",
        samplerate=NATIVE_SAMPLE_RATE,
        channels=1,
        format="WAV",
        subtype="FLOAT",
    ) as destination:
        for event_index, event in enumerate(events):
            event_start = frame_cursor
            if event.kind == "speech":
                record = segment_records[speech_index]
                waveform = load_segment(
                    Path(str(record["path"])),
                    expected_array_sha256=str(record["array_sha256"]),
                )
                destination.write(waveform)
                frame_cursor += int(waveform.size)
                event_records.append(
                    {
                        "event_index": event_index,
                        "kind": "speech",
                        "speech_index": speech_index,
                        "start_frame": event_start,
                        "frames": int(waveform.size),
                        "array_sha256": str(record["array_sha256"]),
                    }
                )
                speech_index += 1
                continue

            assert event.seconds is not None
            silence_frames = round(event.seconds * NATIVE_SAMPLE_RATE)
            remaining = silence_frames
            while remaining:
                count = min(remaining, zero_chunk.size)
                destination.write(zero_chunk[:count])
                remaining -= count
            frame_cursor += silence_frames
            event_records.append(
                {
                    "event_index": event_index,
                    "kind": "silence",
                    "start_frame": event_start,
                    "frames": silence_frames,
                    "seconds": event.seconds,
                }
            )
    partial.replace(output)

    with sf.SoundFile(output) as assembled:
        if (
            assembled.samplerate != NATIVE_SAMPLE_RATE
            or assembled.channels != 1
            or assembled.subtype != "FLOAT"
            or len(assembled) != frame_cursor
        ):
            raise RuntimeError("assembled meditation does not preserve the native format")
        for record in event_records:
            assembled.seek(cast(int, record["start_frame"]))
            remaining = cast(int, record["frames"])
            digest = hashlib.sha256() if record["kind"] == "speech" else None
            while remaining:
                count = min(remaining, NATIVE_SAMPLE_RATE)
                chunk = assembled.read(count, dtype="float32", always_2d=False)
                if chunk.size != count:
                    raise RuntimeError("assembled meditation ended before its manifest")
                if digest is None:
                    if np.any(chunk):
                        raise RuntimeError("authored silence contains non-zero samples")
                else:
                    digest.update(chunk.tobytes(order="C"))
                remaining -= count
            if digest is not None and digest.hexdigest() != record["array_sha256"]:
                raise RuntimeError("speech samples changed during final assembly")

    return {
        "events": event_records,
        "frames": frame_cursor,
        "duration_seconds": frame_cursor / NATIVE_SAMPLE_RATE,
        "sample_rate": NATIVE_SAMPLE_RATE,
        "channels": 1,
        "wav_subtype": "FLOAT",
        "output": str(output.resolve()),
        "output_sha256": sha256_file(output),
        "processing": "none; exact model float arrays plus authored zero silence only",
        "postprocessing": [],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--script", required=True, type=Path)

    render = subparsers.add_parser("render")
    render.add_argument("--script", required=True, type=Path)
    render.add_argument("--model", required=True, type=Path)
    render.add_argument("--output", required=True, type=Path)
    render.add_argument("--device", default="mps", choices=("mps", "cpu"))
    render.add_argument("--base-seed", default=1742, type=int)
    render.add_argument("--control", default=DEFAULT_CONTROL)
    render.add_argument("--resume", action="store_true")
    render.add_argument("--overwrite", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    script_path = args.script.resolve(strict=True)
    source = script_path.read_text(encoding="utf-8")
    events = parse_script(source)
    validate_events(events)
    if args.command == "plan":
        print(
            json.dumps(
                {
                    "script": str(script_path),
                    "script_sha256": sha256_text(source),
                    "speech_events": sum(event.kind == "speech" for event in events),
                    "silence_events": sum(event.kind == "silence" for event in events),
                    "authored_silence_seconds": sum(event.seconds or 0 for event in events),
                    "events": [asdict(event) for event in events],
                },
                indent=2,
            )
        )
        return 0

    import torch  # type: ignore[import-not-found]
    from voxcpm import VoxCPM  # type: ignore[import-not-found]

    model_path = args.model.resolve(strict=True)
    output = args.output.resolve()
    if output.suffix.lower() != ".wav":
        raise SystemExit("--output must end in .wav")
    if output.exists() and not args.overwrite:
        raise SystemExit(f"output already exists; pass --overwrite to replace it: {output}")
    if args.device == "mps" and not torch.backends.mps.is_available():
        raise SystemExit("Apple MPS is unavailable in this process")

    segment_directory = output.parent / f"{output.stem}-segments"
    segment_directory.mkdir(parents=True, exist_ok=True)
    control = args.control.strip().strip("()")
    if not control:
        raise SystemExit("--control cannot be blank")

    load_started = time.monotonic()
    model = VoxCPM.from_pretrained(
        str(model_path),
        load_denoiser=False,
        local_files_only=True,
        optimize=False,
        device=args.device,
    )
    load_seconds = time.monotonic() - load_started
    if int(model.tts_model.sample_rate) != NATIVE_SAMPLE_RATE:
        raise RuntimeError("VoxCPM2 did not expose native 48 kHz output")

    segment_records: list[dict[str, object]] = []
    speech_index = 0
    try:
        for event in events:
            if event.kind != "speech":
                continue
            assert event.text is not None
            seed = segment_seed(args.base_seed, speech_index)
            resumed = (
                resume_segment(
                    segment_directory=segment_directory,
                    speech_index=speech_index,
                    text=event.text,
                    control=control,
                    seed=seed,
                )
                if args.resume
                else None
            )
            if resumed is not None:
                _, report = resumed
                report = dict(report)
                report["resumed"] = True
                segment_records.append(report)
                print(f"reused speech {speech_index + 1}/19", flush=True)
                speech_index += 1
                continue

            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            generated_text = f"({control}){event.text}"
            started = time.monotonic()
            waveform = validate_native_waveform(
                model.generate(
                    text=generated_text,
                    cfg_value=2.0,
                    inference_timesteps=10,
                    normalize=False,
                    denoise=False,
                )
            )
            synthesis_seconds = time.monotonic() - started
            audio_path, report_path = segment_paths(segment_directory, speech_index)
            write_exact_float_wav(audio_path, waveform)
            report = {
                "runtime": RUNTIME_REVISION,
                "model_revision": MODEL_REVISION,
                "device": str(model.tts_model.device),
                "dtype": str(model.tts_model.config.dtype),
                "speech_index": speech_index,
                "text": event.text,
                "text_sha256": sha256_text(event.text),
                "control": control,
                "seed": seed,
                "cfg_value": 2.0,
                "inference_timesteps": 10,
                "sample_rate": NATIVE_SAMPLE_RATE,
                "samples": int(waveform.size),
                "duration_seconds": waveform.size / NATIVE_SAMPLE_RATE,
                "synthesis_seconds": synthesis_seconds,
                "array_sha256": sha256_float_array(waveform),
                "wav_sha256": sha256_file(audio_path),
                "path": str(audio_path.resolve()),
                "wav_subtype": "FLOAT",
                "postprocessing": [],
                "resumed": False,
            }
            report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            segment_records.append(report)
            print(
                f"generated speech {speech_index + 1}/19: "
                f"{waveform.size / NATIVE_SAMPLE_RATE:.2f}s audio in "
                f"{synthesis_seconds:.2f}s",
                flush=True,
            )
            speech_index += 1
    finally:
        del model
        gc.collect()
        if args.device == "mps":
            torch.mps.empty_cache()

    assembly = assemble_native(
        events=events,
        segment_records=segment_records,
        output=output,
    )
    manifest = {
        "schema_version": 1,
        "script": str(script_path),
        "script_sha256": sha256_text(source),
        "model_path": str(model_path),
        "model_revision": MODEL_REVISION,
        "runtime": RUNTIME_REVISION,
        "control": control,
        "base_seed": args.base_seed,
        "seed_strategy": "base_seed_plus_zero_based_speech_index",
        "load_seconds": load_seconds,
        "speech_segments": segment_records,
        **assembly,
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
