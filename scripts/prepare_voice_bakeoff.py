"""Render anonymous Kokoro samples for a human listening review."""

from __future__ import annotations

import argparse
import json
import random
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from whoopy.adapters.tts import SherpaOnnxKokoroAdapter, SherpaOnnxSettings
from whoopy.artifacts import ArtifactStore, TargetPlatform, load_artifact_lock
from whoopy.audio import TimelineWaveRenderer
from whoopy.audio.processing import ProcessedSpeechSynthesizer, SpeechProcessingSettings
from whoopy.timeline import build_script_timeline
from whoopy.voices import KOKORO_ENGLISH_VOICES


def _write(path: Path, payload: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--models-dir", type=Path, default=Path("models/managed"))
    parser.add_argument("--artifact-lock", type=Path, default=Path("config/artifacts.yaml"))
    parser.add_argument(
        "--voices",
        nargs="+",
        choices=tuple(sorted(KOKORO_ENGLISH_VOICES)),
        default=("af_heart", "af_bella", "af_nicole", "am_michael"),
    )
    parser.add_argument("--speed", type=float, default=0.9)
    parser.add_argument("--seed", type=int, default=42)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.output_dir.exists():
        raise SystemExit(f"Refusing to replace existing output: {args.output_dir}")
    script = args.script_file.read_text(encoding="utf-8")
    lock = load_artifact_lock(args.artifact_lock)
    store = ArtifactStore(args.models_dir)
    target = TargetPlatform.current()
    labels = [chr(ord("A") + index) for index in range(len(args.voices))]
    voices = list(args.voices)
    random.Random(args.seed).shuffle(voices)

    args.output_dir.mkdir(parents=True)
    samples = args.output_dir / "samples"
    samples.mkdir()
    sample_keys: dict[str, dict[str, object]] = {}
    for label, voice in zip(labels, voices, strict=True):
        run_id = uuid4()
        timeline = build_script_timeline(
            run_id=run_id,
            script=script,
            created_at=datetime.now(UTC),
        )
        raw = SherpaOnnxKokoroAdapter.from_artifact_store(
            artifact_lock=lock,
            store=store,
            profile_name="basic",
            target=target,
            settings=SherpaOnnxSettings(
                voice_name=voice,
                speaker_id=KOKORO_ENGLISH_VOICES[voice],
                speed=args.speed,
            ),
        )
        rendered = TimelineWaveRenderer(
            ProcessedSpeechSynthesizer(raw, settings=SpeechProcessingSettings())
        ).render(timeline)
        _write(samples / f"sample-{label}.wav", rendered.wave_bytes)
        _write(
            samples / f"sample-{label}-quality.json",
            (rendered.quality.model_dump_json(indent=2) + "\n").encode(),
        )
        sample_keys[label] = {
            "voice": voice,
            "speaker_id": KOKORO_ENGLISH_VOICES[voice],
            "quality_passed": rendered.quality.passed,
            "duration_ms": rendered.quality.duration_ms,
        }

    answer_key = {
        "schema_version": 1,
        "seed": args.seed,
        "speed": args.speed,
        "samples": sample_keys,
    }
    review = {
        "schema_version": 1,
        "instructions": (
            "Listen without opening answer-key.json. Rate every criterion from 1 "
            "(poor) to 5 (excellent), add notes, then choose a preference."
        ),
        "criteria": [
            "naturalness",
            "warmth",
            "calmness",
            "intelligibility",
            "pacing",
            "pause_transitions",
            "absence_of_artifacts",
            "long_listen_comfort",
        ],
        "samples": {
            label: {"ratings": {}, "notes": "", "preference_rank": None} for label in labels
        },
    }
    _write(
        args.output_dir / "review.json",
        (json.dumps(review, indent=2) + "\n").encode(),
    )
    _write(
        args.output_dir / "answer-key.json",
        (json.dumps(answer_key, indent=2) + "\n").encode(),
    )
    print(args.output_dir / "review.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
