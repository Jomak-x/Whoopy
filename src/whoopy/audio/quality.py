"""Inspect segment PCM and assembled WAV output for deterministic integrity."""

from __future__ import annotations

import hashlib
import math
import wave
from array import array
from io import BytesIO
from sys import byteorder

from whoopy.audio.models import (
    SAMPLE_RATE,
    SAMPLE_WIDTH_BYTES,
    AudioManifest,
    AudioQualityReport,
    PcmAudio,
    QualityCheck,
)

HEADROOM_LIMIT_DBFS = -1.0
MAX_JOIN_DELTA = 1_000


def _check(name: str, passed: bool, detail: str) -> QualityCheck:
    return QualityCheck(name=name, passed=passed, detail=detail)


def _samples_from_pcm(pcm_bytes: bytes) -> array[int]:
    samples = array("h")
    samples.frombytes(pcm_bytes)
    if byteorder != "little":
        samples.byteswap()
    return samples


def _peak_dbfs(samples: array[int]) -> float | None:
    peak = max((abs(sample) for sample in samples), default=0)
    return None if peak == 0 else round(20 * math.log10(peak / 32_767), 2)


def pcm_integrity_error(audio: PcmAudio) -> str | None:
    """Return why synthesized speech PCM is unsafe to cache, or ``None``."""

    if audio.sample_rate != SAMPLE_RATE:
        return f"sample_rate={audio.sample_rate}, expected={SAMPLE_RATE}"
    if audio.frame_count == 0:
        return "audio contains no frames"
    samples = _samples_from_pcm(audio.pcm_s16le)
    peak_dbfs = _peak_dbfs(samples)
    if peak_dbfs is None:
        return "speech audio is completely silent"
    if peak_dbfs > HEADROOM_LIMIT_DBFS:
        return f"peak_dbfs={peak_dbfs}, limit={HEADROOM_LIMIT_DBFS}"
    return None


def inspect_wave(wave_bytes: bytes, manifest: AudioManifest) -> AudioQualityReport:
    """Read the actual container and compare it with the render manifest."""

    with wave.open(BytesIO(wave_bytes), "rb") as wave_file:
        channels = wave_file.getnchannels()
        sample_width = wave_file.getsampwidth()
        sample_rate = wave_file.getframerate()
        frame_count = wave_file.getnframes()
        pcm_bytes = wave_file.readframes(frame_count)

    samples = _samples_from_pcm(pcm_bytes)
    peak_dbfs = _peak_dbfs(samples)
    clipped_samples = sum(abs(sample) >= 32_767 for sample in samples)
    duration_ms = round(frame_count * 1_000 / sample_rate, 3)
    pcm_sha256 = hashlib.sha256(pcm_bytes).hexdigest()

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
    requested_silence_exact = True
    segment_hashes_match = True
    for span in manifest.segments:
        segment_samples = samples[span.start_frame : span.end_frame]
        start_byte = span.start_frame * SAMPLE_WIDTH_BYTES
        end_byte = span.end_frame * SAMPLE_WIDTH_BYTES
        actual_segment_hash = hashlib.sha256(pcm_bytes[start_byte:end_byte]).hexdigest()
        if span.pcm_sha256 is not None:
            segment_hashes_match = segment_hashes_match and actual_segment_hash == span.pcm_sha256
        if span.segment_type == "SILENCE":
            silence_exact = silence_exact and all(sample == 0 for sample in segment_samples)
            if span.requested_duration_ms is not None:
                expected_frames = (span.requested_duration_ms * sample_rate + 500) // 1_000
                requested_silence_exact = (
                    requested_silence_exact and span.frame_count == expected_frames
                )
        else:
            speech_audible = speech_audible and any(sample != 0 for sample in segment_samples)

    join_deltas = [
        abs(samples[span.start_frame] - samples[span.start_frame - 1])
        for span in manifest.segments[1:]
        if 0 < span.start_frame < len(samples)
    ]
    max_join_delta = max(join_deltas, default=0)
    whole_hash_matches = manifest.pcm_sha256 is None or manifest.pcm_sha256 == pcm_sha256
    headroom_ok = peak_dbfs is not None and peak_dbfs <= HEADROOM_LIMIT_DBFS

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
        _check(
            "requested_silence_timing",
            requested_silence_exact,
            "every requested SILENCE duration maps to its exact frame count",
        ),
        _check("exact_silence", silence_exact, "all SILENCE samples are zero"),
        _check("audible_speech_fixture", speech_audible, "every SPEECH span contains a tone"),
        _check(
            "segment_pcm_hashes",
            segment_hashes_match,
            "every segment PCM payload matches its manifest digest",
        ),
        _check(
            "whole_pcm_hash",
            whole_hash_matches,
            f"pcm_sha256={pcm_sha256}",
        ),
        _check(
            "boundary_continuity",
            max_join_delta <= MAX_JOIN_DELTA,
            f"max_join_delta={max_join_delta}, limit={MAX_JOIN_DELTA}",
        ),
        _check(
            "peak_headroom",
            headroom_ok,
            f"peak_dbfs={peak_dbfs}, limit={HEADROOM_LIMIT_DBFS}",
        ),
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
        pcm_sha256=pcm_sha256,
        max_join_delta=max_join_delta,
        headroom_limit_dbfs=HEADROOM_LIMIT_DBFS,
        checks=checks,
    )
