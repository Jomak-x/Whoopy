from __future__ import annotations

import hashlib
import json
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest

import scripts.moss_local_v15_raw_meditation as moss
from scripts.moss_local_v15_raw_meditation import (
    GENERATION_CONTROLS,
    MAX_NEW_TOKENS,
    assemble_native,
    build_plan,
    inspect_float_wav,
    load_reference_report,
    resume_segment,
    segment_identity,
    segment_paths,
    sha256_file,
    validate_events,
)
from scripts.raw_voice_bakeoff import Event, parse_script

ROOT = Path(__file__).parents[1]


@pytest.fixture
def script(tmp_path: Path) -> Path:
    """A small authored fixture meeting the harness's 19-pair/470-second contract."""
    durations = [20] * 17 + [70, 60]
    source = "\n\n".join(
        f"Rest here for passage {index + 1}.\n\nKeep your own words.\n\n"
        f"[Pause for {seconds} seconds]"
        for index, seconds in enumerate(durations)
    )
    path = tmp_path / "authored.txt"
    path.write_text(source, encoding="utf-8")
    return path


def _float_wav(path: Path, frames: bytes, *, channels: int = 1) -> None:
    """Write a tiny IEEE-float WAV fixture using only the standard library."""
    rate = 48_000
    path.parent.mkdir(parents=True, exist_ok=True)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(frames),
        b"WAVE",
        b"fmt ",
        16,
        3,
        channels,
        rate,
        rate * channels * 4,
        channels * 4,
        32,
        b"data",
        len(frames),
    )
    path.write_bytes(header + frames)


@pytest.fixture
def reference(tmp_path: Path) -> tuple[Path, Path]:
    audio = tmp_path / "reference.wav"
    _float_wav(audio, struct.pack("<4f", 0.1, -0.2, 0.3, -0.4))
    report = tmp_path / "reference.json"
    report.write_text(
        json.dumps(
            {"spoken_text": "These are the exact reference words.", "sha256": sha256_file(audio)}
        ),
        encoding="utf-8",
    )
    return audio, report


def _fake_install(tmp_path: Path) -> tuple[Path, Path]:
    model = tmp_path / "model"
    codec = tmp_path / "codec"
    model.mkdir()
    codec.mkdir()
    for path in (
        model / "config.json",
        model / "model.safetensors",
        model / "processing_moss_tts.py",
        codec / "config.json",
        codec / "model.safetensors.index.json",
    ):
        path.write_text("{}", encoding="utf-8")
    return model, codec


def test_authored_script_preserves_prose_and_nineteen_pairs_of_silence(script: Path) -> None:
    events = parse_script(script.read_text(encoding="utf-8"))
    validate_events(events)

    assert len(events) == 38
    assert sum(event.kind == "speech" for event in events) == 19
    assert sum(event.seconds or 0 for event in events) == 470
    assert events[0].text == "Rest here for passage 1.\n\nKeep your own words."
    assert events[-2].text == "Rest here for passage 19.\n\nKeep your own words."
    assert events[-1].seconds == 60


def test_reference_report_is_bound_to_audio_and_exact_transcript(
    reference: tuple[Path, Path],
) -> None:
    audio, report_path = reference
    report = load_reference_report(audio, report_path)
    assert report["spoken_text"] == "These are the exact reference words."
    assert report["sha256"] == sha256_file(audio)

    with audio.open("ab") as destination:
        destination.write(b"tampered")
    with pytest.raises(ValueError, match="does not match"):
        load_reference_report(audio, report_path)


def test_plan_omits_duration_forcing_and_records_transcript_as_provenance(
    tmp_path: Path,
    script: Path,
    reference: tuple[Path, Path],
) -> None:
    source = script.read_text(encoding="utf-8")
    events = parse_script(source)
    model, codec = _fake_install(tmp_path)
    audio, report_path = reference
    report = load_reference_report(audio, report_path)
    plan = build_plan(
        source=source,
        events=events,
        script=script,
        model=model,
        codec=codec,
        reference=audio,
        reference_report=report_path,
        reference_report_data=report,
        output_directory=tmp_path / "output",
        base_seed=1742,
    )

    assert plan["status"] == "plan_only_no_inference"
    assert plan["device"] == "cpu"
    assert plan["dtype"] == "float32"
    assert plan["native_sample_rate"] == 48_000
    assert plan["native_channels"] == 2
    assert plan["duration_control"] == "omitted"
    assert plan["max_new_tokens"] == MAX_NEW_TOKENS == 4096
    assert plan["generation_controls"] == GENERATION_CONTROLS
    assert plan["authored_silence_seconds"] == 470.0
    assert plan["reference_transcript"] == report["spoken_text"]
    assert "provenance only" in str(plan["reference_transcript_usage"])
    speech = [
        event for event in cast(list[dict[str, Any]], plan["events"]) if event["kind"] == "speech"
    ]
    assert len(speech) == 19
    assert all(event["identity"]["duration_tokens"] is None for event in speech)
    assert [event["seed"] for event in speech[:3]] == [1742, 1743, 1744]


@dataclass(frozen=True)
class _NativeFrames:
    """Already decoded native frames at the optional NumPy loader boundary."""

    frames: bytes

    @property
    def shape(self) -> tuple[int, int]:
        return 2, len(self.frames) // 8

    @property
    def T(self) -> _NativeFrames:
        return self

    def tobytes(self, *, order: str) -> bytes:
        assert order == "C"
        return self.frames


def test_native_stereo_float_assembly_preserves_speech_and_zero_silence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = struct.pack("<6f", 0.1, -0.4, -0.2, 0.5, 0.3, -0.6)
    second = struct.pack("<4f", 0.7, 0.9, -0.8, -1.0)
    decoded: dict[Path, _NativeFrames] = {}
    reports: list[dict[str, Any]] = []
    for index, frames in enumerate((first, second)):
        path, _ = segment_paths(tmp_path, index)
        _float_wav(path, frames, channels=2)
        decoded[path] = _NativeFrames(frames)
        reports.append({"path": str(path), "frames_sha256": hashlib.sha256(frames).hexdigest()})

    def load(path: Path, *, expected_frames_sha256: str) -> _NativeFrames:
        frames = decoded[path]
        assert hashlib.sha256(frames.frames).hexdigest() == expected_frames_sha256
        return frames

    # Exercise the real assembler and its persisted-frame checks without loading
    # NumPy or any model runtime. Only decoding the tiny fixtures is injected.
    monkeypatch.setattr(moss, "load_native_waveform", load)
    events = [
        Event(kind="speech", text="One."),
        Event(kind="silence", seconds=0.25),
        Event(kind="speech", text="Two."),
        Event(kind="silence", seconds=0.5),
    ]
    output = tmp_path / "assembled.wav"
    manifest = assemble_native(events=events, segment_reports=reports, output=output)
    metadata = inspect_float_wav(output)
    frames = output.read_bytes()[metadata["data_offset"] :]
    assert frames == first + bytes(12_000 * 8) + second + bytes(24_000 * 8)
    assert metadata["channels"] == 2
    assert metadata["sample_rate"] == 48_000
    assert metadata["frames"] == 36_005
    assert manifest["postprocessing"] == []


def test_resume_requires_full_identity_and_untampered_native_audio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frames = struct.pack("<4f", 0.1, -0.1, 0.2, -0.2)
    path, report_path = segment_paths(tmp_path, 0)
    _float_wav(path, frames, channels=2)
    identity = segment_identity(
        text="Rest now.",
        seed=1742,
        script_sha256="script",
        reference_sha256="reference",
    )
    report = {
        **identity,
        "path": str(path),
        "frames_sha256": hashlib.sha256(frames).hexdigest(),
        "wav_sha256": sha256_file(path),
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")
    loads: list[Path] = []

    def load(audio: Path, *, expected_frames_sha256: str) -> _NativeFrames:
        loads.append(audio)
        assert expected_frames_sha256 == hashlib.sha256(frames).hexdigest()
        return _NativeFrames(frames)

    monkeypatch.setattr(moss, "load_native_waveform", load)
    resumed = resume_segment(output_directory=tmp_path, speech_index=0, identity=identity)
    assert resumed == (_NativeFrames(frames), report)
    changed = {**identity, "seed": 999}
    assert resume_segment(output_directory=tmp_path, speech_index=0, identity=changed) is None
    with path.open("ab") as destination:
        destination.write(b"tampered")
    assert resume_segment(output_directory=tmp_path, speech_index=0, identity=identity) is None
    assert loads == [path]


def test_render_command_cannot_load_model_without_confirmation(
    tmp_path: Path,
    script: Path,
    reference: tuple[Path, Path],
) -> None:
    model, codec = _fake_install(tmp_path)
    audio, report_path = reference
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/moss_local_v15_raw_meditation.py"),
            "render",
            "--script",
            str(script),
            "--model",
            str(model),
            "--codec",
            str(codec),
            "--reference",
            str(audio),
            "--reference-report",
            str(report_path),
            "--output-directory",
            str(tmp_path / "output"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "INFERENCE BLOCKED" in result.stderr
    assert not (tmp_path / "output").exists()
