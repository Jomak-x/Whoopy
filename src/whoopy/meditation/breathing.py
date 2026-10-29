"""Reviewed breathing modes; models never invent ratios, holds, or timing."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class BreathProtocol(BaseModel):
    """A gentle inhale/exhale protocol with an explicit no-hold invariant."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_id: str
    version: int = Field(ge=1)
    inhale_seconds: int = Field(ge=2, le=6)
    exhale_seconds: int = Field(ge=2, le=8)
    holds: Literal[False] = False
    minimum_cycles: int = Field(ge=2, le=8)
    maximum_cycles: int = Field(ge=2, le=12)
    opt_out_guidance: str
    comfort_guidance: str

    @model_validator(mode="after")
    def ordered_cycles(self) -> BreathProtocol:
        if self.maximum_cycles < self.minimum_cycles:
            raise ValueError("maximum cycles must be at least minimum cycles")
        return self

    @property
    def versioned_id(self) -> str:
        return f"{self.protocol_id}@{self.version}"


REVIEWED_BREATH_PROTOCOLS: dict[str, BreathProtocol] = {
    protocol.versioned_id: protocol
    for protocol in (
        BreathProtocol(
            protocol_id="gentle_even_4_4",
            version=1,
            inhale_seconds=4,
            exhale_seconds=4,
            minimum_cycles=3,
            maximum_cycles=6,
            opt_out_guidance=(
                "If timed breathing is not comfortable, let the breath return "
                "to its natural rhythm."
            ),
            comfort_guidance=(
                "Stop the timing if you feel dizzy, strained, short of breath, or uncomfortable."
            ),
        ),
        BreathProtocol(
            protocol_id="gentle_long_exhale_3_4",
            version=1,
            inhale_seconds=3,
            exhale_seconds=4,
            minimum_cycles=3,
            maximum_cycles=6,
            opt_out_guidance=(
                "You can leave the counting at any time and breathe in your usual way."
            ),
            comfort_guidance=(
                "Stop the timing if you feel dizzy, strained, short of breath, or uncomfortable."
            ),
        ),
    )
}


def compile_breath_protocol(protocol_id: str, *, cycles: int) -> str:
    """Compile reviewed timings into typed timeline markers and safety guidance."""

    try:
        protocol = REVIEWED_BREATH_PROTOCOLS[protocol_id]
    except KeyError as error:
        raise ValueError(f"Unknown reviewed breath protocol {protocol_id!r}.") from error
    if not protocol.minimum_cycles <= cycles <= protocol.maximum_cycles:
        raise ValueError(
            f"{protocol_id} requires {protocol.minimum_cycles}-{protocol.maximum_cycles} cycles."
        )
    parts = [protocol.opt_out_guidance, protocol.comfort_guidance]
    for cycle in range(1, cycles + 1):
        parts.extend(
            [
                f"Inhale gently for {protocol.inhale_seconds} seconds.",
                (
                    f"[breath: inhale {protocol.inhale_seconds}s cycle={cycle} "
                    f"protocol={protocol.versioned_id}]"
                ),
                f"Exhale gently for {protocol.exhale_seconds} seconds.",
                (
                    f"[breath: exhale {protocol.exhale_seconds}s cycle={cycle} "
                    f"protocol={protocol.versioned_id}]"
                ),
            ]
        )
    return "\n\n".join(parts) + "\n"
