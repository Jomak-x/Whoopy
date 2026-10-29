#!/usr/bin/env python3
"""Qualify an assembled raw file from exact paragraph ASR + decoded PCM assembly.

Run in the isolated verification runtime. No model inference occurs here. The
original full-file ASR report is preserved and explicitly cited, even if it fails.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "benchmark_audio_verifier", ROOT / "src/whoopy/evaluation/audio_verification.py"
)
assert SPEC is not None and SPEC.loader is not None
VERIFIER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VERIFIER)


def require(condition: object, message: str) -> None:
    if not condition:
        raise ValueError(message)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate")
    parser.add_argument("--segment-reports", type=Path, nargs="+", required=True)
    args = parser.parse_args()
    generation_path = HERE.parent / "voices" / args.candidate / "report.json"
    generation = json.loads(generation_path.read_text())
    expected_path = HERE.parent / "voice-script.txt"
    expected = expected_path.read_text()
    paragraphs = [part.strip() for part in expected.split("\n\n") if part.strip()]
    native_segments = generation["segments"]
    require(
        generation["script_sha256"] == VERIFIER.file_sha256(expected_path), "Script hash changed"
    )
    require([segment["text"] for segment in native_segments] == paragraphs, "Paragraphs differ")
    require(len(args.segment_reports) == len(native_segments), "Missing segment reports")
    raw_path = generation_path.parent / "raw.wav"
    full_report_path = HERE / f"{args.candidate}.json"
    full_report = json.loads(full_report_path.read_text())
    require(full_report["audio_sha256"] == VERIFIER.file_sha256(raw_path), "Raw hash changed")
    require(full_report["expected_transcript"] == expected, "Full-file expectation differs")
    raw_samples, raw_rate = sf.read(str(raw_path), dtype="float32", always_2d=True)
    require(np.isfinite(raw_samples).all(), "Raw samples contain nonfinite values")
    decoded_segments = []
    evidence = []
    observed = []
    revisions = set()
    for native, paragraph, report_path in zip(
        native_segments, paragraphs, args.segment_reports, strict=True
    ):
        report = json.loads(report_path.read_text())
        source = ROOT / native["path"]
        source_hash = VERIFIER.file_sha256(source)
        require(source_hash == native["sha256"] == report["audio_sha256"], "Segment hash changed")
        require(Path(report["audio_path"]).resolve() == source.resolve(), "ASR audio path differs")
        require(
            report["expected_transcript"].strip() == paragraph, "ASR expected paragraph differs"
        )
        require(report["status"] == "pass", "Segment verification did not pass")
        # This aggregate demands exact independently recognized normalized words.
        require(report["content"]["word_error_rate"] == 0, "Segment ASR contains word errors")
        require(report["content"]["ordered_word_coverage"] == 1, "Segment coverage is incomplete")
        samples, rate = sf.read(str(source), dtype="float32", always_2d=True)
        require(
            rate == raw_rate and samples.shape[1] == raw_samples.shape[1], "Audio formats differ"
        )
        require(np.isfinite(samples).all(), "Segment samples contain nonfinite values")
        decoded_segments.append(samples)
        observed.append(report["asr"]["text"])
        revisions.add((report["asr"]["model_revision"], report["asr"]["model_weights_sha256"]))
        evidence.append(
            {
                "segment": native["path"],
                "audio_sha256": source_hash,
                "expected_text": paragraph,
                "frame_count": samples.shape[0],
                "report": str(report_path.resolve().relative_to(ROOT)),
                "report_sha256": VERIFIER.file_sha256(report_path),
                "status": report["status"],
                "word_error_rate": 0,
                "ordered_word_coverage": 1,
                "asr_text": report["asr"]["text"],
            }
        )
    require(len(revisions) == 1, "ASR model revisions differ")
    assembled = np.concatenate(decoded_segments, axis=0)
    require(assembled.shape == raw_samples.shape, "Concatenated frame/channel shape differs")
    require(np.array_equal(assembled, raw_samples), "Raw decoded samples differ from concatenation")
    decoded_bytes = raw_samples.astype("<f4", copy=False).tobytes()
    result = {
        "schema_version": 1,
        "status": "pass",
        "qualification": "complete independent per-paragraph recognition with exact PCM assembly",
        "verification_method": "full-file ASR + paragraph ASR + exact decoded sample proof",
        "full_file_asr_status": full_report["status"],
        "full_file_report": str(full_report_path.relative_to(ROOT)),
        "full_file_report_sha256": VERIFIER.file_sha256(full_report_path),
        "full_file_asr_disagreement": full_report["content"]["word_error_rate"] != 0,
        "generation_report": str(generation_path.relative_to(ROOT)),
        "generation_report_sha256": VERIFIER.file_sha256(generation_path),
        "audio_path": str(raw_path.resolve()),
        "audio_sha256": VERIFIER.file_sha256(raw_path),
        "expected_transcript": expected,
        "expected_text_sha256": VERIFIER.file_sha256(expected_path),
        "asr": {
            "engine": "independent mlx-whisper per paragraph",
            "text": " ".join(observed),
            "model_revision": next(iter(revisions))[0],
            "model_weights_sha256": next(iter(revisions))[1],
        },
        "content": VERIFIER.compare_transcripts(expected, " ".join(observed)),
        "pcm": full_report["pcm"],
        "segments": evidence,
        "assembly_proof": {
            "comparison": "numpy.array_equal of decoded FLOAT32 samples",
            "equal": True,
            "inserted_frames": 0,
            "sample_rate": raw_rate,
            "channels": raw_samples.shape[1],
            "frame_count": raw_samples.shape[0],
            "decoded_pcm_float32le_sha256": hashlib.sha256(decoded_bytes).hexdigest(),
            "raw_sha256": VERIFIER.file_sha256(raw_path),
        },
        "limits": [
            "Original full-file ASR disagreement is retained, not overwritten.",
            "ASR is fallible; complete paragraph evidence qualifies assembled words only.",
            "Warmth, naturalness, voice identity and join quality require listener judgment.",
        ],
        "failure_reasons": [],
        "review_reasons": [],
        "aggregation_helper_source_sha256": VERIFIER.file_sha256(Path(__file__)),
    }
    destination = HERE / f"{args.candidate}-aggregate.json"
    destination.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": result["status"],
                "report": str(destination),
                "content": {k: v for k, v in result["content"].items() if k != "alignment"},
                "assembly_proof": result["assembly_proof"],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
