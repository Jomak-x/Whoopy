"""Speech-synthesis contract and the failure types used by retry logic."""

from __future__ import annotations

import hashlib
import json
import unicodedata

from whoopy.ports.errors import (
    AdapterError,
    FatalAdapterError,
    InvalidAdapterOutput,
    TransientAdapterError,
)
from whoopy.ports.models import SpeechSynthesizer as SpeechSynthesizer
from whoopy.timeline import SpeechSegment


class SynthesisError(AdapterError):
    """Base class for errors that a speech adapter deliberately classifies."""


class TransientSynthesisError(SynthesisError, TransientAdapterError):
    """A temporary failure that may succeed when the same segment is retried."""


class FatalSynthesisError(SynthesisError, FatalAdapterError):
    """A deterministic failure that should be surfaced without retrying."""


class InvalidSynthesisOutput(FatalSynthesisError, InvalidAdapterOutput):
    """Speech output that violates Whoopy's PCM contract."""


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
