"""Typed configuration and model metadata persisted for real local runs."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from whoopy.artifacts import TargetPlatform
from whoopy.audio.processing import SpeechProcessingSettings
from whoopy.hardware import HardwareSnapshot
from whoopy.ports import AdapterMetadata
from whoopy.timeline import DeliveryIntensity, DeliveryStyle, PaceIntent, VoiceMode

MeditationKind = Literal[
    "grounding",
    "focused_attention",
    "breath_awareness",
    "body_scan",
    "noting",
    "reflection",
    "loving_kindness",
    "self_compassion",
    "visualization",
    "open_awareness",
    "sleep",
]
WritingMethod = Literal[
    "plan_first",
    "blueprint_first_sparse",
    "three_candidate_structural_selection",
    "recipe_whole_session",
]


class PacingRunSettings(BaseModel):
    """Content identity of the reviewed pacing policy used for this run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,95}$")
    version: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


QwenTTSBackend = Literal[
    "qwen3-custom-voice-0.6b",
    "qwen3-custom-voice-1.7b",
    "qwen3-voice-design-1.7b",
    "qwen3-base-clone-0.6b",
    "qwen3-base-clone-1.7b",
]
_QWEN_BACKENDS = {
    "qwen3-custom-voice-0.6b": "preset",
    "qwen3-custom-voice-1.7b": "preset",
    "qwen3-voice-design-1.7b": "designed",
    "qwen3-base-clone-0.6b": "reference",
    "qwen3-base-clone-1.7b": "reference",
}


class TTSRunSettings(BaseModel):
    """Structured controls required to reconstruct the exact TTS adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal[
        "kokoro",
        "fish-1.4",
        "moss-local-v1.5",
        "moss-v1.5",
        "qwen3-custom-voice-0.6b",
        "qwen3-custom-voice-1.7b",
        "qwen3-voice-design-1.7b",
        "qwen3-base-clone-0.6b",
        "qwen3-base-clone-1.7b",
        "mlx-qwen3-custom-voice-1.7b-8bit",
        "mlx-qwen3-voice-design-1.7b-8bit",
    ] = "kokoro"
    voice_name: str = Field(min_length=1)
    speaker_id: int = Field(ge=0)
    speed: float = Field(gt=0)
    num_threads: int = Field(ge=1)
    provider: str = Field(min_length=1)
    language: str = Field(min_length=1)
    seed: int = 42
    instruction: str = ""
    delivery_override: DeliveryStyle | None = None
    intensity_override: DeliveryIntensity | None = None
    pace_override: PaceIntent | None = None
    use_reference: bool = True
    voice_mode: VoiceMode = "preset"
    experimental_installation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    experimental_worker_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    experimental_adapter_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    experimental_model_revision: str | None = None
    experimental_runtime_revision: str | None = None
    experimental_classification: Literal["experimental_local"] | None = None
    experimental_managed_ready: Literal[False] | None = None
    qwen_model_revision: str | None = None
    qwen_runtime_revision: str | None = None
    qwen_voice_design_preset: Literal[
        "warm_meditation", "grounded_meditation", "sleep_meditation"
    ] = "warm_meditation"
    reference_id: str | None = None
    reference_revision: int | None = Field(default=None, ge=1)
    reference_revision_id: str | None = None
    reference_audio_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reference_transcript_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    reference_consent_scope: Literal["local_voice_cloning_experiment_only"] | None = None

    @model_validator(mode="after")
    def validate_qwen_contract(self) -> TTSRunSettings:
        experimental_values = (
            self.experimental_installation_sha256,
            self.experimental_worker_sha256,
            self.experimental_adapter_sha256,
            self.experimental_model_revision,
            self.experimental_runtime_revision,
            self.experimental_classification,
            self.experimental_managed_ready,
        )
        if self.backend.startswith("mlx-qwen3-"):
            if any(value is None for value in experimental_values):
                raise ValueError("Experimental MLX requires complete pinned local provenance")
            if not self.experimental_model_revision or not self.experimental_runtime_revision:
                raise ValueError("Experimental MLX revisions cannot be empty")
            mode = "preset" if "custom-voice" in self.backend else "designed"
            if self.voice_mode != mode or self.use_reference:
                raise ValueError("Experimental MLX requires its declared mode without references")
            if self.language != "English" or not 0 <= self.seed < 2**32:
                raise ValueError("Experimental MLX requires English and an unsigned 32-bit seed")
            voices = (
                {"Ryan", "Aiden", "Serena"}
                if mode == "preset"
                else {"warm_meditation", "grounded_meditation", "sleep_meditation"}
            )
            if self.voice_name not in voices:
                raise ValueError("Unsupported experimental MLX voice")
        elif any(value is not None for value in experimental_values):
            raise ValueError("Experimental provenance is valid only for experimental MLX backends")
        required_mode = _QWEN_BACKENDS.get(self.backend)
        reference_values = (
            self.reference_id,
            self.reference_revision,
            self.reference_revision_id,
            self.reference_audio_sha256,
            self.reference_transcript_sha256,
            self.reference_consent_scope,
        )
        if required_mode is None:
            if self.qwen_model_revision is not None or self.qwen_runtime_revision is not None:
                raise ValueError("Qwen revisions are valid only for Qwen3-TTS backends")
            has_reference = any(value is not None for value in reference_values)
            if has_reference and self.backend not in {
                "fish-1.4",
                "moss-local-v1.5",
                "moss-v1.5",
            }:
                raise ValueError(
                    "durable voice references are valid only for reference-capable backends"
                )
            if has_reference and any(value is None for value in reference_values):
                raise ValueError("a durable voice reference revision must be complete")
            if has_reference and not self.use_reference:
                raise ValueError("direct voice mode cannot contain a durable voice reference")
            return self
        if self.voice_mode != required_mode:
            raise ValueError(f"{self.backend} requires voice_mode={required_mode}")
        if not self.qwen_model_revision or not self.qwen_runtime_revision:
            raise ValueError("Qwen3-TTS requires exact model and runtime revisions")
        is_clone = required_mode == "reference"
        if is_clone and any(value is None for value in reference_values):
            raise ValueError("Qwen Base cloning requires a complete consented reference revision")
        if not is_clone and any(value is not None for value in reference_values):
            raise ValueError("only Qwen Base cloning accepts a durable voice reference")
        return self


class GenerationRunSettings(BaseModel):
    """Reproducible text-generation controls and prompt versions."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    duration_seconds: int = Field(ge=60, le=1_800)
    seed: int
    plan_prompt_id: str = Field(min_length=1)
    plan_prompt_version: int = Field(ge=1)
    section_prompt_id: str = Field(min_length=1)
    section_prompt_version: int = Field(ge=1)
    max_parallel_sections: int = Field(ge=1, le=2)
    estimated_duration_seconds: float = Field(gt=0)
    meditation_kind: MeditationKind | None = None
    writing_method: WritingMethod = "plan_first"
    pacing: PacingRunSettings | None = None


class ScriptRunConfig(BaseModel):
    """Resolved, durable settings for authored or generated script rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    mode: Literal["script_file", "generated_prompt"] = "script_file"
    profile: Literal["basic", "lite", "standard", "high"]
    target: TargetPlatform
    tts: TTSRunSettings
    processing: SpeechProcessingSettings
    generation: GenerationRunSettings | None = None
    requested_duration_seconds: int | None = Field(default=None, ge=60, le=1_800)

    @model_validator(mode="after")
    def validate_mode(self) -> ScriptRunConfig:
        if self.mode == "script_file" and self.generation is not None:
            raise ValueError("script-file config cannot contain generation settings")
        if self.mode == "generated_prompt":
            if self.requested_duration_seconds is not None:
                raise ValueError("generated-prompt duration must come from generation settings")
            if self.generation is None:
                raise ValueError("generated-prompt config requires generation settings")
            if self.profile == "basic":
                raise ValueError("generated-prompt config requires a local LLM profile")
        return self


def resolved_synthesis_digest(config: ScriptRunConfig, metadata: AdapterMetadata) -> str:
    payload = {
        "tts": config.tts.model_dump(mode="json"),
        "processing": config.model_dump(mode="json")["processing"],
        "adapter": metadata.model_dump(mode="json"),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class TelemetryMeasurement(BaseModel):
    """A measured value, or a durable explanation of why it was unavailable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | int | None = Field(default=None, ge=0)
    unit: Literal[
        "seconds",
        "bytes",
        "calls",
        "audio_seconds_per_second",
        "tokens_per_second",
    ]
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_value_or_reason(self) -> TelemetryMeasurement:
        if (self.value is None) == (self.unavailable_reason is None):
            raise ValueError("telemetry requires exactly one of value or unavailable_reason")
        return self


class HardwareTelemetry(BaseModel):
    """One truthful hardware snapshot, including an explicit inspection failure."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: HardwareSnapshot | None = None
    unavailable_reason: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def require_snapshot_or_reason(self) -> HardwareTelemetry:
        if (self.snapshot is None) == (self.unavailable_reason is None):
            raise ValueError("hardware telemetry requires a snapshot or unavailable_reason")
        return self


class AdapterRunTelemetry(BaseModel):
    """Runtime evidence for one adapter during this specific production run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    load_elapsed: TelemetryMeasurement
    inference_elapsed: TelemetryMeasurement
    inference_speed: TelemetryMeasurement
    peak_process_tree_memory: TelemetryMeasurement
    live_process_tree_memory_after: TelemetryMeasurement
    inference_calls: TelemetryMeasurement
    runtime_device: str | None = Field(default=None, min_length=1, max_length=200)
    runtime_device_unavailable_reason: str | None = Field(
        default=None, min_length=1, max_length=500
    )

    @model_validator(mode="after")
    def require_runtime_device_or_reason(self) -> AdapterRunTelemetry:
        if (self.runtime_device is None) == (self.runtime_device_unavailable_reason is None):
            raise ValueError("runtime device requires a value or unavailable reason")
        return self


class RunTelemetry(BaseModel):
    """Per-run production telemetry kept beside exact adapter provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    captured_at: AwareDatetime
    hardware_before: HardwareTelemetry
    hardware_after: HardwareTelemetry
    tts: AdapterRunTelemetry
    llm: AdapterRunTelemetry | None = None


class RunModelMetadata(BaseModel):
    """Exact adapter provenance plus optional evidence from a completed run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tts: AdapterMetadata
    llm: AdapterMetadata | None = None
    telemetry: RunTelemetry | None = None


class PendingGenerationConfig(BaseModel):
    """Immutable inputs required to restart prompt planning exactly.

    Unlike :class:`GenerationRunSettings`, this record is written before the
    first LLM call, so it deliberately excludes values such as estimated
    duration that only exist after a draft has been compiled.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    profile: Literal["lite", "standard", "high"]
    target: TargetPlatform
    tts: TTSRunSettings
    processing: SpeechProcessingSettings
    duration_seconds: int = Field(ge=60, le=1_800)
    seed: int
    plan_prompt_id: str = Field(min_length=1)
    plan_prompt_version: int = Field(ge=1)
    section_prompt_id: str = Field(min_length=1)
    section_prompt_version: int = Field(ge=1)
    max_parallel_sections: int = Field(ge=1, le=2)
    meditation_kind: MeditationKind | None = None
    writing_method: WritingMethod = "plan_first"
    pacing: PacingRunSettings | None = None
    model_metadata: RunModelMetadata

    @model_validator(mode="after")
    def require_llm_metadata(self) -> PendingGenerationConfig:
        """A resumable prompt request must identify both model adapters."""

        if self.model_metadata.llm is None:
            raise ValueError("pending generation config requires LLM metadata")
        return self
