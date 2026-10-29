"""Render the exact authored meditation with dots.tts-soar MLX.

This is deliberately a raw bakeoff harness. It performs no trimming, fading,
normalization, resampling, pitch shifting, time stretching, filtering, or other
waveform processing. Each model waveform is stored as native 48 kHz float WAV.
The final file contains those samples unchanged plus only the zero-valued
silences explicitly authored in the source script.

Heavyweight work is guarded by ``--confirm-heavyweight``. Without that flag the
script only validates inputs and prints the deterministic 19-speech-event plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    import numpy as np  # type: ignore[import-not-found]

if TYPE_CHECKING or __package__:
    from scripts.raw_voice_bakeoff import Event, parse_script
else:
    from raw_voice_bakeoff import Event, parse_script


SCHEMA_VERSION = 1
DEFAULT_MODEL = Path("models/managed/installed/dots-tts-soar-mlx-0.7.0/int4")
DEFAULT_REFERENCE = Path("evaluations/manual/raw-voice-bakeoff/voxcpm2-deep-male-smoke.wav")
DEFAULT_REFERENCE_REPORT = Path("evaluations/manual/raw-voice-bakeoff/voxcpm2-deep-male-smoke.json")
DEFAULT_OUTPUT_DIR = Path("evaluations/manual/raw-voice-bakeoff/dots-tts-soar-deep-male-full")
DEFAULT_PLAN_OUTPUT = Path("evaluations/manual/raw-voice-bakeoff/dots-tts-soar-deep-male-plan.json")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_inputs(
    *, script: Path, reference: Path, reference_report: Path
) -> tuple[list[Event], str, dict[str, object]]:
    import soundfile as sf  # type: ignore[import-not-found]

    events = parse_script(script.read_text(encoding="utf-8"))
    speech_count = sum(event.kind == "speech" for event in events)
    silence_count = sum(event.kind == "silence" for event in events)
    if (speech_count, silence_count) != (19, 19):
        raise ValueError(
            f"expected exact 19 speech and 19 silence events, got "
            f"{speech_count} speech and {silence_count} silence"
        )

    report = json.loads(reference_report.read_text(encoding="utf-8"))
    reference_text = report.get("spoken_text")
    if not isinstance(reference_text, str) or not reference_text.strip():
        raise ValueError(f"{reference_report} has no non-empty spoken_text")
    expected_sha = report.get("sha256")
    actual_sha = sha256(reference)
    if expected_sha != actual_sha:
        raise ValueError(
            f"reference SHA-256 mismatch: report={expected_sha!r}, actual={actual_sha}"
        )
    with sf.SoundFile(reference) as audio:
        if audio.samplerate != 48_000 or audio.channels != 1:
            raise ValueError(
                f"reference must be native 48 kHz mono, got "
                f"{audio.samplerate} Hz/{audio.channels} channels"
            )
    return events, reference_text, report


def plan_document(
    *,
    events: list[Event],
    script: Path,
    model: Path,
    reference: Path,
    reference_report: Path,
    reference_text: str,
    base_seed: int,
) -> dict[str, object]:
    speech_number = 0
    planned_events: list[dict[str, object]] = []
    for event in events:
        item = asdict(event)
        if event.kind == "speech":
            speech_number += 1
            item["speech_id"] = f"speech-{speech_number:04d}"
            # Avoid identical random trajectories for repeated phrases while
            # retaining a completely deterministic and inspectable bakeoff.
            item["seed"] = base_seed + speech_number - 1
        planned_events.append(item)
    return {
        "schema_version": SCHEMA_VERSION,
        "model": str(model.resolve()),
        "runtime": "dots-tts-mlx==0.7.0@0d49348b97a2cf31ef02efd5e0e87910acacc38b",
        "script": str(script.resolve()),
        "script_sha256": sha256(script),
        "reference": str(reference.resolve()),
        "reference_sha256": sha256(reference),
        "reference_report": str(reference_report.resolve()),
        "reference_text": reference_text,
        "reference_kind": "consented project-generated synthetic deep-male voice",
        "sample_rate": 48_000,
        "language": "EN",
        "num_steps": 10,
        "guidance_scale": 1.2,
        "speaker_scale": 1.5,
        "base_seed": base_seed,
        "speech_event_count": 19,
        "silence_event_count": 19,
        "authored_silence_seconds": sum(
            float(event.seconds or 0) for event in events if event.kind == "silence"
        ),
        "waveform_processing": [],
        "events": planned_events,
    }


def validate_raw_waveform(waveform: np.ndarray, *, speech_id: str) -> tuple[float, float]:
    import numpy as np  # type: ignore[import-not-found]

    if waveform.ndim != 1 or waveform.size == 0:
        raise RuntimeError(f"{speech_id}: invalid waveform shape {waveform.shape}")
    if not np.isfinite(waveform).all():
        raise RuntimeError(f"{speech_id}: waveform contains non-finite samples")
    peak = float(np.max(np.abs(waveform)))
    rms = float(math.sqrt(float(np.mean(np.square(waveform, dtype=np.float64)))))
    if rms <= 1e-6:
        raise RuntimeError(f"{speech_id}: waveform is silent (RMS {rms})")
    if peak > 1.0:
        raise RuntimeError(f"{speech_id}: waveform exceeds full scale (peak {peak})")
    return peak, rms


def assemble_float_wave(
    *, events: list[Event], speech_dir: Path, output: Path
) -> dict[str, object]:
    """Copy float32 samples and insert exact authored zeros, without DSP."""

    import numpy as np  # type: ignore[import-not-found]
    import soundfile as sf  # type: ignore[import-not-found]

    output.parent.mkdir(parents=True, exist_ok=True)
    speech_number = 0
    total_frames = 0
    sources: list[dict[str, object]] = []
    with sf.SoundFile(
        output,
        mode="w",
        samplerate=48_000,
        channels=1,
        format="WAV",
        subtype="FLOAT",
    ) as destination:
        for event in events:
            if event.kind == "speech":
                speech_number += 1
                path = speech_dir / f"speech-{speech_number:04d}.wav"
                with sf.SoundFile(path) as source:
                    if (
                        source.samplerate != 48_000
                        or source.channels != 1
                        or source.subtype != "FLOAT"
                    ):
                        raise ValueError(
                            f"{path} changed native format: "
                            f"{source.samplerate}Hz/{source.channels}ch/{source.subtype}"
                        )
                    source_frames = source.frames
                    while True:
                        samples = source.read(65_536, dtype="float32", always_2d=True)
                        if samples.size == 0:
                            break
                        destination.write(samples)
                    total_frames += source_frames
                sources.append(
                    {
                        "speech_id": f"speech-{speech_number:04d}",
                        "path": str(path.resolve()),
                        "frames": source_frames,
                        "sha256": sha256(path),
                    }
                )
            else:
                frames = round(float(event.seconds or 0) * 48_000)
                remaining = frames
                zeros = np.zeros((65_536, 1), dtype=np.float32)
                while remaining:
                    count = min(remaining, zeros.shape[0])
                    destination.write(zeros[:count])
                    remaining -= count
                total_frames += frames
    return {
        "output": str(output.resolve()),
        "output_sha256": sha256(output),
        "sample_rate": 48_000,
        "channels": 1,
        "subtype": "FLOAT",
        "frames": total_frames,
        "duration_seconds": total_frames / 48_000,
        "speech_sources": sources,
        "processing": "none; float32 samples copied unchanged; authored zero silence only",
    }


def run_heavyweight(
    *,
    events: list[Event],
    plan: dict[str, object],
    model_path: Path,
    reference: Path,
    reference_text: str,
    output_dir: Path,
    enroll_only: bool,
) -> None:
    try:
        output_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as error:
        raise SystemExit("Evidence already exists; use a new output directory.") from error

    import mlx.core as mx  # type: ignore[import-not-found]
    from dots_tts_mlx.loader import from_pretrained  # type: ignore[import-not-found]

    plan_path = output_dir / "plan.json"
    plan_path.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
    profile_dir = output_dir / "voxcpm2-deep-male.dtprofile"

    load_started = time.perf_counter()
    model = from_pretrained(model_path, dtype=mx.bfloat16).model
    load_seconds = time.perf_counter() - load_started
    enrollment_started = time.perf_counter()
    profile = model.enroll(
        str(reference), reference_text, speaker_scale=float(cast(float, plan["speaker_scale"]))
    )
    profile.save(profile_dir)
    enrollment_seconds = time.perf_counter() - enrollment_started

    enrollment_receipt = {
        "schema_version": SCHEMA_VERSION,
        "profile": str(profile_dir.resolve()),
        "profile_schema_version": profile.schema_version,
        "compat_hash": profile.compat_hash,
        "prompt_patch_count": profile.prompt_patch_count,
        "speaker_scale": profile.speaker_scale,
        "sample_rate": profile.sample_rate,
        "reference": str(reference.resolve()),
        "reference_sha256": sha256(reference),
        "reference_text": reference_text,
        "model_load_seconds": load_seconds,
        "enrolled_during_this_run": True,
        "enrollment_seconds": enrollment_seconds,
        "mlx_peak_bytes": int(mx.get_peak_memory()),
    }
    (output_dir / "enrollment.json").write_text(
        json.dumps(enrollment_receipt, indent=2) + "\n", encoding="utf-8"
    )
    if enroll_only:
        return

    import numpy as np  # type: ignore[import-not-found]
    import soundfile as sf  # type: ignore[import-not-found]

    speech_dir = output_dir / "speech"
    speech_dir.mkdir(parents=True, exist_ok=True)
    speech_events = [event for event in events if event.kind == "speech"]
    planned_speech = [
        event
        for event in cast(list[dict[str, object]], plan["events"])
        if event["kind"] == "speech"
    ]
    for index, (event, event_plan) in enumerate(
        zip(speech_events, planned_speech, strict=True), start=1
    ):
        assert event.text is not None
        speech_id = f"speech-{index:04d}"
        output = speech_dir / f"{speech_id}.wav"
        report_path = speech_dir / f"{speech_id}.json"
        started = time.perf_counter()
        result = model.generate(
            text=event.text,
            profile=profile,
            num_steps=int(cast(int, plan["num_steps"])),
            guidance_scale=float(cast(float, plan["guidance_scale"])),
            language=str(plan["language"]),
            seed=int(cast(int, event_plan["seed"])),
            max_generate_length=500,
            trim_onset=False,
            streaming_decode=True,
        )
        waveform = np.asarray(result["audio"].astype(mx.float32)).reshape(-1)
        sample_rate = int(result["sample_rate"])
        if sample_rate != 48_000:
            raise RuntimeError(f"{speech_id}: model returned {sample_rate} Hz, expected 48000")
        peak, rms = validate_raw_waveform(waveform, speech_id=speech_id)
        sf.write(output, waveform, 48_000, format="WAV", subtype="FLOAT")
        report = {
            "schema_version": SCHEMA_VERSION,
            "speech_id": speech_id,
            "text": event.text,
            "seed": int(cast(int, event_plan["seed"])),
            "sample_rate": 48_000,
            "samples": int(waveform.size),
            "duration_seconds": waveform.size / 48_000,
            "generation_seconds": time.perf_counter() - started,
            "num_patches": result.get("num_patches"),
            "peak": peak,
            "rms": rms,
            "wav_subtype": "FLOAT",
            "waveform_processing": [],
            "sha256": sha256(output),
            "mlx_peak_bytes": int(mx.get_peak_memory()),
        }
        report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(
            f"[dots-full] {speech_id}/speech-0019: "
            f"{report['duration_seconds']:.2f}s audio in "
            f"{report['generation_seconds']:.2f}s",
            flush=True,
        )

    final_output = output_dir / "dots-tts-soar-deep-male-good-night.wav"
    manifest = assemble_float_wave(events=events, speech_dir=speech_dir, output=final_output)
    manifest.update(
        {
            "schema_version": SCHEMA_VERSION,
            "plan": str(plan_path.resolve()),
            "profile": str(profile_dir.resolve()),
            "waveform_processing": [],
        }
    )
    final_output.with_suffix(".manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--script", type=Path, required=True)
    parser.add_argument("--model", type=Path, default=DEFAULT_MODEL)
    parser.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
    parser.add_argument("--reference-report", type=Path, default=DEFAULT_REFERENCE_REPORT)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--plan-output", type=Path, default=DEFAULT_PLAN_OUTPUT)
    parser.add_argument("--base-seed", type=int, default=1742)
    parser.add_argument("--confirm-heavyweight", action="store_true")
    parser.add_argument("--enroll-only", action="store_true")
    args = parser.parse_args()

    events, reference_text, _ = load_inputs(
        script=args.script,
        reference=args.reference,
        reference_report=args.reference_report,
    )
    plan = plan_document(
        events=events,
        script=args.script,
        model=args.model,
        reference=args.reference,
        reference_report=args.reference_report,
        reference_text=reference_text,
        base_seed=args.base_seed,
    )
    if not args.confirm_heavyweight:
        args.plan_output.parent.mkdir(parents=True, exist_ok=True)
        args.plan_output.write_text(json.dumps(plan, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(plan, indent=2))
        print(
            f"\nPLAN ONLY: wrote {args.plan_output}; pass --confirm-heavyweight "
            "after explicit coordination approval.",
            flush=True,
        )
        return 0
    run_heavyweight(
        events=events,
        plan=plan,
        model_path=args.model,
        reference=args.reference,
        reference_text=reference_text,
        output_dir=args.output_dir,
        enroll_only=args.enroll_only,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
