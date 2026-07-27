"""Assemble a canonical timeline into one deterministic PCM WAV file."""

from __future__ import annotations

import wave
from io import BytesIO

from whoopy.audio.fixture import FixtureSpeechSynthesizer, silence
from whoopy.audio.models import (
    CHANNELS,
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    AudioManifest,
    RenderedWave,
    SegmentAudioSpan,
)
from whoopy.audio.quality import inspect_wave
from whoopy.timeline import SilenceSegment, Timeline


class AudioRenderError(RuntimeError):
    """Raised when timeline audio cannot pass the deterministic quality gate."""


class TimelineWaveRenderer:
    """Render speech fixtures and exact silence in canonical timeline order."""

    def __init__(self, synthesizer: FixtureSpeechSynthesizer | None = None) -> None:
        self.synthesizer = synthesizer or FixtureSpeechSynthesizer()

    def render(self, timeline: Timeline) -> RenderedWave:
        chunks: list[bytes] = []
        spans: list[SegmentAudioSpan] = []
        cursor = 0

        for segment in timeline.segments:
            if isinstance(segment, SilenceSegment):
                audio = silence(segment.duration_ms)
                requested_duration_ms: int | None = segment.duration_ms
            else:
                audio = self.synthesizer.synthesize(segment)
                requested_duration_ms = None

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
            segments=spans,
        )
        quality = inspect_wave(wave_bytes, manifest)
        if not quality.passed:
            failed_checks = ", ".join(check.name for check in quality.checks if not check.passed)
            raise AudioRenderError(f"Rendered WAV failed quality checks: {failed_checks}")
        return RenderedWave(wave_bytes=wave_bytes, manifest=manifest, quality=quality)
