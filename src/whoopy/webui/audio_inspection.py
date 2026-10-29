"""Read-only waveform and audition artifacts derived from finished production audio."""

from __future__ import annotations

import array
import hashlib
import math
import re
import sys
import wave
from io import BytesIO
from pathlib import Path
from typing import Any

from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import AudioManifest, AudioQualityReport
from whoopy.audio.processing import processed_speech_metadata
from whoopy.audio.quality import HEADROOM_LIMIT_DBFS, MAX_JOIN_DELTA
from whoopy.audio.synthesis import cache_key_for_identity
from whoopy.meditation import DraftedSection, MeditationPlan
from whoopy.meditation.pacing import pace_section
from whoopy.pipeline.generation import TTSRunSettings, resolved_synthesis_digest
from whoopy.pipeline.runs import RunRecord, RunStatus, RunStore, RunStoreError
from whoopy.ports import AdapterMetadata
from whoopy.timeline import BreathSegment, SpeechSegment, Timeline, build_script_timeline


class AudioInspectionError(RunStoreError):
    """Raised when finished audio cannot be inspected without guessing."""


def current_audio_artifacts(
    store: RunStore, record: RunRecord
) -> tuple[Path, AudioManifest, AudioQualityReport]:
    if record.status is not RunStatus.COMPLETED or record.audio_artifact is None:
        raise AudioInspectionError("The active meditation has no completed narration.")
    try:
        timeline = store.load_timeline(record.run_id)
        manifest = AudioManifest.model_validate_json(
            store.audio_manifest_path(record.run_id).read_text(encoding="utf-8")
        )
        quality = AudioQualityReport.model_validate_json(
            store.quality_path(record.run_id).read_text(encoding="utf-8")
        )
        if record.source_kind == "fixture_prompt":
            identity = FixtureSpeechSynthesizer.cache_identity
        else:
            config = store.load_resolved_config(record.run_id)
            metadata = store.load_model_metadata(record.run_id).tts
            if manifest.synthesis_sha256 is not None:
                if manifest.synthesis_sha256 != resolved_synthesis_digest(config, metadata):
                    raise AudioInspectionError(
                        "Narration differs from resolved synthesis settings."
                    )
            else:
                _validate_resolved_synthesis(metadata, config.tts)
            identity = processed_speech_metadata(
                metadata, inner_cache_identity=metadata.cache_identity, settings=config.processing
            ).cache_identity
        if (
            manifest.schema_version != 2
            or manifest.run_id != record.run_id
            or timeline.run_id != record.run_id
            or manifest.timeline_schema_version != timeline.schema_version
            or len(manifest.segments) != len(timeline.segments)
        ):
            raise AudioInspectionError("Narration has no matching canonical timeline identity.")
        for segment, span in zip(timeline.segments, manifest.segments, strict=True):
            if segment.id != span.segment_id or segment.type != span.segment_type:
                raise AudioInspectionError("Narration differs from the active segment sequence.")
            if isinstance(segment, SpeechSegment):
                expected = cache_key_for_identity(
                    segment, synthesizer_identity=identity, sample_rate=manifest.sample_rate
                )
                if span.cache_key != expected:
                    raise AudioInspectionError("Narration differs from the active speech controls.")
            elif span.requested_duration_ms != segment.duration_ms:
                raise AudioInspectionError("Narration differs from the active pause timing.")
        audio = store.audio_path(record.run_id)
        _validate_audio_evidence(audio, manifest, quality)
        return audio, manifest, quality
    except AudioInspectionError:
        raise
    except (OSError, ValueError, RunStoreError, wave.Error, EOFError) as error:
        raise AudioInspectionError(f"Current narration cannot be verified: {error}") from error


def _validate_audio_evidence(
    audio: Path, manifest: AudioManifest, quality: AudioQualityReport
) -> None:
    required_checks = {
        "mono",
        "sample_width",
        "sample_rate",
        "frame_count",
        "duration",
        "segment_joins",
        "requested_silence_timing",
        "exact_silence",
        "audible_speech",
        "segment_pcm_hashes",
        "whole_pcm_hash",
        "boundary_continuity",
        "peak_headroom",
        "no_clipping",
    }
    if (
        quality.passed != all(check.passed for check in quality.checks)
        or not required_checks.issubset({check.name for check in quality.checks if check.passed})
        or any(not check.passed for check in quality.checks if check.name in required_checks)
        or quality.peak_dbfs is None
        or not math.isfinite(quality.peak_dbfs)
        or quality.peak_dbfs > HEADROOM_LIMIT_DBFS
        or quality.headroom_limit_dbfs != HEADROOM_LIMIT_DBFS
        or quality.clipped_samples != 0
        or quality.max_join_delta is None
        or quality.max_join_delta > MAX_JOIN_DELTA
    ):
        raise AudioInspectionError("Narration quality evidence is inconsistent.")
    with wave.open(str(audio), "rb") as source:
        metadata = {
            "sample_rate": source.getframerate(),
            "channels": source.getnchannels(),
            "sample_width_bytes": source.getsampwidth(),
        }
        if (
            source.getcomptype() != "NONE"
            or source.getnframes() != manifest.total_frames
            or source.getnframes() != quality.frame_count
            or manifest.duration_ms != round(source.getnframes() * 1_000 / manifest.sample_rate, 3)
            or quality.duration_ms != manifest.duration_ms
            or any(
                value != getattr(manifest, name) or value != getattr(quality, name)
                for name, value in metadata.items()
            )
        ):
            raise AudioInspectionError("Narration metadata differs from its assembly evidence.")
        whole_hash = hashlib.sha256()
        expected_start = 0
        for span in manifest.segments:
            if (
                span.start_frame != expected_start
                or span.end_frame - span.start_frame != span.frame_count
                or span.end_frame > manifest.total_frames
                or span.actual_duration_ms
                != round(span.frame_count * 1_000 / manifest.sample_rate, 3)
                or (
                    span.segment_type in {"SILENCE", "BREATH"}
                    and (
                        span.requested_duration_ms is None
                        or span.frame_count
                        != (span.requested_duration_ms * manifest.sample_rate + 500) // 1_000
                    )
                )
            ):
                raise AudioInspectionError("Narration segment timing evidence is inconsistent.")
            segment_hash = hashlib.sha256()
            remaining = span.frame_count
            while remaining:
                frames = min(remaining, 32_768)
                chunk = source.readframes(frames)
                if len(chunk) != frames * manifest.channels * manifest.sample_width_bytes:
                    raise AudioInspectionError("Narration PCM is truncated.")
                whole_hash.update(chunk)
                segment_hash.update(chunk)
                remaining -= frames
            if segment_hash.hexdigest() != span.pcm_sha256:
                raise AudioInspectionError("Narration segment bytes differ from assembly evidence.")
            expected_start = span.end_frame
        if (
            expected_start != manifest.total_frames
            or whole_hash.hexdigest() != manifest.pcm_sha256
            or whole_hash.hexdigest() != quality.pcm_sha256
        ):
            raise AudioInspectionError("Narration bytes do not match their quality evidence.")


def _validate_resolved_synthesis(metadata: AdapterMetadata, settings: TTSRunSettings) -> None:
    if metadata == FixtureSpeechSynthesizer.metadata:
        return
    backend = settings.backend
    expected: dict[str, object] = {"sample_rate": 24_000}
    if backend == "kokoro":
        adapter_id = "whoopy.sherpa_onnx_kokoro"
        expected.update(
            voice=settings.voice_name,
            speaker_id=settings.speaker_id,
            speed=settings.speed,
            threads=settings.num_threads,
            language=settings.language,
        )
        if metadata.device != settings.provider:
            raise AudioInspectionError("Resolved speech provider differs from rendered metadata.")
    elif backend in {"moss-local-v1.5", "moss-v1.5"}:
        adapter_id = f"whoopy.{backend}"
        expected.update(
            language=settings.language,
            instruction=settings.instruction,
            use_reference=settings.use_reference,
            seed=settings.seed,
        )
        if not settings.use_reference:
            expected["reference_sha256"] = "none"
        elif settings.reference_audio_sha256 is not None:
            expected["reference_sha256"] = settings.reference_audio_sha256
    elif backend == "fish-1.4":
        raise AudioInspectionError("Reference narration has no bound resolved synthesis settings.")
    elif backend.startswith("mlx-qwen3-"):
        task = "custom_voice" if settings.voice_mode == "preset" else "voice_design"
        adapter_id = f"whoopy.experimental_mlx_qwen3.{task}"
        expected.pop("sample_rate")
        expected.update(
            voice=settings.voice_name,
            language=settings.language,
            seed=settings.seed,
            installation_sha256=settings.experimental_installation_sha256,
            worker_sha256=settings.experimental_worker_sha256,
            adapter_sha256=settings.experimental_adapter_sha256,
        )
        if (
            metadata.runtime_version != settings.experimental_runtime_revision
            or not metadata.versioned_model_id.endswith(f"@{settings.experimental_model_revision}")
        ):
            raise AudioInspectionError("Resolved experimental revisions differ from narration.")
    else:
        if settings.voice_mode == "reference":
            raise AudioInspectionError(
                "Reference narration has no bound resolved synthesis settings."
            )
        adapter_id = f"whoopy.qwen3_tts.{backend.removeprefix('qwen3-')}"
        expected.update(
            language=settings.language,
            seed=settings.seed,
            speaker=settings.voice_name if settings.voice_mode == "preset" else "none",
            voice_design_profile=(
                settings.qwen_voice_design_preset if settings.voice_mode == "designed" else "none"
            ),
            consent_scope=settings.reference_consent_scope or "none",
        )
        if (
            metadata.runtime_version != settings.qwen_runtime_revision
            or not metadata.versioned_model_id.endswith(f"@{settings.qwen_model_revision}")
        ):
            raise AudioInspectionError("Resolved Qwen revisions differ from narration.")
    rendered = dict(value.split("=", 1) for value in metadata.settings)
    if metadata.adapter_id != adapter_id or any(
        rendered.get(name) != str(value) for name, value in expected.items()
    ):
        raise AudioInspectionError("Resolved speech settings differ from rendered metadata.")


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
            script = (
                "\n\n".join(
                    f"{paragraph}\n\n[pause: 1000ms]"
                    for paragraph in re.split(r"\n\s*\n", draft.text.strip())
                )
                if plan.recipe_sha256 is not None
                else pace_section(draft.text, section_pause_ms=planned.pause_after_ms)
            )
            expected = build_script_timeline(
                run_id=timeline.run_id,
                script=script,
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
        if [segment.type for segment in actual] != [
            segment.type for segment in expected.segments
        ] or actual_speech != expected_speech:
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
