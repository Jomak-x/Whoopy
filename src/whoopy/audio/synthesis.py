"""Speech-synthesis contract and the failure types used by retry logic."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from typing import Protocol

from whoopy.audio.models import PcmAudio
from whoopy.timeline import SpeechSegment


class SpeechSynthesizer(Protocol):
    """Small replaceable boundary implemented by fixture and future TTS adapters."""

    cache_identity: str
    sample_rate: int

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        """Generate one speech segment or raise a classified synthesis error."""


class SynthesisError(RuntimeError):
    """Base class for errors that a speech adapter deliberately classifies."""


class TransientSynthesisError(SynthesisError):
    """A temporary failure that may succeed when the same segment is retried."""


class FatalSynthesisError(SynthesisError):
    """A deterministic failure that should be surfaced without retrying."""


def _normalized_text(text: str) -> str:
    """Normalize equivalent Unicode and whitespace without changing case."""

    return " ".join(unicodedata.normalize("NFKC", text).split())


def synthesis_input_bytes(
    segment: SpeechSegment,
    synthesizer: SpeechSynthesizer,
) -> bytes:
    """Return the canonical bytes whose digest identifies synthesis output."""

    # Future voice, speed, delivery-mode, seed, and model fields belong here
    # when they enter SpeechSegment. Omitting an output-affecting input would
    # incorrectly reuse audio, so this function is intentionally centralized.
    values = {
        "format": "pcm_s16le_mono",
        "sample_rate": synthesizer.sample_rate,
        "segment_type": segment.type,
        "synthesizer_identity": synthesizer.cache_identity,
        "text": _normalized_text(segment.text),
    }
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def cache_key_for(segment: SpeechSegment, synthesizer: SpeechSynthesizer) -> str:
    """Hash every current synthesis-affecting input for one speech segment."""

    return hashlib.sha256(synthesis_input_bytes(segment, synthesizer)).hexdigest()
