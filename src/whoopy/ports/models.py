"""Stable typed ports for replaceable local script and speech backends."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from whoopy.audio.models import PcmAudio
    from whoopy.timeline import SpeechSegment


class AdapterMetadata(BaseModel):
    """Reproducible identity and provenance shared by all model adapters."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    adapter_id: str = Field(min_length=1)
    versioned_model_id: str = Field(min_length=1)
    runtime_id: str = Field(min_length=1)
    runtime_version: str = Field(min_length=1)
    license_id: str = Field(min_length=1)
    device: str = Field(min_length=1)
    settings: tuple[str, ...] = ()

    @property
    def cache_identity(self) -> str:
        """Return a compact identity that changes with every recorded setting."""

        payload = self.model_dump_json().encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()[:20]
        return f"{self.adapter_id}@{digest}"


class ScriptGenerationRequest(BaseModel):
    """Backend-neutral request for one bounded local text generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    prompt: str = Field(min_length=1)
    system_prompt: str | None = None
    max_output_tokens: int = Field(default=512, ge=1)
    seed: int = 0


class ScriptGenerationResult(BaseModel):
    """Text plus enough metadata to reproduce and inspect its generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    metadata: AdapterMetadata
    elapsed_seconds: float = Field(ge=0)


@runtime_checkable
class ScriptGenerator(Protocol):
    """Replaceable capability for one bounded piece of meditation text."""

    metadata: AdapterMetadata

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        """Generate text or raise a classified adapter error."""


@runtime_checkable
class SpeechSynthesizer(Protocol):
    """Replaceable capability for one canonical speech segment."""

    metadata: AdapterMetadata
    cache_identity: str
    sample_rate: int

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        """Generate mono signed 16-bit PCM or raise a classified error."""
