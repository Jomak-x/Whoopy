from __future__ import annotations

import hashlib
import json
import math
import shutil
import wave
from array import array
from pathlib import Path

import pytest

from whoopy.audio.ambience import AmbienceError, generate_bed, mix_bed

pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg/FFprobe required"
)


def _wave(path: Path, *, frames: int = 30_017, value: int | None = None) -> None:
    samples = array(
        "h",
        (
            value if value is not None else round(3000 * math.sin(2 * math.pi * 220 * n / 24_000))
            for n in range(frames)
        ),
    )
    import sys

    if sys.byteorder != "little":
        samples.byteswap()
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(samples.tobytes())


@pytest.mark.parametrize("kind", ["rain", "ocean"])
def test_real_generation_repeatable_seed_and_verified_format(tmp_path: Path, kind: str) -> None:
    first = generate_bed(tmp_path / "first", kind=kind, seconds=0.75, seed=12)  # type: ignore[arg-type]
    second = generate_bed(tmp_path / "second", kind=kind, seconds=0.75, seed=12)  # type: ignore[arg-type]
    third = generate_bed(tmp_path / "third", kind=kind, seconds=0.75, seed=13)  # type: ignore[arg-type]
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes() != third.read_bytes()
    manifest = json.loads((first.parent / "manifest.json").read_text())
    evidence = manifest["outputs"]["bed.wav"]
    assert evidence["frames"] == 36_000
    assert evidence["full_decode_verified"]
    assert evidence["clipped_samples"] == 0
    assert evidence["peak_dbfs"] < -12
    assert manifest["seed"] == 12
    assert "unrated" in manifest["quality_status"]
    with wave.open(str(first)) as audio:
        samples = array("h")
        samples.frombytes(audio.readframes(audio.getnframes()))
    assert samples[::2] != samples[1::2]


def test_mix_exact_resampled_duration_input_preservation_and_loop(tmp_path: Path) -> None:
    narration = tmp_path / "narration.wav"
    _wave(narration)
    bed = generate_bed(tmp_path / "bed", seconds=0.6)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (narration, bed)}
    output = mix_bed(tmp_path / "mix", narration=narration, bed=bed)
    manifest = json.loads((output.parent / "manifest.json").read_text())
    evidence = manifest["outputs"]["mix.wav"]
    assert evidence["frames"] == 60_034
    assert evidence["channels"] == 2
    assert evidence["full_decode_verified"]
    assert evidence["clipped_samples"] == 0
    assert manifest["inputs_unchanged_verified"]
    assert manifest["loop_recipe"]["overlap_frames"] > 0
    for path, digest in before.items():
        assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    # A second run also verifies the source loop can repeat beyond one bed duration.
    again = mix_bed(tmp_path / "mix-again", narration=narration, bed=bed)
    assert output.read_bytes() == again.read_bytes()


def test_fractional_resampling_rounds_to_nearest_target_frame(tmp_path: Path) -> None:
    narration = tmp_path / "44100.wav"
    with wave.open(str(narration), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(44_100)
        audio.writeframes(b"\x00\x00" * 32_779)
    bed = generate_bed(tmp_path / "bed", seconds=0.5)
    output = mix_bed(tmp_path / "mix", narration=narration, bed=bed)
    with wave.open(str(output)) as audio:
        assert audio.getnframes() == round(32_779 * 48_000 / 44_100)


def test_truncated_input_is_rejected_before_publication(tmp_path: Path) -> None:
    narration = tmp_path / "truncated.wav"
    _wave(narration)
    narration.write_bytes(narration.read_bytes()[:-3])
    bed = generate_bed(tmp_path / "bed", seconds=0.5)
    with pytest.raises(AmbienceError, match="command failed"):
        mix_bed(tmp_path / "mix", narration=narration, bed=bed)
    assert not (tmp_path / "mix").exists()


@pytest.mark.parametrize("level", [float("nan"), float("inf"), 1, -61])
def test_bad_levels_fail_before_output(tmp_path: Path, level: float) -> None:
    with pytest.raises(AmbienceError, match="Gains"):
        mix_bed(
            tmp_path / "output",
            narration=tmp_path / "missing.wav",
            bed=tmp_path / "bed.wav",
            bed_gain_db=level,
        )
    assert not (tmp_path / "output").exists()


def test_missing_input_and_occupied_output_are_never_replaced(tmp_path: Path) -> None:
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    marker = occupied / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(AmbienceError, match="fresh"):
        generate_bed(occupied, seconds=0.5)
    assert marker.read_text() == "keep"
    with pytest.raises(FileNotFoundError):
        mix_bed(tmp_path / "new", narration=tmp_path / "absent.wav", bed=tmp_path / "absent.wav")
    assert not (tmp_path / "new").exists()


def test_clipping_mix_is_rejected_and_inputs_stay_intact(tmp_path: Path) -> None:
    narration = tmp_path / "narration.wav"
    bed = tmp_path / "bed.wav"
    _wave(narration, value=30_000)
    _wave(bed, value=30_000)
    original = narration.read_bytes(), bed.read_bytes()
    with pytest.raises(AmbienceError, match="clipping"):
        mix_bed(
            tmp_path / "clipped", narration=narration, bed=bed, narration_gain_db=0, bed_gain_db=0
        )
    assert not (tmp_path / "clipped").exists()
    assert (narration.read_bytes(), bed.read_bytes()) == original


@pytest.mark.parametrize("seconds,seed", [(0, 1), (float("nan"), 1), (1, 0), (1, True)])
def test_invalid_generation_parameters(tmp_path: Path, seconds: float, seed: int) -> None:
    with pytest.raises(AmbienceError):
        generate_bed(tmp_path / "output", seconds=seconds, seed=seed)
    assert not (tmp_path / "output").exists()
