"""Optional offline synthetic beds and derived mixes; narration stays authoritative.

Only stdlib and an existing FFmpeg/FFprobe installation are used. These recipes
are procedural noise, not field recordings or AI-generated natural ambience.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import wave
from array import array
from pathlib import Path
from sys import byteorder
from typing import Any, Literal

RATE = 48_000
KINDS = ("rain", "ocean")


class AmbienceError(ValueError):
    """Invalid input or an unverified derived audio artifact."""


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(arguments: list[str]) -> str:
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AmbienceError(f"Audio command failed: {error}") from error
    if result.returncode:
        raise AmbienceError(f"Audio command failed: {result.stderr[-3000:]}")
    return result.stdout


def _tools() -> tuple[str, str, str]:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise AmbienceError("Install FFmpeg and FFprobe before preparing ambience.")
    return ffmpeg, ffprobe, _run([ffmpeg, "-version"]).splitlines()[0]


def _input(path: Path) -> tuple[Path, dict[str, Any]]:
    path = path.expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".wav":
        raise AmbienceError("An explicit local PCM WAV file is required.")
    try:
        with wave.open(str(path), "rb") as audio:
            if (
                audio.getcomptype() != "NONE"
                or audio.getnchannels() not in (1, 2)
                or audio.getsampwidth() not in (2, 3, 4)
                or not 8_000 <= audio.getframerate() <= 192_000
                or audio.getnframes() < 1
            ):
                raise AmbienceError("Expected nonempty mono/stereo 16/24/32-bit PCM WAV.")
            frames = audio.getnframes()
            rate = audio.getframerate()
    except (wave.Error, EOFError) as error:
        raise AmbienceError(f"Unreadable PCM WAV: {path}") from error
    return path, {"path": str(path), "sha256": _hash(path), "frames": frames, "rate": rate}


def _decode(ffmpeg: str, path: Path) -> None:
    _run([ffmpeg, "-v", "error", "-xerror", "-nostdin", "-i", str(path), "-f", "null", "-"])


def _verify(ffmpeg: str, ffprobe: str, path: Path, frames: int) -> dict[str, Any]:
    _decode(ffmpeg, path)
    probe = json.loads(_run([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)]))[
        "streams"
    ]
    if len(probe) != 1 or probe[0].get("codec_name") != "pcm_s16le":
        raise AmbienceError("Derived output must contain exactly one PCM s16le stream.")
    peak = 0
    clipped = 0
    with wave.open(str(path), "rb") as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getnframes()) != (RATE, 2, frames):
            raise AmbienceError("Derived output duration or stereo format changed.")
        count = 0
        while data := audio.readframes(32_768):
            samples = array("h")
            samples.frombytes(data)
            if byteorder != "little":
                samples.byteswap()
            count += len(samples)
            peak = max(peak, max(abs(sample) for sample in samples))
            clipped += sum(abs(sample) >= 32_767 for sample in samples)
    if count != frames * 2 or clipped:
        raise AmbienceError("Derived audio is truncated or reaches PCM clipping; reduce gains.")
    return {
        "sha256": _hash(path),
        "frames": frames,
        "sample_rate": RATE,
        "channels": 2,
        "duration_seconds": frames / RATE,
        "peak_dbfs": 20 * math.log10(peak / 32_768) if peak else None,
        "clipped_samples": clipped,
        "full_decode_verified": True,
        "true_peak_verified": False,
    }


def _target(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise AmbienceError("Output directory must be fresh; existing paths are never overwritten.")
    if not path.parent.is_dir():
        raise AmbienceError("Output directory parent must already exist.")
    return path


def _publish(staging: Path, destination: Path, manifest: dict[str, Any]) -> None:
    (staging / "manifest.json").write_text(
        json.dumps(manifest, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    # mkdir is exclusive, so a concurrent/new destination cannot be replaced.
    destination.mkdir()
    try:
        for child in staging.iterdir():
            child.rename(destination / child.name)
    except BaseException:
        shutil.rmtree(destination)
        raise


def _base(version: str) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ffmpeg_version": version,
        "source_type": "procedural synthetic noise; not a natural recording or AI",
        "quality_status": "unrated; requires human listening",
        "determinism": "same recipe, seed and FFmpeg build; no cross-platform hash promise",
        "canonical_narration_modified": False,
        "ducking": False,
    }


def generate_bed(
    output_dir: Path,
    *,
    kind: Literal["rain", "ocean"] = "rain",
    seconds: float = 60,
    seed: int = 17,
) -> Path:
    """Create an audition bed. Ocean is slow synthetic swells, rain filtered noise."""
    destination = _target(output_dir)
    if kind not in KINDS or not math.isfinite(seconds) or not 0.25 <= seconds <= 3_600:
        raise AmbienceError("Choose rain/ocean and a duration from 0.25 to 3600 seconds.")
    if isinstance(seed, bool) or not isinstance(seed, int) or not 1 <= seed <= 2_147_483_646:
        raise AmbienceError("Seed must be an integer from 1 to 2147483646.")
    ffmpeg, ffprobe, version = _tools()
    frames = round(seconds * RATE)
    # Independent channel seeds avoid identical stereo; gentle filtering limits hiss.
    filters = (
        "highpass=f=90,lowpass=f=3200"
        if kind == "rain"
        else ("highpass=f=65,lowpass=f=1800,tremolo=f=0.105:d=0.70")
    )
    # Short click-prevention edges stay inside the much longer loop crossfade.
    fade = min(0.05, seconds / 4)
    graph = (
        f"[0:a]{filters}[l];[1:a]{filters}[r];"
        "[l][r]join=inputs=2:channel_layout=stereo:map=0.0-FL|1.0-FR,"
        f"volume=0.45,afade=t=in:d={fade},"
        f"afade=t=out:st={max(0, frames / RATE - fade)}:d={fade},"
        f"atrim=end_sample={frames}[out]"
    )
    with tempfile.TemporaryDirectory(prefix=".ambience-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        output = staging / "bed.wav"
        command = [ffmpeg, "-v", "error", "-nostdin", "-n"]
        for channel_seed in (seed, seed + 1):
            command += [
                "-f",
                "lavfi",
                "-i",
                f"anoisesrc=color=pink:amplitude=0.35:sample_rate={RATE}:seed={channel_seed}",
            ]
        command += [
            "-filter_complex_threads",
            "1",
            "-filter_complex",
            graph,
            "-map",
            "[out]",
            "-c:a",
            "pcm_s16le",
            "-ar",
            str(RATE),
            "-ac",
            "2",
            str(output),
        ]
        _run(command)
        manifest = _base(version) | {
            "operation": "generate_bed",
            "kind": kind,
            "seed": seed,
            "channel_seeds": [seed, seed + 1],
            "filter_recipe": graph,
            "noise_amplitude": 0.35,
            "bed_volume_multiplier": 0.45,
            "loudness_target_lufs": None,
            "levels": "relative amplitude only; not loudness normalized",
            "inputs": [],
            "outputs": {"bed.wav": _verify(ffmpeg, ffprobe, output, frames)},
        }
        _publish(staging, destination, manifest)
    return destination / "bed.wav"


def _gain(value: float) -> None:
    if not math.isfinite(value) or not -60 <= value <= 0:
        raise AmbienceError("Gains must be finite values between -60 and 0 dB.")


def _loop(ffmpeg: str, source: Path, target: Path, temporary: Path) -> dict[str, Any]:
    """One bounded PCM loop: body then tail/head linear crossfade, no endpoint fade."""
    _run(
        [
            ffmpeg,
            "-v",
            "error",
            "-nostdin",
            "-n",
            "-i",
            str(source),
            "-af",
            "aresample=48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            str(temporary),
        ]
    )
    with wave.open(str(temporary), "rb") as audio:
        length = audio.getnframes()
        if not RATE // 2 <= length <= RATE * 600:
            raise AmbienceError("Bed must be from 0.5 to 600 seconds for bounded loop preparation.")
        samples = array("h")
        samples.frombytes(audio.readframes(length))
    if byteorder != "little":
        samples.byteswap()
    overlap = min(2 * RATE, length // 4)
    result = samples[2 * overlap : 2 * (length - overlap)]
    for frame in range(overlap):
        fraction = frame / (overlap - 1)
        for channel in range(2):
            head = samples[2 * frame + channel]
            tail = samples[2 * (length - overlap + frame) + channel]
            result.append(round(tail * (1 - fraction) + head * fraction))
    if byteorder != "little":
        result.byteswap()
    with wave.open(str(target), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(RATE)
        output.writeframes(result.tobytes())
    temporary.unlink()
    return {
        "method": "body then linear tail/head crossfade; repeat resulting PCM",
        "source_frames_at_48k": length,
        "overlap_frames": overlap,
        "loop_frames": length - overlap,
        "warning": "Crossfade prevents a hard edit; audible repetition still needs listening.",
    }


def mix_bed(
    output_dir: Path,
    *,
    narration: Path,
    bed: Path,
    narration_gain_db: float = -3,
    bed_gain_db: float = -18,
) -> Path:
    """Write a derived stereo audition mix, preserving all input files byte for byte."""
    destination = _target(output_dir)
    _gain(narration_gain_db)
    _gain(bed_gain_db)
    narration, speech_info = _input(narration)
    bed, bed_info = _input(bed)
    # Nearest 48k frame: at most half a target sample differs from source duration.
    frames = (speech_info["frames"] * RATE + speech_info["rate"] // 2) // speech_info["rate"]
    if frames < 1 or frames > RATE * 3_600:
        raise AmbienceError("Narration must be nonempty and at most one hour.")
    if not 0.5 <= bed_info["frames"] / bed_info["rate"] <= 600:
        raise AmbienceError("Bed must be from 0.5 to 600 seconds for bounded loop preparation.")
    ffmpeg, ffprobe, version = _tools()
    _decode(ffmpeg, narration)
    _decode(ffmpeg, bed)
    graph = (
        f"[0:a]aresample={RATE},aformat=channel_layouts=stereo,"
        f"apad,atrim=end_sample={frames},volume={narration_gain_db}dB[voice];"
        f"[1:a]volume={bed_gain_db}dB,atrim=end_sample={frames}[bed];"
        "[voice][bed]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]"
    )
    with tempfile.TemporaryDirectory(prefix=".ambience-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        loop = staging / "loop.wav"
        loop_recipe = _loop(ffmpeg, bed, loop, staging / "resampled.wav")
        output = staging / "mix.wav"
        _run(
            [
                ffmpeg,
                "-v",
                "error",
                "-nostdin",
                "-n",
                "-i",
                str(narration),
                "-stream_loop",
                "-1",
                "-i",
                str(loop),
                "-filter_complex_threads",
                "1",
                "-filter_complex",
                graph,
                "-map",
                "[out]",
                "-c:a",
                "pcm_s16le",
                "-ar",
                str(RATE),
                "-ac",
                "2",
                str(output),
            ]
        )
        outputs = {
            "mix.wav": _verify(ffmpeg, ffprobe, output, frames),
            "loop.wav": _verify(ffmpeg, ffprobe, loop, loop_recipe["loop_frames"]),
        }
        for path, info in ((narration, speech_info), (bed, bed_info)):
            if _hash(path) != info["sha256"]:
                raise AmbienceError("Input changed while preparing mix; refusing stale evidence.")
        manifest = _base(version) | {
            "operation": "mix_existing_bed",
            "source_type": "explicit local WAV inputs",
            "inputs": [speech_info, bed_info],
            "inputs_unchanged_verified": True,
            "narration_gain_db": narration_gain_db,
            "bed_gain_db": bed_gain_db,
            "loudness_target_lufs": None,
            "filter_recipe": graph,
            "loop_recipe": loop_recipe,
            "duration_rule": "nearest 48k frame to narration; padded/trimmed after resampling",
            "outputs": outputs,
        }
        _publish(staging, destination, manifest)
    return destination / "mix.wav"
