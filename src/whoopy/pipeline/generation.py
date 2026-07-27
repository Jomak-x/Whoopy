"""Typed configuration and model metadata persisted for real local runs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from whoopy.artifacts import TargetPlatform
from whoopy.audio.processing import SpeechProcessingSettings
from whoopy.ports import AdapterMetadata


class TTSRunSettings(BaseModel):
    """Structured controls required to reconstruct the exact TTS adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    voice_name: str = Field(min_length=1)
    speaker_id: int = Field(ge=0)
    speed: float = Field(gt=0)
    num_threads: int = Field(ge=1)
    provider: str = Field(min_length=1)
    language: str = Field(min_length=1)


class ScriptRunConfig(BaseModel):
    """Resolved, durable settings for a script-file render."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    mode: Literal["script_file"] = "script_file"
    profile: Literal["basic", "lite", "standard"]
    target: TargetPlatform
    tts: TTSRunSettings
    processing: SpeechProcessingSettings


class RunModelMetadata(BaseModel):
    """Exact adapter provenance saved before model-backed work begins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    tts: AdapterMetadata
    llm: AdapterMetadata | None = None
