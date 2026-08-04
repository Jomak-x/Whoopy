from __future__ import annotations

import math
from array import array
from sys import byteorder

import pytest

from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import ProcessedSpeechSynthesizer, SpeechProcessingSettings
from whoopy.audio.synthesis import InvalidSynthesisOutput
from whoopy.ports import AdapterMetadata
from whoopy.timeline import SpeechSegment


def _pcm(values: list[int]) -> bytes:
    samples = array("h", values)
    if byteorder != "little":
        samples.byteswap()
    return samples.tobytes()


class _StaticSynthesizer:
    metadata = AdapterMetadata(
        adapter_id="tests.static_speech",
        versioned_model_id="tests/static@1",
        runtime_id="python",
        runtime_version="test",
        license_id="test",
        device="cpu",
    )
    cache_identity = metadata.cache_identity
    sample_rate = 24_000

    def __init__(self, values: list[int]) -> None:
        self.values = values
        self.closed = False

    def synthesize(self, _segment: SpeechSegment) -> PcmAudio:
        return PcmAudio(_pcm(self.values), sample_rate=self.sample_rate)

    def close(self) -> None:
        """Test double owns no external resources."""

        self.closed = True


def test_processing_trims_edges_normalizes_and_fades_boundaries() -> None:
    raw_values = [0] * 1_000 + [4_000] * 1_000 + [0] * 1_000
    synthesizer = ProcessedSpeechSynthesizer(_StaticSynthesizer(raw_values))

    audio = synthesizer.synthesize(SpeechSegment(id="speech-1", text="Welcome."))
    samples = array("h")
    samples.frombytes(audio.pcm_s16le)
    if byteorder != "little":
        samples.byteswap()

    assert audio.frame_count == 3_000
    expected_peak = round(4_000 * 10 ** (3 / 20))
    assert max(abs(sample) for sample in samples) == expected_peak
    assert abs(samples[0]) <= 1_000
    assert abs(samples[-1]) <= 1_000


def test_processing_settings_change_cache_identity() -> None:
    first = ProcessedSpeechSynthesizer(FixtureSpeechSynthesizer())
    second = ProcessedSpeechSynthesizer(
        FixtureSpeechSynthesizer(),
        SpeechProcessingSettings(target_peak_dbfs=-9),
    )

    assert first.cache_identity != second.cache_identity


def test_processing_close_releases_the_inner_synthesizer() -> None:
    inner = _StaticSynthesizer([4_000] * 100)
    synthesizer = ProcessedSpeechSynthesizer(inner)

    synthesizer.close()

    assert inner.closed is True


def test_processing_rejects_audio_below_trim_threshold() -> None:
    synthesizer = ProcessedSpeechSynthesizer(_StaticSynthesizer([1] * 2_000))

    with pytest.raises(InvalidSynthesisOutput, match="trim threshold"):
        synthesizer.synthesize(SpeechSegment(id="speech-1", text="Welcome."))


def test_processing_settings_reject_an_unsafe_peak_target() -> None:
    with pytest.raises(ValueError, match="target peak"):
        SpeechProcessingSettings(target_peak_dbfs=math.inf)
