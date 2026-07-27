"""Reusable assertions every concrete model adapter should satisfy."""

from __future__ import annotations

from whoopy.audio.quality import pcm_integrity_error
from whoopy.ports import (
    ScriptGenerationRequest,
    ScriptGenerator,
    SpeechSynthesizer,
)
from whoopy.timeline import SpeechSegment


def assert_script_generator_contract(generator: ScriptGenerator) -> None:
    assert isinstance(generator, ScriptGenerator)
    result = generator.generate(
        ScriptGenerationRequest(
            prompt="Return one calm sentence.",
            max_output_tokens=32,
            seed=7,
        )
    )
    assert result.text.strip()
    assert result.metadata == generator.metadata
    assert result.elapsed_seconds >= 0


def assert_speech_synthesizer_contract(synthesizer: SpeechSynthesizer) -> None:
    assert isinstance(synthesizer, SpeechSynthesizer)
    assert synthesizer.cache_identity == synthesizer.metadata.cache_identity
    audio = synthesizer.synthesize(
        SpeechSegment(id="speech-contract", text="Welcome to this calm moment.")
    )
    assert audio.sample_rate == synthesizer.sample_rate
    assert audio.frame_count > 0
    assert pcm_integrity_error(audio) is None
