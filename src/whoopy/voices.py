"""Reviewed speaker IDs for the pinned sherpa-onnx Kokoro v1.0 bundle."""

from __future__ import annotations

from typing import Final

KOKORO_ENGLISH_VOICES: Final[dict[str, int]] = {
    "af_bella": 2,
    "af_heart": 3,
    "af_nicole": 6,
    "am_michael": 16,
}


def kokoro_speaker_id(voice_name: str) -> int:
    """Resolve a reviewed name without silently accepting an arbitrary ID."""

    try:
        return KOKORO_ENGLISH_VOICES[voice_name]
    except KeyError as error:
        available = ", ".join(sorted(KOKORO_ENGLISH_VOICES))
        raise ValueError(
            f"Unsupported Kokoro voice {voice_name!r}; currently available: {available}."
        ) from error
