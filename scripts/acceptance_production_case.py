"""Run and verify one fresh production prompt-to-audio acceptance case."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from whoopy.audio import AudioManifest, AudioQualityReport
from whoopy.locations import default_config_directory
from whoopy.pipeline import RunStatus, RunStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, choices=(1, 3, 5, 10, 20), required=True)
    parser.add_argument("--runs-dir", type=Path, default=Path("runs"))
    parser.add_argument("--config-dir", type=Path, default=default_config_directory())
    parser.add_argument("--models-dir", type=Path, default=Path("models/managed"))
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--profile", choices=("lite", "standard"), default="standard")
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(json.dumps(receipt, indent=2).encode("utf-8") + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def _rounded_duration_ms(duration_ms: float) -> int:
    """Represent frame-derived fractional milliseconds in the integer receipt schema."""

    return round(duration_ms)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    run_id = uuid4()
    prompt = (
        "Create a calm grounding meditation with sparse guidance, embodied pauses, "
        "and a gentle daytime reorientation."
    )
    command = (
        sys.executable,
        "-m",
        "whoopy",
        "generate",
        prompt,
        "--minutes",
        str(args.minutes),
        "--meditation-kind",
        "grounding",
        "--profile",
        args.profile,
        "--tts-model",
        "kokoro",
        "--run-id",
        str(run_id),
        "--runs-dir",
        str(args.runs_dir.resolve()),
        "--config-dir",
        str(args.config_dir.resolve()),
        "--models-dir",
        str(args.models_dir.resolve()),
        "--audience",
        "system",
        "--json",
    )
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or completed.stdout)
        return completed.returncode

    store = RunStore(args.runs_dir)
    record = store.load(run_id)
    if record.status is not RunStatus.COMPLETED:
        raise RuntimeError(f"Production run ended in {record.status.value}, not completed")
    run_directory = store.run_directory(run_id)
    paths = {
        "run": run_directory / "run.json",
        "timeline": run_directory / "timeline.json",
        "audio": run_directory / "narration.wav",
        "manifest": run_directory / "audio-manifest.json",
        "quality": run_directory / "quality.json",
    }
    if missing := [name for name, path in paths.items() if not path.is_file()]:
        raise RuntimeError(f"Completed run is missing artifacts: {', '.join(missing)}")

    manifest = AudioManifest.model_validate_json(paths["manifest"].read_text(encoding="utf-8"))
    quality = AudioQualityReport.model_validate_json(paths["quality"].read_text(encoding="utf-8"))
    with wave.open(str(paths["audio"]), "rb") as reader:
        playable = (
            reader.getnchannels() == 1
            and reader.getsampwidth() == 2
            and reader.getframerate() == 24_000
            and reader.getnframes() > 0
        )
    requested_ms = args.minutes * 60_000
    duration_error = abs(manifest.duration_ms - requested_ms) / requested_ms
    if not quality.passed or not playable or duration_error > 0.05:
        raise RuntimeError(
            "Production artifacts failed verification: "
            f"quality={quality.passed}, playable={playable}, duration_error={duration_error:.4f}"
        )

    receipt = {
        "schema_version": 2,
        "evidence_scope": "production_prompt_to_audio_case",
        "generated_at": datetime.now(UTC).isoformat(),
        "run_id": str(run_id),
        "command": list(command),
        "profile": args.profile,
        "duration_minutes": args.minutes,
        "actual_duration_ms": _rounded_duration_ms(manifest.duration_ms),
        "quality_passed": quality.passed,
        "artifacts": {
            name: {
                "path": str(path.resolve()),
                "sha256": _sha256(path),
                "bytes": path.stat().st_size,
            }
            for name, path in paths.items()
        },
    }
    args.evidence_dir.mkdir(parents=True, exist_ok=True)
    destination = args.evidence_dir / f"{run_id}.json"
    _write_receipt(destination, receipt)
    print(destination.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
