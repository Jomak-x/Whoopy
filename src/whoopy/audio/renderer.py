"""Assemble a canonical timeline into one deterministic PCM WAV file."""

from __future__ import annotations

import hashlib
import wave
from collections.abc import Mapping
from io import BytesIO

from whoopy.audio.fixture import FixtureSpeechSynthesizer, silence
from whoopy.audio.models import (
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    AudioManifest,
    PcmAudio,
    RenderedWave,
    SegmentAudioSpan,
)
from whoopy.audio.quality import inspect_wave
from whoopy.audio.synthesis import SpeechSynthesizer, cache_key_for
from whoopy.timeline import SilenceSegment, Timeline


class AudioRenderError(RuntimeError):
    """Raised when timeline audio cannot pass the deterministic quality gate."""


class TimelineWaveRenderer:
    """Render prepared speech and exact silence in canonical timeline order."""

    def __init__(self, synthesizer: SpeechSynthesizer | None = None) -> None:
        self.synthesizer: SpeechSynthesizer = (
            synthesizer if synthesizer is not None else FixtureSpeechSynthesizer()
        )

    def render(
        self,
        timeline: Timeline,
        *,
        speech_audio: Mapping[str, PcmAudio] | None = None,
        speech_cache_keys: Mapping[str, str] | None = None,
    ) -> RenderedWave:
        """Assemble cached/checkpointed speech when supplied, otherwise synthesize."""

        chunks: list[bytes] = []
        spans: list[SegmentAudioSpan] = []
        cursor = 0

        for segment in timeline.segments:
            if isinstance(segment, SilenceSegment):
                audio = silence(segment.duration_ms)
                requested_duration_ms: int | None = segment.duration_ms
                segment_cache_key = None
            else:
                if speech_audio is None:
                    audio = self.synthesizer.synthesize(segment)
                else:
                    try:
                        audio = speech_audio[segment.id]
                    except KeyError as error:
                        raise AudioRenderError(
                            f"No prepared speech audio exists for segment {segment.id}"
                        ) from error
                requested_duration_ms = None
                segment_cache_key = (
                    speech_cache_keys.get(segment.id)
                    if speech_cache_keys is not None
                    else cache_key_for(segment, self.synthesizer)
                )
                if segment_cache_key is None:
                    raise AudioRenderError(f"No cache key exists for segment {segment.id}")

            if audio.sample_rate != SAMPLE_RATE:
                raise AudioRenderError(
                    f"Segment {segment.id} uses {audio.sample_rate} Hz; expected {SAMPLE_RATE} Hz"
                )
            end_frame = cursor + audio.frame_count
            spans.append(
                SegmentAudioSpan(
                    segment_id=segment.id,
                    segment_type=segment.type,
                    start_frame=cursor,
                    end_frame=end_frame,
                    frame_count=audio.frame_count,
                    requested_duration_ms=requested_duration_ms,
                    actual_duration_ms=round(audio.frame_count * 1_000 / SAMPLE_RATE, 3),
                    cache_key=segment_cache_key,
                    pcm_sha256=hashlib.sha256(audio.pcm_s16le).hexdigest(),
                )
            )
            chunks.append(audio.pcm_s16le)
            cursor = end_frame

        pcm_bytes = b"".join(chunks)
        output = BytesIO()
        with wave.open(output, "wb") as wave_file:
            wave_file.setnchannels(CHANNELS)
            wave_file.setsampwidth(SAMPLE_WIDTH_BYTES)
            wave_file.setframerate(SAMPLE_RATE)
            wave_file.writeframes(pcm_bytes)
        wave_bytes = output.getvalue()

        manifest = AudioManifest(
            run_id=timeline.run_id,
            timeline_schema_version=timeline.schema_version,
            total_frames=cursor,
            duration_ms=round(cursor * 1_000 / SAMPLE_RATE, 3),
            pcm_sha256=hashlib.sha256(pcm_bytes).hexdigest(),
            segments=spans,
        )
        quality = inspect_wave(wave_bytes, manifest)
        if not quality.passed:
            failed_checks = ", ".join(check.name for check in quality.checks if not check.passed)
            raise AudioRenderError(f"Rendered WAV failed quality checks: {failed_checks}")
        return RenderedWave(wave_bytes=wave_bytes, manifest=manifest, quality=quality)
