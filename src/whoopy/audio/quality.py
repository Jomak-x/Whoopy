"""Inspect a rendered WAV and enforce Phase 2's basic audio quality gate."""

from __future__ import annotations

import math
import wave
from array import array
from io import BytesIO
from sys import byteorder

from whoopy.audio.models import (
    SAMPLE_WIDTH_BYTES,
    AudioManifest,
    AudioQualityReport,
    QualityCheck,
)


def _check(name: str, passed: bool, detail: str) -> QualityCheck:
    return QualityCheck(name=name, passed=passed, detail=detail)


def inspect_wave(wave_bytes: bytes, manifest: AudioManifest) -> AudioQualityReport:
    """Read the actual container and compare it with the render manifest."""

    with wave.open(BytesIO(wave_bytes), "rb") as wave_file:
        channels = wave_file.getnchannels()
        sample_width = wave_file.getsampwidth()
        sample_rate = wave_file.getframerate()
        frame_count = wave_file.getnframes()
        pcm_bytes = wave_file.readframes(frame_count)

    samples = array("h")
    samples.frombytes(pcm_bytes)
    if byteorder != "little":
        samples.byteswap()

    peak = max((abs(sample) for sample in samples), default=0)
    peak_dbfs = None if peak == 0 else round(20 * math.log10(peak / 32_767), 2)
    clipped_samples = sum(abs(sample) >= 32_767 for sample in samples)
    duration_ms = round(frame_count * 1_000 / sample_rate, 3)

    spans_contiguous = bool(manifest.segments)
    expected_start = 0
    for span in manifest.segments:
        spans_contiguous = spans_contiguous and span.start_frame == expected_start
        span_length_matches = span.end_frame - span.start_frame == span.frame_count
        spans_contiguous = spans_contiguous and span_length_matches
        expected_start = span.end_frame
    spans_contiguous = spans_contiguous and expected_start == frame_count

    silence_exact = True
    speech_audible = True
    for span in manifest.segments:
        segment_samples = samples[span.start_frame : span.end_frame]
        if span.segment_type == "SILENCE":
            silence_exact = silence_exact and all(sample == 0 for sample in segment_samples)
        else:
            speech_audible = speech_audible and any(sample != 0 for sample in segment_samples)

    checks = [
        _check("mono", channels == manifest.channels, f"channels={channels}"),
        _check(
            "sample_width",
            sample_width == SAMPLE_WIDTH_BYTES == manifest.sample_width_bytes,
            f"sample_width_bytes={sample_width}",
        ),
        _check(
            "sample_rate",
            sample_rate == manifest.sample_rate,
            f"sample_rate={sample_rate}",
        ),
        _check(
            "frame_count",
            frame_count == manifest.total_frames,
            f"frames={frame_count}, expected={manifest.total_frames}",
        ),
        _check(
            "duration",
            duration_ms == manifest.duration_ms,
            f"duration_ms={duration_ms}, expected={manifest.duration_ms}",
        ),
        _check("segment_joins", spans_contiguous, "segment frame ranges are contiguous"),
        _check("exact_silence", silence_exact, "all SILENCE samples are zero"),
        _check("audible_speech_fixture", speech_audible, "every SPEECH span contains a tone"),
        _check("no_clipping", clipped_samples == 0, f"clipped_samples={clipped_samples}"),
    ]
    return AudioQualityReport(
        passed=all(check.passed for check in checks),
        sample_rate=sample_rate,
        channels=channels,
        sample_width_bytes=sample_width,
        frame_count=frame_count,
        duration_ms=duration_ms,
        peak_dbfs=peak_dbfs,
        clipped_samples=clipped_samples,
        checks=checks,
    )
