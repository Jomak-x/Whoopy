#!/usr/bin/env python3
"""Bind narration ASR reports to exact production PCM/timeline/silence evidence."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
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
    parser.add_argument("report", type=Path)
    parser.add_argument("--expected-file", type=Path, required=True)
    args = parser.parse_args()
    report = json.loads(args.report.read_text())
    audio = Path(report["audio_path"])
    manifest_path, timeline_path = (
        audio.parent / "audio-manifest.json",
        audio.parent / "timeline.json",
    )
    manifest = json.loads(manifest_path.read_text())
    timeline = json.loads(timeline_path.read_text())
    expected = args.expected_file.read_text()
    require(report["mode"] == "narration", "Report was not evaluated in narration mode")
    require(
        report["expected_transcript"] == expected, "Expected spoken text differs from ASR input"
    )
    require(VERIFIER.file_sha256(audio) == report["audio_sha256"], "Audio changed after ASR")
    with wave.open(str(audio), "rb") as source:
        frames, rate = source.getnframes(), source.getframerate()
        channels, width = source.getnchannels(), source.getsampwidth()
        pcm = source.readframes(frames)
    block = channels * width
    require(len(pcm) == frames * block, "WAV payload is truncated")
    require(
        rate == manifest["sample_rate"] and frames == manifest["total_frames"],
        "WAV rate/length differs from production manifest",
    )
    require(
        channels == manifest["channels"] and width == manifest["sample_width_bytes"],
        "WAV channels/width differ from production manifest",
    )
    require(hashlib.sha256(pcm).hexdigest() == manifest["pcm_sha256"], "PCM hash differs")
    spans, events = manifest["segments"], timeline["segments"]
    require(
        [span["segment_id"] for span in spans] == [event["id"] for event in events],
        "Canonical timeline and PCM manifest segment order differ",
    )
    spoken = " ".join(event["text"] for event in events if event["type"] == "SPEECH")
    require(
        VERIFIER.normalize_words(spoken) == VERIFIER.normalize_words(expected),
        "Expected spoken words differ from canonical speech events",
    )
    position, speech_frames, silence_frames = 0, 0, 0
    silence_evidence = []
    for span, event in zip(spans, events, strict=True):
        start, end = span["start_frame"], span["end_frame"]
        require(
            start == position and start < end <= frames, "PCM spans are not exact contiguous ranges"
        )
        require(end - start == span["frame_count"], "PCM span frame count differs")
        require(span["segment_type"] == event["type"], "Canonical/manifest event types differ")
        payload = pcm[start * block : end * block]
        require(
            hashlib.sha256(payload).hexdigest() == span["pcm_sha256"], "Segment PCM hash differs"
        )
        if event["type"] == "SPEECH":
            speech_frames += end - start
        elif event["type"] == "SILENCE":
            require(not any(payload), "Planned silence contains nonzero PCM samples")
            silence_frames += end - start
            silence_evidence.append(
                {
                    "segment_id": span["segment_id"],
                    "start_frame": start,
                    "end_frame": end,
                    "frame_count": end - start,
                    "exact_zero_pcm": True,
                    "pcm_sha256": span["pcm_sha256"],
                }
            )
        else:
            raise ValueError(f"Narration proof does not support event type {event['type']}")
        position = end
    require(position == frames, "PCM remains outside canonical narration spans")
    proof = {
        "audio_manifest": str(manifest_path.relative_to(ROOT)),
        "audio_manifest_sha256": VERIFIER.file_sha256(manifest_path),
        "timeline": str(timeline_path.relative_to(ROOT)),
        "timeline_sha256": VERIFIER.file_sha256(timeline_path),
        "expected_spoken_file": str(args.expected_file.resolve().relative_to(ROOT)),
        "expected_spoken_file_sha256": VERIFIER.file_sha256(args.expected_file),
        "full_pcm_sha256": manifest["pcm_sha256"],
        "all_segment_pcm_hashes_match": True,
        "all_frames_contiguously_covered": True,
        "spoken_words_match_canonical_timeline": True,
        "speech_seconds": speech_frames / rate,
        "intentional_silence_seconds": silence_frames / rate,
        "intentional_silence_segment_count": len(silence_evidence),
        "intentional_silence_spans": silence_evidence,
        "proof_scope": "PCM/timeline intent evidence; ASR content status is independent.",
        "narration_provenance_helper_source_sha256": VERIFIER.file_sha256(Path(__file__)),
    }
    report.setdefault(
        "report_before_narration_provenance_sha256", VERIFIER.file_sha256(args.report)
    )
    report["narration_provenance"] = proof
    args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(args.report),
                "speech_seconds": proof["speech_seconds"],
                "intentional_silence_seconds": proof["intentional_silence_seconds"],
                "intentional_silence_segment_count": len(silence_evidence),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
