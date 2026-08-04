"""Typed configuration and model metadata persisted for real local runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from whoopy.artifacts import TargetPlatform
from whoopy.audio.processing import SpeechProcessingSettings
from whoopy.ports import AdapterMetadata


class TTSRunSettings(BaseModel):
    """Structured controls required to reconstruct the exact TTS adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal[
        "kokoro",
        "fish-1.4",
        "moss-local-v1.5",
        "moss-v1.5",
    ] = "kokoro"
    voice_name: str = Field(min_length=1)
    speaker_id: int = Field(ge=0)
    speed: float = Field(gt=0)
    num_threads: int = Field(ge=1)
    provider: str = Field(min_length=1)
    language: str = Field(min_length=1)
    seed: int = 42
    instruction: str = ""
    use_reference: bool = True


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


class ScriptRunConfig(BaseModel):
    """Resolved, durable settings for authored or generated script rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    mode: Literal["script_file", "generated_prompt"] = "script_file"
    profile: Literal["basic", "lite", "standard"]
    target: TargetPlatform
    tts: TTSRunSettings
    processing: SpeechProcessingSettings
    generation: GenerationRunSettings | None = None

    @model_validator(mode="after")
    def validate_mode(self) -> ScriptRunConfig:
        if self.mode == "script_file" and self.generation is not None:
            raise ValueError("script-file config cannot contain generation settings")
        if self.mode == "generated_prompt":
            if self.generation is None:
                raise ValueError("generated-prompt config requires generation settings")
            if self.profile == "basic":
                raise ValueError("generated-prompt config requires a local LLM profile")
        return self


class RunModelMetadata(BaseModel):
    """Exact adapter provenance saved before model-backed work begins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tts: AdapterMetadata
    llm: AdapterMetadata | None = None


class PendingGenerationConfig(BaseModel):
    """Immutable inputs required to restart prompt planning exactly.

    Unlike :class:`GenerationRunSettings`, this record is written before the
    first LLM call, so it deliberately excludes values such as estimated
    duration that only exist after a draft has been compiled.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    profile: Literal["lite", "standard"]
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
    model_metadata: RunModelMetadata

    @model_validator(mode="after")
    def require_llm_metadata(self) -> PendingGenerationConfig:
        """A resumable prompt request must identify both model adapters."""

        if self.model_metadata.llm is None:
            raise ValueError("pending generation config requires LLM metadata")
        return self
