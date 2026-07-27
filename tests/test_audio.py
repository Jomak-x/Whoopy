from __future__ import annotations

import wave
from datetime import UTC, datetime
from io import BytesIO
from uuid import UUID

from whoopy.audio import AudioManifest, TimelineWaveRenderer
from whoopy.audio.fixture import frames_for_milliseconds
from whoopy.audio.quality import inspect_wave
from whoopy.timeline import SilenceSegment, SpeechSegment, Timeline

RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
CREATED_AT = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _timeline() -> Timeline:
    return Timeline(
        schema_version=2,
        run_id=RUN_ID,
        created_at=CREATED_AT,
        source="phase_2_fixture_meditation",
        segments=[
            SpeechSegment(id="speech-0001", text="Breathe in."),
            SilenceSegment(id="silence-0001", duration_ms=1_234),
            SpeechSegment(id="speech-0002", text="Breathe out."),
        ],
    )


def test_renderer_is_reproducible_and_preserves_exact_silence() -> None:
    renderer = TimelineWaveRenderer()

    first = renderer.render(_timeline())
    second = renderer.render(_timeline())

    assert first.wave_bytes == second.wave_bytes
    assert first.manifest == second.manifest
    assert first.quality.passed is True
    assert first.quality.clipped_samples == 0
    assert first.quality.peak_dbfs is not None
    assert first.quality.peak_dbfs < -3

    silence_span = first.manifest.segments[1]
    assert silence_span.segment_type == "SILENCE"
    assert silence_span.frame_count == frames_for_milliseconds(1_234)
    assert silence_span.actual_duration_ms == 1_234

    with wave.open(BytesIO(first.wave_bytes), "rb") as wave_file:
        wave_file.setpos(silence_span.start_frame)
        silence_bytes = wave_file.readframes(silence_span.frame_count)
    assert silence_bytes == b"\x00\x00" * silence_span.frame_count


def test_phase_two_audio_manifest_remains_readable() -> None:
    phase_two = TimelineWaveRenderer().render(_timeline()).manifest.model_dump()
    phase_two["schema_version"] = 1
    phase_two.pop("pcm_sha256")
    for span in phase_two["segments"]:
        span.pop("cache_key")
        span.pop("pcm_sha256")

    manifest = AudioManifest.model_validate(phase_two)

    assert manifest.schema_version == 1
    assert manifest.pcm_sha256 is None


def test_quality_gate_detects_nonzero_samples_inside_silence() -> None:
    rendered = TimelineWaveRenderer().render(_timeline())
    silence_span = rendered.manifest.segments[1]

    with wave.open(BytesIO(rendered.wave_bytes), "rb") as source:
        parameters = source.getparams()
        frames = bytearray(source.readframes(source.getnframes()))
    byte_offset = silence_span.start_frame * parameters.sampwidth
    frames[byte_offset : byte_offset + 2] = b"\x01\x00"

    output = BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setparams(parameters)
        destination.writeframes(frames)

    report = inspect_wave(output.getvalue(), rendered.manifest)

    assert report.passed is False
    exact_silence = next(check for check in report.checks if check.name == "exact_silence")
    assert exact_silence.passed is False


def test_quality_gate_detects_requested_pause_timing_regression() -> None:
    rendered = TimelineWaveRenderer().render(_timeline())
    silence_span = rendered.manifest.segments[1].model_copy(update={"requested_duration_ms": 1_235})
    manifest = rendered.manifest.model_copy(
        update={
            "segments": [
                rendered.manifest.segments[0],
                silence_span,
                rendered.manifest.segments[2],
            ]
        }
    )

    report = inspect_wave(rendered.wave_bytes, manifest)

    timing = next(check for check in report.checks if check.name == "requested_silence_timing")
    assert timing.passed is False


def test_quality_gate_detects_peak_headroom_regression_before_full_clipping() -> None:
    rendered = TimelineWaveRenderer().render(_timeline())
    speech_span = rendered.manifest.segments[0]

    with wave.open(BytesIO(rendered.wave_bytes), "rb") as source:
        parameters = source.getparams()
        frames = bytearray(source.readframes(source.getnframes()))
    byte_offset = (speech_span.start_frame + 100) * parameters.sampwidth
    frames[byte_offset : byte_offset + 2] = (32_000).to_bytes(2, "little", signed=True)

    output = BytesIO()
    with wave.open(output, "wb") as destination:
        destination.setparams(parameters)
        destination.writeframes(frames)

    report = inspect_wave(output.getvalue(), rendered.manifest)

    headroom = next(check for check in report.checks if check.name == "peak_headroom")
    no_clipping = next(check for check in report.checks if check.name == "no_clipping")
    assert report.passed is False
    assert headroom.passed is False
    assert no_clipping.passed is True
