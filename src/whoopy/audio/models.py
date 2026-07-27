"""Typed in-memory PCM and durable Phase 2 audio artifact metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

SAMPLE_RATE: Literal[24_000] = 24_000
CHANNELS: Literal[1] = 1
SAMPLE_WIDTH_BYTES: Literal[2] = 2


@dataclass(frozen=True)
class PcmAudio:
    """Mono signed 16-bit little-endian PCM with no container header."""

    pcm_s16le: bytes
    sample_rate: int = SAMPLE_RATE

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample rate must be positive")
        if len(self.pcm_s16le) % SAMPLE_WIDTH_BYTES != 0:
            raise ValueError("16-bit PCM byte length must be divisible by two")

    @property
    def frame_count(self) -> int:
        return len(self.pcm_s16le) // SAMPLE_WIDTH_BYTES


class SegmentAudioSpan(BaseModel):
    """Exact location of one timeline segment in the assembled PCM stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    segment_id: str
    segment_type: Literal["SPEECH", "SILENCE"]
    start_frame: int = Field(ge=0)
    end_frame: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    requested_duration_ms: int | None = Field(default=None, gt=0)
    actual_duration_ms: float = Field(gt=0)
    cache_key: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    pcm_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class AudioManifest(BaseModel):
    """Reviewable map from timeline segments to exact WAV frame ranges."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 2
    run_id: UUID
    timeline_schema_version: int = Field(ge=1)
    format: Literal["pcm_s16le_wav"] = "pcm_s16le_wav"
    sample_rate: Literal[24_000] = SAMPLE_RATE
    channels: Literal[1] = CHANNELS
    sample_width_bytes: Literal[2] = SAMPLE_WIDTH_BYTES
    total_frames: int = Field(gt=0)
    duration_ms: float = Field(gt=0)
    pcm_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    segments: list[SegmentAudioSpan] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_integrity_metadata(self) -> AudioManifest:
        """Require hashes and speech cache keys in the Phase 3 manifest."""

        if self.schema_version == 1:
            if self.pcm_sha256 is not None:
                raise ValueError("audio manifest schema v1 does not support a PCM hash")
            if any(
                span.cache_key is not None or span.pcm_sha256 is not None for span in self.segments
            ):
                raise ValueError("audio manifest schema v1 does not support segment hashes")
            return self

        if self.pcm_sha256 is None:
            raise ValueError("audio manifest schema v2 requires a whole-file PCM hash")
        for span in self.segments:
            if span.pcm_sha256 is None:
                raise ValueError("audio manifest schema v2 requires every segment PCM hash")
            if span.segment_type == "SPEECH" and span.cache_key is None:
                raise ValueError("audio manifest schema v2 requires speech cache keys")
            if span.segment_type == "SILENCE" and span.cache_key is not None:
                raise ValueError("SILENCE segments cannot have synthesis cache keys")
        return self


class QualityCheck(BaseModel):
    """One explicit, machine-readable audio acceptance check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    passed: bool
    detail: str


class AudioQualityReport(BaseModel):
    """Basic integrity result written beside every Phase 2 WAV artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 2
    passed: bool
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    sample_width_bytes: int = Field(gt=0)
    frame_count: int = Field(gt=0)
    duration_ms: float = Field(gt=0)
    peak_dbfs: float | None
    clipped_samples: int = Field(ge=0)
    pcm_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    max_join_delta: int | None = Field(default=None, ge=0)
    headroom_limit_dbfs: float | None = None
    checks: list[QualityCheck] = Field(min_length=1)


@dataclass(frozen=True)
class RenderedWave:
    """Complete renderer output before the run store persists it."""

    wave_bytes: bytes
    manifest: AudioManifest
    quality: AudioQualityReport
