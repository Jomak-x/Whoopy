from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

import pytest

from whoopy.adapters.tts.qwen3_tts import (
    Qwen3TTSAdapter,
    Qwen3TTSSettings,
    resolve_qwen3_tts_installation,
)
from whoopy.audio.synthesis import FatalSynthesisError
from whoopy.model_packs.registry import load_model_pack_registry
from whoopy.timeline import SpeechSegment, build_script_timeline


def _settings(
    tmp_path: Path,
    variant: str,
    *,
    consent: bool = True,
) -> Qwen3TTSSettings:
    reference_audio: Path | None = None
    reference_text: Path | None = None
    consent_scope: str | None = None
    if variant.startswith("base-clone"):
        reference_audio = tmp_path / "reference.wav"
        reference_text = tmp_path / "reference.txt"
        reference_audio.write_bytes(b"owned-reference")
        reference_text.write_text("This is my recorded reference.", encoding="utf-8")
        if consent:
            consent_scope = "local_voice_cloning_experiment_only"
    return Qwen3TTSSettings(
        runtime_directory=tmp_path / "runtime",
        worker_script=tmp_path / "qwen-worker.py",
        model_directory=tmp_path / "model",
        variant=variant,  # type: ignore[arg-type]
        model_revision="test-model-revision",
        runtime_revision="test-runtime-revision",
        reference_audio=reference_audio,
        reference_text=reference_text,
        consent_scope=consent_scope,  # type: ignore[arg-type]
        models_root=tmp_path / "models",
    )


@pytest.mark.parametrize("variant", ["base-clone-0.6b", "base-clone-1.7b"])
def test_qwen_base_requires_explicit_voice_clone_consent(tmp_path: Path, variant: str) -> None:
    with pytest.raises(ValueError, match="explicit local experiment consent"):
        _settings(tmp_path, variant, consent=False)


def test_qwen_custom_voice_1_7b_translates_typed_controls_without_speaking_them(
    tmp_path: Path,
) -> None:
    adapter = Qwen3TTSAdapter(_settings(tmp_path, "custom-voice-1.7b"))
    timeline = build_script_timeline(
        run_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        created_at=datetime(2026, 8, 5, tzinfo=UTC),
        script="""[delivery: calm]
[intensity: moderate]
[pace: slow]
Settle into this moment.
""",
    )
    segment = timeline.segments[0]
    assert isinstance(segment, SpeechSegment)

    request = adapter.translate_segment(segment)

    assert request["text"] == "Settle into this moment."
    assert request["speaker"] == "Serena"
    assert "calm" in str(request["instruction"])
    assert "slowly" in str(request["instruction"])
    assert "[delivery" not in str(request)
    assert adapter.capabilities.voice_modes == ("preset",)
    assert adapter.capabilities.supports_freeform_instruction


def test_qwen_custom_voice_0_6b_exposes_only_controls_upstream_applies(
    tmp_path: Path,
) -> None:
    adapter = Qwen3TTSAdapter(_settings(tmp_path, "custom-voice-0.6b"))
    segment = SpeechSegment(
        id="speech-1",
        text="Settle into this moment.",
        voice_mode="preset",
    )

    request = adapter.translate_segment(segment)

    assert request == {
        "task": "custom_voice",
        "text": "Settle into this moment.",
        "language": "English",
        "seed": 42,
        "speaker": "Serena",
    }
    assert adapter.capabilities.delivery_styles == ("neutral",)
    assert adapter.capabilities.intensity_levels == ("subtle",)
    assert adapter.capabilities.pace_intents == ("natural",)
    assert not adapter.capabilities.supports_freeform_instruction
    assert "instruction_support=none" in adapter.metadata.settings
    assert (
        "generation_policy=sampled_then_greedy_on_invalid_probabilities@1"
        in adapter.metadata.settings
    )


def test_qwen_voice_design_derives_instruction_from_reviewed_profile(tmp_path: Path) -> None:
    adapter = Qwen3TTSAdapter(_settings(tmp_path, "voice-design-1.7b"))
    segment = SpeechSegment(
        id="speech-1",
        text="Notice the support beneath you.",
        voice_mode="designed",
        delivery="grounded",
        pace="unhurried",
    )

    request = adapter.translate_segment(segment)

    assert request["task"] == "voice_design"
    assert "warm, calm adult narrator" in str(request["instruction"])
    assert "grounded" in str(request["instruction"])
    assert request["text"] == segment.text
    assert adapter.capabilities.voice_modes == ("designed",)


@pytest.mark.parametrize("variant", ["base-clone-0.6b", "base-clone-1.7b"])
def test_qwen_base_clone_sends_no_unimplemented_expression_instruction(
    tmp_path: Path, variant: str
) -> None:
    adapter = Qwen3TTSAdapter(_settings(tmp_path, variant))
    request = adapter.translate_segment(
        SpeechSegment(
            id="speech-1",
            text="Take one natural breath.",
            voice_mode="reference",
        )
    )

    assert request == {
        "task": "voice_clone",
        "text": "Take one natural breath.",
        "language": "English",
        "seed": 42,
    }
    assert adapter.capabilities.delivery_styles == ("neutral",)
    assert not adapter.capabilities.supports_freeform_instruction
    assert "consent_scope=local_voice_cloning_experiment_only" in adapter.metadata.settings
    assert adapter.metadata.versioned_model_id.endswith("@test-model-revision")
    assert adapter.metadata.runtime_version == "test-runtime-revision"


@pytest.mark.parametrize("variant", ["custom-voice-0.6b", "base-clone-0.6b"])
def test_qwen_rejects_controls_unsupported_by_selected_variant(
    tmp_path: Path, variant: str
) -> None:
    adapter = Qwen3TTSAdapter(_settings(tmp_path, variant))

    with pytest.raises(FatalSynthesisError, match="does not support delivery calm"):
        adapter.translate_segment(
            SpeechSegment(
                id="speech-1",
                text="Settle here.",
                voice_mode="preset" if variant.startswith("custom") else "reference",
                delivery="calm",
            )
        )


def test_qwen_availability_reports_clean_isolated_runtime_remedy(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "custom-voice-0.6b")

    assert Qwen3TTSAdapter.availability_error(settings) == (
        "missing isolated Python runtime, Qwen3-TTS worker, model checkpoint"
    )


def test_qwen_cache_identity_changes_with_preset_speaker(tmp_path: Path) -> None:
    first_settings = _settings(tmp_path, "custom-voice-0.6b")
    second_settings = Qwen3TTSSettings(
        **{
            **first_settings.__dict__,
            "speaker": "Aiden",
        }
    )

    assert (
        Qwen3TTSAdapter(first_settings).cache_identity
        != Qwen3TTSAdapter(second_settings).cache_identity
    )


def test_reviewed_custom_voice_pack_is_exact_and_absent_files_fail_closed(
    tmp_path: Path,
) -> None:
    registry_path = Path("config/model_packs.yaml")
    registry = load_model_pack_registry(registry_path, models_root=tmp_path)
    pack = registry.get("qwen3-tts-custom-voice-0.6b")

    assert pack.revision == "85e237c12c027371202489a0ec509ded67b5e4b5"
    assert pack.runtime.revision == "022e286b98fbec7e1e916cb940cdf532cd9f488e"
    assert sum(item.size_bytes for item in pack.files) == 2_498_383_610

    with pytest.raises(FatalSynthesisError, match=r"is missing;.*pinned files"):
        resolve_qwen3_tts_installation(
            tmp_path,
            "custom-voice-0.6b",
            registry_path=registry_path,
        )


@pytest.mark.parametrize(
    "variant",
    [
        "custom-voice-1.7b",
        "voice-design-1.7b",
        "base-clone-0.6b",
        "base-clone-1.7b",
    ],
)
def test_registered_but_missing_qwen_variants_remain_fail_closed(
    tmp_path: Path,
    variant: Literal[
        "custom-voice-1.7b",
        "voice-design-1.7b",
        "base-clone-0.6b",
        "base-clone-1.7b",
    ],
) -> None:
    with pytest.raises(FatalSynthesisError, match=r"is missing; 0 of 11 pinned files"):
        resolve_qwen3_tts_installation(
            tmp_path,
            variant,
            registry_path=Path("config/model_packs.yaml"),
        )
