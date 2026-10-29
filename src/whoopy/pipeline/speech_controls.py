"""Shared effective speech controls for initial rendering and revisions."""

from __future__ import annotations

from typing import cast

from whoopy.pipeline.generation import TTSRunSettings
from whoopy.ports import SpeechCapabilities
from whoopy.timeline import SpeechSegment, Timeline, TimelineSegment, VoiceMode


def apply_global_speech_overrides(
    timeline: Timeline,
    settings: TTSRunSettings,
    *,
    excluded_controls: dict[str, set[str]] | None = None,
) -> Timeline:
    changes = {
        key: value
        for key, value in {
            "delivery": settings.delivery_override,
            "intensity": settings.intensity_override,
            "pace": settings.pace_override,
        }.items()
        if value is not None
    }
    if not changes:
        return timeline
    excluded = excluded_controls or {}
    return timeline.model_copy(
        update={
            "segments": [
                segment.model_copy(
                    update={
                        name: value
                        for name, value in changes.items()
                        if name not in excluded.get(segment.id, ())
                    }
                )
                if isinstance(segment, SpeechSegment)
                else segment
                for segment in timeline.segments
            ]
        }
    )


def apply_speech_capability_fallbacks(
    timeline: Timeline,
    capabilities: SpeechCapabilities,
    *,
    voice_mode: str,
) -> Timeline:
    """Persist the controls an adapter will actually apply, never ignored intent."""

    segments: list[TimelineSegment] = []
    selected_voice_mode = (
        voice_mode if voice_mode in capabilities.voice_modes else capabilities.voice_modes[0]
    )
    for segment in timeline.segments:
        if not isinstance(segment, SpeechSegment):
            segments.append(segment)
            continue
        changes: dict[str, object] = {
            "voice_mode": selected_voice_mode,
            "delivery": (
                segment.delivery
                if segment.delivery in capabilities.delivery_styles
                else capabilities.delivery_styles[0]
            ),
            "intensity": (
                segment.intensity
                if segment.intensity in capabilities.intensity_levels
                else capabilities.intensity_levels[0]
            ),
            "pace": (
                segment.pace
                if segment.pace in capabilities.pace_intents
                else capabilities.pace_intents[0]
            ),
            "language": (
                segment.language
                if segment.language in capabilities.languages
                else capabilities.languages[0]
            ),
        }
        if not capabilities.supports_pronunciation_overrides:
            changes["pronunciation_overrides"] = ()
        if not capabilities.supports_duration_control:
            changes["target_duration_ms"] = None
        segments.append(segment.model_copy(update=changes))
    return timeline.model_copy(update={"segments": segments})


def effective_voice_mode(settings: TTSRunSettings) -> VoiceMode:
    if settings.backend == "fish-1.4":
        return "reference"
    if settings.backend in {"moss-local-v1.5", "moss-v1.5"}:
        return "reference" if settings.use_reference else "designed"
    if "qwen3-" in settings.backend:
        return settings.voice_mode
    return "preset"


def speech_capabilities(settings: TTSRunSettings) -> SpeechCapabilities:
    from whoopy.adapters.tts import (
        FishSpeech14Adapter,
        MossTTSAdapter,
        Qwen3TTSAdapter,
        Qwen3TTSVariant,
        SherpaOnnxKokoroAdapter,
    )
    from whoopy.adapters.tts.mlx_qwen3_tts import MlxQwenAdapter

    if settings.backend == "fish-1.4":
        return FishSpeech14Adapter.capabilities
    if settings.backend in {"moss-local-v1.5", "moss-v1.5"}:
        return MossTTSAdapter.capabilities
    if settings.backend.startswith("mlx-qwen3-"):
        return MlxQwenAdapter.capabilities_for_mode(settings.voice_mode)
    if settings.backend.startswith("qwen3-"):
        return Qwen3TTSAdapter.capabilities_for_variant(
            cast(Qwen3TTSVariant, settings.backend.removeprefix("qwen3-"))
        )
    return SherpaOnnxKokoroAdapter.capabilities
