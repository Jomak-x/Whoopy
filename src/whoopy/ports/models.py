"""Stable typed ports for replaceable local script and speech backends."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from whoopy.timeline.models import DeliveryIntensity, DeliveryStyle, PaceIntent, VoiceMode

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
    response_json_schema: dict[str, Any] | None = None
    raw_invocation_directory: Path | None = None


class ScriptGenerationTelemetry(BaseModel):
    """Per-invocation evidence reported by a text-generation adapter.

    Optional values are paired with durable reasons.  The default keeps custom
    and test adapters source-compatible while ensuring persisted production
    attempts never silently turn missing measurements into zeroes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    load_elapsed_seconds: float | None = Field(default=None, ge=0)
    load_elapsed_unavailable_reason: str | None = Field(
        default="adapter did not report a separate model-load duration", min_length=1
    )
    output_tokens: int | None = Field(default=None, ge=1)
    output_tokens_unavailable_reason: str | None = Field(
        default="adapter did not report generated-token count", min_length=1
    )
    output_token_elapsed_seconds: float | None = Field(default=None, gt=0)
    output_token_elapsed_unavailable_reason: str | None = Field(
        default="adapter did not report generated-token evaluation duration", min_length=1
    )
    output_tokens_per_second: float | None = Field(default=None, gt=0)
    output_tokens_per_second_unavailable_reason: str | None = Field(
        default="adapter did not report generated-token throughput", min_length=1
    )
    peak_process_tree_memory_bytes: int | None = Field(default=None, ge=1)
    peak_process_tree_memory_unavailable_reason: str | None = Field(
        default="adapter did not measure its inference process tree", min_length=1
    )
    live_process_tree_memory_after_bytes: int | None = Field(default=None, ge=0)
    live_process_tree_memory_after_unavailable_reason: str | None = Field(
        default="adapter did not measure its inference process tree after completion",
        min_length=1,
    )
    runtime_device: str | None = Field(default=None, min_length=1)
    runtime_device_unavailable_reason: str | None = Field(
        default="adapter did not report its selected runtime device", min_length=1
    )

    @model_validator(mode="after")
    def require_value_or_reason(self) -> ScriptGenerationTelemetry:
        pairs = (
            (self.load_elapsed_seconds, self.load_elapsed_unavailable_reason),
            (self.output_tokens, self.output_tokens_unavailable_reason),
            (self.output_token_elapsed_seconds, self.output_token_elapsed_unavailable_reason),
            (self.output_tokens_per_second, self.output_tokens_per_second_unavailable_reason),
            (
                self.peak_process_tree_memory_bytes,
                self.peak_process_tree_memory_unavailable_reason,
            ),
            (
                self.live_process_tree_memory_after_bytes,
                self.live_process_tree_memory_after_unavailable_reason,
            ),
            (self.runtime_device, self.runtime_device_unavailable_reason),
        )
        if any((value is None) == (reason is None) for value, reason in pairs):
            raise ValueError("generation telemetry requires exactly one of value or reason")
        return self


class ScriptGenerationResult(BaseModel):
    """Text plus enough metadata to reproduce and inspect its generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str = Field(min_length=1)
    metadata: AdapterMetadata
    elapsed_seconds: float = Field(ge=0)
    telemetry: ScriptGenerationTelemetry = Field(default_factory=ScriptGenerationTelemetry)


class SpeechCapabilities(BaseModel):
    """Controls one speech adapter can honestly apply to generated audio."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    voice_modes: tuple[VoiceMode, ...] = ("preset",)
    delivery_styles: tuple[DeliveryStyle, ...] = ("neutral",)
    intensity_levels: tuple[DeliveryIntensity, ...] = ("subtle",)
    pace_intents: tuple[PaceIntent, ...] = ("natural",)
    languages: tuple[str, ...] = ("English",)
    supports_freeform_instruction: bool = False
    supports_pronunciation_overrides: bool = False
    supports_duration_control: bool = False


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

    def close(self) -> None:
        """Release model memory and any owned subprocesses deterministically."""
