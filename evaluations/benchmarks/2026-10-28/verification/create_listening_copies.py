#!/usr/bin/env python3
"""Constant gain audition copies, only for raw candidates passing verification."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "listening"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def loudness(path: Path) -> dict[str, float]:
    measured = subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-filter_complex",
            "ebur128=peak=true",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stderr
    integrated = re.findall(r"I:\s+(-?[\d.]+) LUFS", measured)
    peaks = re.findall(r"Peak:\s+(-?[\d.]+) dBFS", measured)
    if not integrated or not peaks:
        raise ValueError("FFmpeg did not produce finite integrated loudness/true peak evidence.")
    result = {"integrated_lufs": float(integrated[-1]), "true_peak_dbfs": float(peaks[-1])}
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError("Loudness measurement was nonfinite.")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--candidate",
        action="append",
        default=[],
        help="Only create these report stems; preserve existing other copies.",
    )
    args = parser.parse_args()
    selected = set(args.candidate)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    records = []
    manifest_path = OUTPUT / "manifest.json"
    if selected and manifest_path.exists():
        existing = json.loads(manifest_path.read_text())
        previous_source = existing.get(
            "normalization_helper_source_sha256", existing.get("script_sha256")
        )
        for record in existing["records"]:
            if record["candidate"] not in selected:
                if previous_source:
                    record.setdefault("normalization_helper_source_sha256", previous_source)
                records.append(record)
    processed = set()
    for report_path in sorted(HERE.glob("*.json")):
        if selected and report_path.stem not in selected:
            continue
        report = json.loads(report_path.read_text())
        # Positive tiny segments and synthetic controls are not full audition candidates.
        if report.get("status") != "pass" or not report_path.stem.startswith(
            (
                "voxcpm2-designed-male",
                "kokoro-",
                "baseline-production-kokoro",
                "qwen-",
                "mlx-qwen-",
                "moss-",
                "dots-",
                "chatterbox-",
            )
        ):
            continue
        source = Path(report["audio_path"])
        if source.name not in {"raw.wav", "narration.wav"}:
            continue
        if digest(source) != report["audio_sha256"]:
            raise ValueError(f"Source changed since raw verification: {source}")
        original = loudness(source)
        gain_db = min(-23.0 - original["integrated_lufs"], -1.1 - original["true_peak_dbfs"])
        destination = OUTPUT / f"{report_path.stem}-matched.wav"
        if destination.resolve() == source.resolve():
            raise ValueError("Listening copy must not overwrite raw source.")
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostats",
            "-y",
            "-i",
            str(source),
            "-af",
            f"volume={gain_db:.8f}dB",
            "-c:a",
            "pcm_s16le",
            str(destination),
        ]
        subprocess.run(command, capture_output=True, text=True, check=True)
        final = loudness(destination)
        if final["true_peak_dbfs"] > -1.0:
            raise ValueError(f"Listening true peak exceeded -1dBFS: {destination}")
        records.append(
            {
                "candidate": report_path.stem,
                "raw_verification": str(report_path.relative_to(ROOT)),
                "raw_status": "pass",
                "input": str(source.relative_to(ROOT)),
                "input_sha256": digest(source),
                "output": str(destination.relative_to(ROOT)),
                "output_sha256": digest(destination),
                "input_measurements": original,
                "gain_db": gain_db,
                "output_measurements": final,
                "target_integrated_lufs": -23.0,
                "max_true_peak_dbfs": -1.0,
                "transforms": ["constant volume gain", "PCM16 listening copy"],
                "raw_preserved": True,
                "subjective_voice_quality": "requires listener judgment",
                "normalization_helper_source_sha256": digest(Path(__file__)),
            }
        )
        processed.add(report_path.stem)
    if selected - processed:
        raise ValueError(
            f"Requested candidates did not have eligible pass reports: {selected - processed}"
        )
    version = subprocess.run(
        ["ffmpeg", "-version"], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    manifest = {
        "schema_version": 1,
        "ffmpeg": version,
        "records": records,
        "manifest_writer_source_sha256": digest(Path(__file__)),
    }
    records.sort(key=lambda record: record["candidate"])
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
