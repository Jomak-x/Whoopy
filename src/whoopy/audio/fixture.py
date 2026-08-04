"""Dependency-free fixture speech used to test timing before real TTS."""

from __future__ import annotations

from array import array
from sys import byteorder

from whoopy.audio.models import SAMPLE_RATE, PcmAudio
from whoopy.ports import AdapterMetadata
from whoopy.timeline import SpeechSegment

FIXTURE_AMPLITUDE = 5_000
MIN_SPEECH_MS = 600
MAX_SPEECH_MS = 4_000
MILLISECONDS_PER_WORD = 220


def frames_for_milliseconds(duration_ms: int, sample_rate: int = SAMPLE_RATE) -> int:
    """Convert time to the nearest whole sample using integer arithmetic."""

    return (duration_ms * sample_rate + 500) // 1_000


def silence(duration_ms: int, sample_rate: int = SAMPLE_RATE) -> PcmAudio:
    """Return exact zero-valued PCM for a deliberate timeline pause."""

    frame_count = frames_for_milliseconds(duration_ms, sample_rate)
    return PcmAudio(pcm_s16le=b"\x00\x00" * frame_count, sample_rate=sample_rate)


class FixtureSpeechSynthesizer:
    """Represent speech as a deterministic, softly faded triangle tone.

    The tone is intentionally not human speech. Its only job is to make segment
    boundaries audible and testable without adding a model dependency.
    """

    metadata = AdapterMetadata(
        adapter_id="whoopy.fixture_triangle",
        versioned_model_id="whoopy/fixture-triangle@1",
        runtime_id="python",
        runtime_version="3.11",
        license_id="Whoopy-test-fixture",
        device="cpu",
        settings=("amplitude=5000", "milliseconds_per_word=220"),
    )
    # Metadata is the single cache identity source for fixture and real models.
    cache_identity: str = metadata.cache_identity
    sample_rate: int = SAMPLE_RATE

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        words = max(1, len(segment.text.split()))
        duration_ms = min(
            MAX_SPEECH_MS,
            max(MIN_SPEECH_MS, 300 + words * MILLISECONDS_PER_WORD),
        )
        frame_count = frames_for_milliseconds(duration_ms, self.sample_rate)
        frequency_hz = 220 + sum(segment.text.encode("utf-8")) % 180
        fade_frames = min(frame_count // 2, self.sample_rate // 100)
        samples = array("h")

        for frame_index in range(frame_count):
            phase = (frame_index * frequency_hz) % self.sample_rate
            triangle = (
                2 * abs(2 * phase - self.sample_rate) * FIXTURE_AMPLITUDE
            ) // self.sample_rate - FIXTURE_AMPLITUDE
            edge_distance = min(frame_index + 1, frame_count - frame_index)
            gain_numerator = min(edge_distance, fade_frames)
            sample = triangle * gain_numerator // fade_frames
            samples.append(sample)

        if byteorder != "little":
            samples.byteswap()
        return PcmAudio(pcm_s16le=samples.tobytes(), sample_rate=self.sample_rate)

    def close(self) -> None:
        """The dependency-free fixture owns no resources."""
