#!/usr/bin/env python3
"""Plan or render a raw MOSS Local v1.5 voice-cloned meditation.

The heavyweight path follows the upstream Local Transformer v1.5 batch recipe:
CPU/SDPA/float32, official sampling defaults, reference-audio cloning, and the
codec's native 48 kHz stereo float waveform.  Generated samples are never
trimmed, normalized, faded, denoised, resampled, downmixed, clipped, stretched,
pitch shifted, or speed changed.  Assembly only concatenates those samples and
the zero-valued silences explicitly authored in the supplied script.

Inference is impossible unless both the ``render`` command and
``--confirm-heavyweight`` are supplied.  The default ``plan`` command performs
only static validation and emits an inspectable plan.
"""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import math
import os
import random
import struct
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING or __package__:
    from scripts.raw_voice_bakeoff import Event, parse_script
else:
    from raw_voice_bakeoff import Event, parse_script


SCHEMA_VERSION = 1
MODEL_ID = "OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5"
MODEL_REVISION = "be7766a6735b98bd793f7c79fb720b4d0f5d13b8"
MODEL_WEIGHT_SHA256 = "608f1ff64bc6caa9be836060fc7c78a15c4658c4a07b8d73c78d6f70d1b39c23"
CODEC_ID = "OpenMOSS-Team/MOSS-Audio-Tokenizer-v2"
CODEC_REVISION = "f6e20e543b33d2c252a7ef71bdf8aa71e5ff9169"
RUNTIME_REVISION = "58b20a0d5fcc6766658d50967a90a9d890009a46+macos-sdpa-mistral-regex-3"
NATIVE_SAMPLE_RATE = 48_000
NATIVE_CHANNELS = 2
MAX_NEW_TOKENS = 4_096
GENERATION_CONTROLS: dict[str, object] = {
    "do_sample": True,
    "audio_temperature": 1.7,
    "audio_top_p": 0.8,
    "audio_top_k": 25,
    "audio_repetition_penalty": 1.0,
}
POSTPROCESSING: list[str] = []
DEFAULT_MODEL = Path(
    "models/experimental/moss-tts-runtime/checkpoints/MOSS-TTS-Local-Transformer-v1.5"
)
DEFAULT_CODEC = Path("models/experimental/moss-tts-runtime/checkpoints/MOSS-Audio-Tokenizer-v2")
DEFAULT_REFERENCE = Path("evaluations/manual/raw-voice-bakeoff/voxcpm2-deep-male-smoke.wav")
DEFAULT_REFERENCE_REPORT = Path("evaluations/manual/raw-voice-bakeoff/voxcpm2-deep-male-smoke.json")
DEFAULT_OUTPUT_DIRECTORY = Path(
    "evaluations/manual/raw-voice-bakeoff/moss-local-v15-deep-male-full"
)
DEFAULT_PLAN_OUTPUT = Path(
    "evaluations/manual/raw-voice-bakeoff/moss-local-v15-deep-male-plan.json"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json_atomic(path: Path, document: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    partial.replace(path)


def sha256_native_frames(waveform: Any) -> str:
    """Digest the exact interleaved float frames written to a WAV container."""

    import numpy as np  # type: ignore[import-not-found]

    if waveform.dtype != np.float32 or waveform.ndim != 2:
        raise ValueError("native frame digest requires a 2D float32 array")
    return hashlib.sha256(waveform.T.tobytes(order="C")).hexdigest()


def validate_events(events: list[Event]) -> None:
    if len(events) != 38:
        raise ValueError(f"expected 38 alternating authored events, found {len(events)}")
    for index, event in enumerate(events):
        expected_kind = "speech" if index % 2 == 0 else "silence"
        if event.kind != expected_kind:
            raise ValueError(f"event {index + 1} is {event.kind!r}; expected {expected_kind!r}")
        if event.kind == "speech" and not (event.text or "").strip():
            raise ValueError(f"speech event {index // 2 + 1} is empty")
        if event.kind == "silence" and (event.seconds is None or event.seconds <= 0):
            raise ValueError(f"silence event {index // 2 + 1} is not positive")
    silence_total = sum(event.seconds or 0.0 for event in events)
    if not math.isclose(silence_total, 470.0, abs_tol=1e-9):
        raise ValueError(
            f"approved script must contain exactly 470s authored silence, got {silence_total}"
        )


def inspect_float_wav(path: Path) -> dict[str, int]:
    """Inspect an uncompressed IEEE-float RIFF/WAVE without multimedia codecs."""

    with path.open("rb") as source:
        if source.read(4) != b"RIFF":
            raise ValueError(f"{path} is not a RIFF WAV")
        riff_size = struct.unpack("<I", source.read(4))[0]
        if source.read(4) != b"WAVE":
            raise ValueError(f"{path} is not a WAVE file")
        fmt: tuple[int, int, int, int, int, int] | None = None
        data_offset: int | None = None
        data_size: int | None = None
        while source.tell() + 8 <= path.stat().st_size:
            chunk_id = source.read(4)
            chunk_size_bytes = source.read(4)
            if len(chunk_size_bytes) != 4:
                break
            chunk_size = struct.unpack("<I", chunk_size_bytes)[0]
            chunk_data_offset = source.tell()
            if chunk_id == b"fmt ":
                raw = source.read(min(chunk_size, 16))
                if len(raw) < 16:
                    raise ValueError(f"{path} has a truncated fmt chunk")
                fmt = struct.unpack("<HHIIHH", raw)
            elif chunk_id == b"data":
                data_offset = chunk_data_offset
                data_size = chunk_size
            source.seek(chunk_data_offset + chunk_size + (chunk_size % 2))
        if fmt is None or data_offset is None or data_size is None:
            raise ValueError(f"{path} lacks a fmt or data chunk")
    audio_format, channels, sample_rate, byte_rate, block_align, bits = fmt
    if audio_format != 3 or bits != 32:
        raise ValueError(f"{path} is not 32-bit IEEE-float WAV")
    if block_align != channels * 4 or byte_rate != sample_rate * block_align:
        raise ValueError(f"{path} has inconsistent float WAV metadata")
    if data_size <= 0 or data_size % block_align:
        raise ValueError(f"{path} has empty or misaligned float WAV data")
    if data_offset + data_size > path.stat().st_size:
        raise ValueError(f"{path} has truncated float WAV data")
    if riff_size + 8 > path.stat().st_size:
        raise ValueError(f"{path} has a truncated RIFF container")
    return {
        "channels": channels,
        "sample_rate": sample_rate,
        "frames": data_size // block_align,
        "data_offset": data_offset,
        "data_size": data_size,
        "block_align": block_align,
    }


def load_reference_report(reference: Path, report_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("reference report must be a JSON object")
    transcript = report.get("spoken_text")
    if not isinstance(transcript, str) or not transcript.strip():
        raise ValueError("reference report must contain a non-empty spoken_text transcript")
    actual_sha256 = sha256_file(reference)
    if report.get("sha256") != actual_sha256:
        raise ValueError("reference audio does not match its VoxCPM2 report")
    metadata = inspect_float_wav(reference)
    if metadata["sample_rate"] != NATIVE_SAMPLE_RATE or metadata["channels"] != 1:
        raise ValueError("VoxCPM2 reference must remain its native 48 kHz mono waveform")
    return cast(dict[str, Any], report)


def validate_static_inputs(
    *, script: Path, model: Path, codec: Path, reference: Path, reference_report: Path
) -> tuple[str, list[Event], dict[str, Any]]:
    source = script.read_text(encoding="utf-8")
    events = parse_script(source)
    validate_events(events)
    for required in (
        model / "config.json",
        model / "model.safetensors",
        model / "processing_moss_tts.py",
        codec / "config.json",
        codec / "model.safetensors.index.json",
    ):
        if not required.is_file() or required.stat().st_size <= 0:
            raise FileNotFoundError(f"missing required installed MOSS artifact: {required}")
    reference_report_data = load_reference_report(reference, reference_report)
    return source, events, reference_report_data


def segment_seed(base_seed: int, speech_index: int) -> int:
    if base_seed < 0:
        raise ValueError("base seed cannot be negative")
    return (base_seed + speech_index) % (2**31)


def segment_identity(
    *, text: str, seed: int, script_sha256: str, reference_sha256: str
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "model_weight_sha256": MODEL_WEIGHT_SHA256,
        "codec_id": CODEC_ID,
        "codec_revision": CODEC_REVISION,
        "runtime_revision": RUNTIME_REVISION,
        "device": "cpu",
        "dtype": "float32",
        "attention_implementation": "sdpa",
        "script_sha256": script_sha256,
        "reference_sha256": reference_sha256,
        "text_sha256": sha256_text(text),
        "seed": seed,
        "language": "English",
        "voice_mode": "reference",
        "reference_transcript_usage": "provenance_only_not_supported_by_batch_clone_api",
        "duration_tokens": None,
        "max_new_tokens": MAX_NEW_TOKENS,
        "generation_controls": GENERATION_CONTROLS,
        "native_sample_rate": NATIVE_SAMPLE_RATE,
        "native_channels": NATIVE_CHANNELS,
        "wav_subtype": "FLOAT",
        "postprocessing": POSTPROCESSING,
    }


def build_plan(
    *,
    source: str,
    events: list[Event],
    script: Path,
    model: Path,
    codec: Path,
    reference: Path,
    reference_report: Path,
    reference_report_data: dict[str, Any],
    output_directory: Path,
    base_seed: int,
) -> dict[str, object]:
    script_sha256 = sha256_text(source)
    reference_sha256 = sha256_file(reference)
    speech_index = 0
    planned_events: list[dict[str, object]] = []
    for event in events:
        record = asdict(event)
        if event.kind == "speech":
            assert event.text is not None
            record["speech_index"] = speech_index
            record["seed"] = segment_seed(base_seed, speech_index)
            record["identity"] = segment_identity(
                text=event.text,
                seed=segment_seed(base_seed, speech_index),
                script_sha256=script_sha256,
                reference_sha256=reference_sha256,
            )
            speech_index += 1
        planned_events.append(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "plan_only_no_inference",
        "model": str(model.resolve()),
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "codec": str(codec.resolve()),
        "codec_id": CODEC_ID,
        "codec_revision": CODEC_REVISION,
        "runtime_revision": RUNTIME_REVISION,
        "script": str(script.resolve()),
        "script_sha256": script_sha256,
        "reference": str(reference.resolve()),
        "reference_sha256": reference_sha256,
        "reference_report": str(reference_report.resolve()),
        "reference_transcript": reference_report_data["spoken_text"],
        "reference_transcript_usage": (
            "provenance only; the official Local v1.5 batch voice-cloning API "
            "accepts reference audio but has no transcript field"
        ),
        "device": "cpu",
        "dtype": "float32",
        "attention_implementation": "sdpa",
        "language": "English",
        "voice_mode": "reference",
        "speech_event_count": 19,
        "silence_event_count": 19,
        "authored_silence_seconds": 470.0,
        "native_sample_rate": NATIVE_SAMPLE_RATE,
        "native_channels": NATIVE_CHANNELS,
        "wav_subtype": "FLOAT",
        "duration_control": "omitted",
        "max_new_tokens": MAX_NEW_TOKENS,
        "generation_controls": GENERATION_CONTROLS,
        "seed_strategy": "base seed plus zero-based speech index",
        "output_directory": str(output_directory.resolve()),
        "output": str((output_directory / "moss-local-v15-deep-male-good-night.wav").resolve()),
        "assembly": "unaltered native model frames plus authored zero silence only",
        "postprocessing": POSTPROCESSING,
        "events": planned_events,
    }


def validate_native_waveform(waveform: object) -> Any:
    """Validate the decoded tensor without altering its dtype, shape, or samples."""

    import numpy as np
    import torch  # type: ignore[import-not-found]

    if not isinstance(waveform, torch.Tensor):
        raise TypeError("MOSS decoder did not return a torch.Tensor")
    if waveform.device.type != "cpu":
        raise ValueError("MOSS decoder did not return CPU audio")
    if waveform.dtype != torch.float32:
        raise ValueError(f"MOSS decoder returned {waveform.dtype}; refusing to cast")
    if waveform.ndim != 2 or tuple(waveform.shape[:1]) != (NATIVE_CHANNELS,):
        raise ValueError(
            f"MOSS decoder returned {tuple(waveform.shape)}; expected stereo [2, samples]"
        )
    native = waveform.detach().numpy()
    if native.shape[1] <= 0 or not np.isfinite(native).all():
        raise ValueError("MOSS decoder returned empty or non-finite audio")
    peak = float(np.max(np.abs(native)))
    rms = float(math.sqrt(float(np.mean(np.square(native, dtype=np.float64)))))
    if peak <= 1e-5 or rms <= 1e-6:
        raise ValueError("MOSS decoder returned silent audio")
    return native


def _write_float_wav_header(
    destination: Any, *, channels: int, sample_rate: int, data_size: int
) -> None:
    block_align = channels * 4
    destination.write(
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + data_size,
            b"WAVE",
            b"fmt ",
            16,
            3,
            channels,
            sample_rate,
            sample_rate * block_align,
            block_align,
            32,
            b"data",
            data_size,
        )
    )


def write_native_waveform(path: Path, waveform: Any) -> None:
    import numpy as np

    if waveform.dtype != np.float32 or waveform.shape[0] != NATIVE_CHANNELS:
        raise ValueError("native checkpoint must be float32 stereo")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    frames = waveform.T.tobytes(order="C")
    with partial.open("wb") as destination:
        _write_float_wav_header(
            destination,
            channels=NATIVE_CHANNELS,
            sample_rate=NATIVE_SAMPLE_RATE,
            data_size=len(frames),
        )
        destination.write(frames)
    partial.replace(path)
    persisted = load_native_waveform(path, expected_frames_sha256=sha256_native_frames(waveform))
    if not np.array_equal(persisted, waveform):
        raise RuntimeError("native MOSS float samples changed during persistence")


def load_native_waveform(path: Path, *, expected_frames_sha256: str) -> Any:
    import numpy as np

    metadata = inspect_float_wav(path)
    if metadata["sample_rate"] != NATIVE_SAMPLE_RATE or metadata["channels"] != NATIVE_CHANNELS:
        raise ValueError(f"invalid native MOSS checkpoint: {path}")
    with path.open("rb") as source:
        source.seek(metadata["data_offset"])
        raw = source.read(metadata["data_size"])
    waveform = np.frombuffer(raw, dtype="<f4").reshape(-1, NATIVE_CHANNELS).T.copy()
    if sha256_native_frames(waveform) != expected_frames_sha256:
        raise ValueError(f"native frame digest changed for {path}")
    return waveform


def segment_paths(output_directory: Path, speech_index: int) -> tuple[Path, Path]:
    stem = f"speech-{speech_index + 1:04d}"
    directory = output_directory / "speech"
    return directory / f"{stem}.wav", directory / f"{stem}.json"


def resume_segment(
    *, output_directory: Path, speech_index: int, identity: dict[str, object]
) -> tuple[Any, dict[str, Any]] | None:
    audio_path, report_path = segment_paths(output_directory, speech_index)
    if not audio_path.is_file() or not report_path.is_file():
        return None
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if any(report.get(key) != value for key, value in identity.items()):
            return None
        if report.get("wav_sha256") != sha256_file(audio_path):
            return None
        waveform = load_native_waveform(
            audio_path, expected_frames_sha256=str(report["frames_sha256"])
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    return waveform, report


def assemble_native(
    *, events: list[Event], segment_reports: list[dict[str, Any]], output: Path
) -> dict[str, object]:
    """Concatenate native stereo frames and exact authored zero silence."""

    if output.exists():
        raise FileExistsError(f"refusing to overwrite completed output: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_suffix(output.suffix + ".partial")
    if partial.exists():
        partial.unlink()
    event_records: list[dict[str, object]] = []
    speech_index = 0
    frame_cursor = 0
    bytes_per_frame = NATIVE_CHANNELS * 4
    zero_frames = bytes(NATIVE_SAMPLE_RATE * bytes_per_frame)
    with partial.open("wb") as destination:
        _write_float_wav_header(
            destination,
            channels=NATIVE_CHANNELS,
            sample_rate=NATIVE_SAMPLE_RATE,
            data_size=0,
        )
        for event_index, event in enumerate(events):
            start_frame = frame_cursor
            if event.kind == "speech":
                report = segment_reports[speech_index]
                waveform = load_native_waveform(
                    Path(str(report["path"])),
                    expected_frames_sha256=str(report["frames_sha256"]),
                )
                destination.write(waveform.T.tobytes(order="C"))
                frames = int(waveform.shape[1])
                frame_cursor += frames
                event_records.append(
                    {
                        "event_index": event_index,
                        "kind": "speech",
                        "speech_index": speech_index,
                        "start_frame": start_frame,
                        "frames": frames,
                        "frames_sha256": report["frames_sha256"],
                    }
                )
                speech_index += 1
                continue
            assert event.seconds is not None
            frames = round(event.seconds * NATIVE_SAMPLE_RATE)
            remaining = frames
            while remaining:
                count = min(remaining, NATIVE_SAMPLE_RATE)
                destination.write(zero_frames[: count * bytes_per_frame])
                remaining -= count
            frame_cursor += frames
            event_records.append(
                {
                    "event_index": event_index,
                    "kind": "silence",
                    "start_frame": start_frame,
                    "frames": frames,
                    "seconds": event.seconds,
                }
            )
        data_size = frame_cursor * bytes_per_frame
        destination.seek(4)
        destination.write(struct.pack("<I", 36 + data_size))
        destination.seek(40)
        destination.write(struct.pack("<I", data_size))
    partial.replace(output)

    metadata = inspect_float_wav(output)
    if (
        metadata["sample_rate"] != NATIVE_SAMPLE_RATE
        or metadata["channels"] != NATIVE_CHANNELS
        or metadata["frames"] != frame_cursor
    ):
        raise RuntimeError("assembled meditation changed the native MOSS format")
    with output.open("rb") as assembled:
        for record in event_records:
            assembled.seek(
                metadata["data_offset"] + cast(int, record["start_frame"]) * bytes_per_frame
            )
            remaining = cast(int, record["frames"])
            digest = hashlib.sha256() if record["kind"] == "speech" else None
            while remaining:
                count = min(remaining, NATIVE_SAMPLE_RATE)
                chunk = assembled.read(count * bytes_per_frame)
                if len(chunk) != count * bytes_per_frame:
                    raise RuntimeError("assembled meditation ended before its manifest")
                if digest is None:
                    if any(chunk):
                        raise RuntimeError("authored silence contains non-zero samples")
                else:
                    digest.update(chunk)
                remaining -= count
            if digest is not None and digest.hexdigest() != record["frames_sha256"]:
                raise RuntimeError("speech samples changed during assembly")
    return {
        "events": event_records,
        "frames": frame_cursor,
        "duration_seconds": frame_cursor / NATIVE_SAMPLE_RATE,
        "sample_rate": NATIVE_SAMPLE_RATE,
        "channels": NATIVE_CHANNELS,
        "wav_subtype": "FLOAT",
        "authored_silence_seconds": 470.0,
        "output": str(output.resolve()),
        "output_sha256": sha256_file(output),
        "assembly": "unaltered native MOSS float frames plus authored zero silence only",
        "postprocessing": POSTPROCESSING,
    }


def render(plan: dict[str, object], events: list[Event], args: argparse.Namespace) -> None:
    """Run heavyweight inference only after the CLI confirmation gate."""

    if not args.confirm_heavyweight:
        raise SystemExit(
            "INFERENCE BLOCKED: render requires --confirm-heavyweight after explicit approval"
        )
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"

    import numpy as np
    import torch
    import transformers  # type: ignore[import-not-found]
    from transformers import (
        AutoModel,
        AutoProcessor,
    )

    if torch.get_default_dtype() != torch.float32:
        torch.set_default_dtype(torch.float32)
    model_path = args.model.resolve(strict=True)
    codec_path = args.codec.resolve(strict=True)
    output_directory = args.output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    (output_directory / "speech").mkdir(parents=True, exist_ok=True)
    plan_path = output_directory / "plan.json"
    write_json_atomic(plan_path, plan)

    load_started = time.monotonic()
    processor = AutoProcessor.from_pretrained(
        str(model_path),
        trust_remote_code=True,
        codec_path=str(codec_path),
        codec_weight_dtype="fp32",
        codec_compute_dtype="fp32",
        codec_attention_implementation="sdpa",
    )
    processor.audio_tokenizer = processor.audio_tokenizer.to("cpu")
    model = AutoModel.from_pretrained(
        str(model_path),
        trust_remote_code=True,
        attn_implementation="sdpa",
        dtype=torch.float32,
        local_files_only=True,
    ).to("cpu")
    model.eval()
    load_seconds = time.monotonic() - load_started
    if int(processor.model_config.sampling_rate) != NATIVE_SAMPLE_RATE:
        raise RuntimeError("MOSS Local v1.5 did not expose native 48 kHz output")
    reference_metadata = inspect_float_wav(args.reference.resolve(strict=True))
    with args.reference.resolve(strict=True).open("rb") as reference_source:
        reference_source.seek(reference_metadata["data_offset"])
        reference_bytes = reference_source.read(reference_metadata["data_size"])
    reference_array = np.frombuffer(reference_bytes, dtype="<f4").copy()
    reference_tensor = torch.from_numpy(reference_array).reshape(1, -1)
    reference_codes = processor.encode_audios_from_wav(reference_tensor, NATIVE_SAMPLE_RATE)
    if len(reference_codes) != 1:
        raise RuntimeError("MOSS reference enrollment did not return exactly one code tensor")

    script_sha256 = str(plan["script_sha256"])
    reference_sha256 = str(plan["reference_sha256"])
    reports: list[dict[str, Any]] = []
    try:
        speech_index = 0
        for event in events:
            if event.kind != "speech":
                continue
            assert event.text is not None
            seed = segment_seed(args.base_seed, speech_index)
            identity = segment_identity(
                text=event.text,
                seed=seed,
                script_sha256=script_sha256,
                reference_sha256=reference_sha256,
            )
            resumed = (
                resume_segment(
                    output_directory=output_directory,
                    speech_index=speech_index,
                    identity=identity,
                )
                if args.resume
                else None
            )
            if resumed is not None:
                _, report = resumed
                report = dict(report)
                report["resumed"] = True
                reports.append(report)
                print(f"reused speech {speech_index + 1}/19", flush=True)
                speech_index += 1
                continue

            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            conversation = [
                processor.build_user_message(
                    text=event.text,
                    reference=[reference_codes[0]],
                    language="English",
                )
            ]
            batch = processor([conversation], mode="generation")
            started = time.monotonic()
            with torch.no_grad():
                outputs = model.generate(
                    input_ids=batch["input_ids"].to("cpu"),
                    attention_mask=batch["attention_mask"].to("cpu"),
                    max_new_tokens=MAX_NEW_TOKENS,
                    **GENERATION_CONTROLS,
                )
                messages = [message for message in processor.decode(outputs) if message]
            synthesis_seconds = time.monotonic() - started
            if not messages or len(messages[0].audio_codes_list) != 1:
                raise RuntimeError("MOSS Local v1.5 returned no single speech waveform")
            waveform = validate_native_waveform(messages[0].audio_codes_list[0])
            audio_path, report_path = segment_paths(output_directory, speech_index)
            write_native_waveform(audio_path, waveform)
            report = {
                **identity,
                "speech_index": speech_index,
                "text": event.text,
                "samples_per_channel": int(waveform.shape[1]),
                "duration_seconds": waveform.shape[1] / NATIVE_SAMPLE_RATE,
                "synthesis_seconds": synthesis_seconds,
                "peak": float(np.max(np.abs(waveform))),
                "rms": float(math.sqrt(float(np.mean(np.square(waveform, dtype=np.float64))))),
                "frames_sha256": sha256_native_frames(waveform),
                "wav_sha256": sha256_file(audio_path),
                "path": str(audio_path.resolve()),
                "resumed": False,
            }
            write_json_atomic(report_path, report)
            reports.append(report)
            print(
                f"generated speech {speech_index + 1}/19: "
                f"{report['duration_seconds']:.2f}s raw stereo audio in "
                f"{synthesis_seconds:.2f}s",
                flush=True,
            )
            speech_index += 1
    finally:
        del reference_codes
        del model
        del processor
        gc.collect()

    output = output_directory / "moss-local-v15-deep-male-good-night.wav"
    assembly = assemble_native(events=events, segment_reports=reports, output=output)
    manifest = {
        **plan,
        "status": "complete",
        "runtime": {
            "python": sys.version.split()[0],
            "torch": torch.__version__,
            "transformers": transformers.__version__,
        },
        "load_seconds": load_seconds,
        "segments": reports,
        "assembly_result": assembly,
    }
    write_json_atomic(output_directory / "manifest.json", manifest)
    print(json.dumps(assembly, indent=2), flush=True)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    subparsers = result.add_subparsers(dest="command", required=True)
    for command in ("plan", "render"):
        child = subparsers.add_parser(command)
        child.add_argument("--script", required=True, type=Path)
        child.add_argument("--model", type=Path, default=DEFAULT_MODEL)
        child.add_argument("--codec", type=Path, default=DEFAULT_CODEC)
        child.add_argument("--reference", type=Path, default=DEFAULT_REFERENCE)
        child.add_argument("--reference-report", type=Path, default=DEFAULT_REFERENCE_REPORT)
        child.add_argument("--output-directory", type=Path, default=DEFAULT_OUTPUT_DIRECTORY)
        child.add_argument("--plan-output", type=Path, default=DEFAULT_PLAN_OUTPUT)
        child.add_argument("--base-seed", type=int, default=1742)
    render_parser = subparsers.choices["render"]
    render_parser.add_argument("--resume", action="store_true")
    render_parser.add_argument("--confirm-heavyweight", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    script = args.script.resolve(strict=True)
    model = args.model.resolve(strict=True)
    codec = args.codec.resolve(strict=True)
    reference = args.reference.resolve(strict=True)
    reference_report = args.reference_report.resolve(strict=True)
    source, events, reference_report_data = validate_static_inputs(
        script=script,
        model=model,
        codec=codec,
        reference=reference,
        reference_report=reference_report,
    )
    plan = build_plan(
        source=source,
        events=events,
        script=script,
        model=model,
        codec=codec,
        reference=reference,
        reference_report=reference_report,
        reference_report_data=reference_report_data,
        output_directory=args.output_directory,
        base_seed=args.base_seed,
    )
    if args.command == "plan":
        write_json_atomic(args.plan_output.resolve(), plan)
        print(json.dumps(plan, indent=2))
        print(
            f"\nPLAN ONLY: wrote {args.plan_output.resolve()}; "
            "no model was loaded and no inference was run.",
            flush=True,
        )
        return 0
    render(plan, events, args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
