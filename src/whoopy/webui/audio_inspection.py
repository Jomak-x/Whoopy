"""Read-only waveform and audition artifacts derived from finished production audio."""

from __future__ import annotations

import array
import hashlib
import sys
import wave
from io import BytesIO
from pathlib import Path
from typing import Any

from whoopy.audio.models import AudioManifest
from whoopy.meditation import DraftedSection, MeditationPlan
from whoopy.meditation.pacing import pace_section
from whoopy.pipeline.runs import RunStoreError
from whoopy.timeline import BreathSegment, SpeechSegment, Timeline, build_script_timeline


class AudioInspectionError(RunStoreError):
    """Raised when finished audio cannot be inspected without guessing."""


def waveform_payload(
    audio_path: Path,
    manifest_path: Path,
    *,
    points: int,
) -> dict[str, Any]:
    """Return normalized min/max PCM envelopes plus exact timeline boundaries."""

    manifest, pcm = _load_verified_audio(audio_path, manifest_path)
    frame_count = len(pcm)
    bucket_size = max(1, (frame_count + points - 1) // points)
    peaks: list[list[float]] = []
    for start in range(0, frame_count, bucket_size):
        bucket = pcm[start : start + bucket_size]
        peaks.append(
            [
                round(min(bucket) / 32768, 5),
                round(max(bucket) / 32767, 5),
            ]
        )
    return {
        "sample_rate": manifest.sample_rate,
        "total_frames": manifest.total_frames,
        "duration_ms": manifest.duration_ms,
        "peaks": peaks,
        "segments": [
            {
                "segment_id": span.segment_id,
                "segment_type": span.segment_type,
                "start_ratio": span.start_frame / manifest.total_frames,
                "end_ratio": span.end_frame / manifest.total_frames,
                "actual_duration_ms": span.actual_duration_ms,
            }
            for span in manifest.segments
        ],
    }


def enrich_waveform_context(
    payload: dict[str, Any],
    *,
    timeline: Timeline,
    sections: list[dict[str, Any]],
    regenerations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Attach canonical breath, section, and take context to a PCM waveform.

    The PCM manifest remains authoritative for positions. Timeline and take
    evidence only decorate matching segment IDs; mismatches fail instead of
    drawing a plausible but false editor visualization.
    """

    raw_segments = payload.get("segments")
    total_frames = payload.get("total_frames")
    if not isinstance(raw_segments, list) or not isinstance(total_frames, int) or total_frames <= 0:
        raise AudioInspectionError("Waveform payload is missing exact segment frame evidence.")
    timeline_by_id = {segment.id: segment for segment in timeline.segments}
    waveform_ids = [item.get("segment_id") for item in raw_segments if isinstance(item, dict)]
    if waveform_ids != [segment.id for segment in timeline.segments]:
        raise AudioInspectionError("Waveform and canonical timeline segment order do not match.")

    enriched_segments: list[dict[str, Any]] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise AudioInspectionError("Waveform contains an invalid segment descriptor.")
        segment_id = str(raw["segment_id"])
        timeline_segment = timeline_by_id[segment_id]
        enriched = dict(raw)
        if isinstance(timeline_segment, BreathSegment):
            enriched["breath"] = {
                "phase": timeline_segment.phase,
                "cycle": timeline_segment.cycle,
                "protocol_id": timeline_segment.protocol_id,
            }
        if isinstance(timeline_segment, SpeechSegment):
            enriched["regeneration"] = regenerations.get(
                segment_id,
                {
                    "status": "original",
                    "take_count": 0,
                    "selected_take_id": None,
                },
            )
        enriched_segments.append(enriched)

    enriched_sections: list[dict[str, Any]] = []
    previous_end = 0
    for section in sections:
        try:
            start_frame = int(section["start_frame"])
            end_frame = int(section["end_frame"])
        except (KeyError, TypeError, ValueError) as error:
            raise AudioInspectionError("Section boundary evidence is invalid.") from error
        if start_frame != previous_end or end_frame <= start_frame or end_frame > total_frames:
            raise AudioInspectionError(
                "Section boundaries are not contiguous finished-audio spans."
            )
        enriched_sections.append(
            {
                **section,
                "start_ratio": start_frame / total_frames,
                "end_ratio": end_frame / total_frames,
            }
        )
        previous_end = end_frame
    if enriched_sections and previous_end != total_frames:
        raise AudioInspectionError("Section boundaries do not cover the finished audio.")

    return {**payload, "segments": enriched_segments, "sections": enriched_sections}


def segment_wave_bytes(audio_path: Path, manifest_path: Path, segment_id: str) -> bytes:
    """Crop one exact assembled segment, including normalized production processing."""

    manifest, pcm = _load_verified_audio(audio_path, manifest_path)
    span = next((item for item in manifest.segments if item.segment_id == segment_id), None)
    if span is None:
        raise AudioInspectionError(f"Segment {segment_id!r} is not in the audio manifest.")
    return _wave_bytes(pcm[span.start_frame : span.end_frame], manifest.sample_rate)


def section_descriptors(
    *,
    timeline_path: Path,
    manifest_path: Path,
    plan_path: Path,
    draft_sections_path: Path,
) -> list[dict[str, Any]]:
    """Map reviewed plan sections to contiguous rendered timeline ranges.

    Generated scripts place the plan's section-ending pause after every section.
    Matching those deterministic pauses from the end of each remaining section
    keeps the mapping valid after real-speech duration fitting, which changes
    pause values proportionally but never changes segment order.
    """

    try:
        timeline = Timeline.model_validate_json(timeline_path.read_text(encoding="utf-8"))
        manifest = AudioManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        plan = MeditationPlan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AudioInspectionError(f"Section audition metadata is invalid: {error}") from error
    spans = {span.segment_id: span for span in manifest.segments}
    if [segment.id for segment in timeline.segments] != [
        span.segment_id for span in manifest.segments
    ]:
        raise AudioInspectionError("Timeline and audio manifest segment order do not match.")

    values: list[dict[str, Any]] = []
    start_index = 0
    for planned in plan.sections:
        try:
            draft = DraftedSection.model_validate_json(
                (draft_sections_path / f"{planned.id}.json").read_text(encoding="utf-8")
            )
            expected = build_script_timeline(
                run_id=timeline.run_id,
                script=pace_section(draft.text, section_pause_ms=planned.pause_after_ms),
                created_at=timeline.created_at,
                source="generated_prompt",
            )
        except (OSError, ValueError) as error:
            raise AudioInspectionError(
                f"Section {planned.id!r} cannot be reconstructed safely: {error}"
            ) from error
        expected_speech = [
            segment.text for segment in expected.segments if isinstance(segment, SpeechSegment)
        ]
        end_index = start_index + len(expected.segments)
        actual = timeline.segments[start_index:end_index]
        actual_speech = [segment.text for segment in actual if isinstance(segment, SpeechSegment)]
        if len(actual) != len(expected.segments) or actual_speech != expected_speech:
            raise AudioInspectionError(
                "The active script no longer aligns with its reviewed plan; "
                "section audition is unavailable until the plan is regenerated."
            )
        member_ids = [segment.id for segment in actual]
        if not member_ids:
            raise AudioInspectionError(f"Section {planned.id!r} has no rendered timeline events.")
        first = spans[member_ids[0]]
        last = spans[member_ids[-1]]
        values.append(
            {
                "section_id": planned.id,
                "title": planned.title,
                "beat": planned.beat,
                "technique": planned.technique,
                "segment_ids": member_ids,
                "start_frame": first.start_frame,
                "end_frame": last.end_frame,
                "duration_ms": round(
                    (last.end_frame - first.start_frame) * 1_000 / manifest.sample_rate,
                    3,
                ),
            }
        )
        start_index = end_index
    if start_index != len(timeline.segments):
        raise AudioInspectionError("Rendered events remain outside reviewed plan sections.")
    return values


def section_wave_bytes(
    audio_path: Path,
    manifest_path: Path,
    descriptor: dict[str, Any],
) -> bytes:
    """Crop one already-rendered reviewed section without invoking a model."""

    manifest, pcm = _load_verified_audio(audio_path, manifest_path)
    start = int(descriptor["start_frame"])
    end = int(descriptor["end_frame"])
    if start < 0 or end <= start or end > manifest.total_frames:
        raise AudioInspectionError("Section frame range is outside finished audio.")
    return _wave_bytes(pcm[start:end], manifest.sample_rate)


def _load_verified_audio(
    audio_path: Path,
    manifest_path: Path,
) -> tuple[AudioManifest, array.array[int]]:
    try:
        manifest = AudioManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        with wave.open(str(audio_path), "rb") as source:
            if source.getnchannels() != 1 or source.getsampwidth() != 2:
                raise AudioInspectionError("Finished audio must be mono PCM16 WAV.")
            if source.getframerate() != manifest.sample_rate:
                raise AudioInspectionError("Finished audio sample rate differs from its manifest.")
            if source.getnframes() != manifest.total_frames:
                raise AudioInspectionError("Finished audio frame count differs from its manifest.")
            raw = source.readframes(source.getnframes())
            if manifest.pcm_sha256 is not None and hashlib.sha256(raw).hexdigest() != (
                manifest.pcm_sha256
            ):
                raise AudioInspectionError("Finished audio PCM digest differs from its manifest.")
    except AudioInspectionError:
        raise
    except (OSError, ValueError, wave.Error) as error:
        raise AudioInspectionError(f"Finished audio cannot be read: {error}") from error
    pcm = array.array("h")
    pcm.frombytes(raw)
    if sys.byteorder != "little":
        pcm.byteswap()
    if not pcm:
        raise AudioInspectionError("Finished audio contains no PCM frames.")
    return manifest, pcm


def _wave_bytes(pcm: array.array[int], sample_rate: int) -> bytes:
    little_endian = array.array("h", pcm)
    if sys.byteorder != "little":
        little_endian.byteswap()
    output = BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setnchannels(1)
        destination.setsampwidth(2)
        destination.setframerate(sample_rate)
        destination.writeframes(little_endian.tobytes())
    return output.getvalue()
