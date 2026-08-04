"""Deterministic speech cleanup applied before cache/checkpoint persistence."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from sys import byteorder

from whoopy.audio.models import PcmAudio
from whoopy.audio.synthesis import InvalidSynthesisOutput, SpeechSynthesizer
from whoopy.ports import AdapterMetadata
from whoopy.timeline import SpeechSegment


@dataclass(frozen=True)
class SpeechProcessingSettings:
    """Every post-processing value that changes segment PCM."""

    trim_threshold_dbfs: float = -45.0
    edge_residual_ms: int = 60
    edge_fade_ms: int = 40
    target_peak_dbfs: float = -6.0
    max_gain_db: float = 3.0

    def __post_init__(self) -> None:
        if not -90 <= self.trim_threshold_dbfs <= -20:
            raise ValueError("trim threshold must be between -90 and -20 dBFS")
        if not 0 <= self.edge_residual_ms <= 100:
            raise ValueError("edge residual must be between 0 and 100 ms")
        if not 0 <= self.edge_fade_ms <= 100:
            raise ValueError("edge fade must be between 0 and 100 ms")
        if not -20 <= self.target_peak_dbfs <= -1:
            raise ValueError("target peak must be between -20 and -1 dBFS")
        if not 0 <= self.max_gain_db <= 12:
            raise ValueError("maximum gain must be between 0 and 12 dB")
        if self.target_peak_dbfs <= self.trim_threshold_dbfs:
            raise ValueError("target peak must be louder than the trim threshold")


def _samples(audio: PcmAudio) -> array[int]:
    samples = array("h")
    samples.frombytes(audio.pcm_s16le)
    if byteorder != "little":
        samples.byteswap()
    return samples


class ProcessedSpeechSynthesizer:
    """Trim unintended edge silence and normalize speech deterministically."""

    def __init__(
        self,
        inner: SpeechSynthesizer,
        settings: SpeechProcessingSettings | None = None,
    ) -> None:
        self.inner = inner
        self.settings = settings or SpeechProcessingSettings()
        self.sample_rate = inner.sample_rate
        self.metadata = AdapterMetadata(
            adapter_id="whoopy.processed_speech",
            versioned_model_id=inner.metadata.versioned_model_id,
            runtime_id=inner.metadata.runtime_id,
            runtime_version=inner.metadata.runtime_version,
            license_id=inner.metadata.license_id,
            device=inner.metadata.device,
            settings=(
                f"inner_cache_identity={inner.cache_identity}",
                f"trim_threshold_dbfs={self.settings.trim_threshold_dbfs}",
                f"edge_residual_ms={self.settings.edge_residual_ms}",
                f"edge_fade_ms={self.settings.edge_fade_ms}",
                f"target_peak_dbfs={self.settings.target_peak_dbfs}",
                f"max_gain_db={self.settings.max_gain_db}",
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        raw = self.inner.synthesize(segment)
        if raw.sample_rate != self.sample_rate:
            raise InvalidSynthesisOutput(
                f"Inner synthesizer changed sample rate to {raw.sample_rate} Hz."
            )
        samples = _samples(raw)
        threshold = round(32_767 * 10 ** (self.settings.trim_threshold_dbfs / 20))
        active = [index for index, sample in enumerate(samples) if abs(sample) >= threshold]
        if not active:
            raise InvalidSynthesisOutput("Speech contains no audio above the trim threshold")

        residual_frames = round(self.sample_rate * self.settings.edge_residual_ms / 1_000)
        start = max(0, active[0] - residual_frames)
        end = min(len(samples), active[-1] + residual_frames + 1)
        trimmed = samples[start:end]

        peak = max(abs(sample) for sample in trimmed)
        target_peak = round(32_767 * 10 ** (self.settings.target_peak_dbfs / 20))
        # Never greatly amplify a quiet segment: independent peak normalization
        # used to magnify low-level synthesis noise at every new utterance.
        maximum_gain = 10 ** (self.settings.max_gain_db / 20)
        gain = min(target_peak / peak, maximum_gain)
        fade_frames = round(self.sample_rate * self.settings.edge_fade_ms / 1_000)
        normalized = array("h")
        for index, sample in enumerate(trimmed):
            fade = 1.0
            if fade_frames:
                fade = min(
                    1.0,
                    (index + 1) / fade_frames,
                    (len(trimmed) - index) / fade_frames,
                )
            normalized.append(max(-32_767, min(32_767, round(sample * gain * fade))))
        if not normalized:
            raise InvalidSynthesisOutput("Speech post-processing produced invalid PCM")
        if byteorder != "little":
            normalized.byteswap()
        return PcmAudio(
            pcm_s16le=normalized.tobytes(),
            sample_rate=self.sample_rate,
        )
