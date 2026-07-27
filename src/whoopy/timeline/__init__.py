"""Canonical timeline data contracts.

Phase 1 intentionally exports only the smallest useful timeline. Later phases
extend this package with silence, breath, music, cue compilation, and schema
migrations without changing the run/worker boundary established here.
"""

from whoopy.timeline.models import (
    SilenceSegment,
    SpeechSegment,
    Timeline,
    TimelineSegment,
    build_fixture_timeline,
    build_prompt_timeline,
)

__all__ = [
    "SilenceSegment",
    "SpeechSegment",
    "Timeline",
    "TimelineSegment",
    "build_fixture_timeline",
    "build_prompt_timeline",
]
