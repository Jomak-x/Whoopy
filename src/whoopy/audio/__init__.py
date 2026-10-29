"""Deterministic fixture audio rendering and quality inspection."""

from whoopy.audio.exports import (
    AudioExportError,
    ExportArtifact,
    ExportManifest,
    ExportMetadata,
    discover_ffmpeg,
    export_finished_audio,
    load_export_manifest,
)
from whoopy.audio.models import (
    AudioManifest,
    AudioQualityReport,
    QualityCheck,
    RenderedWave,
    SegmentAudioSpan,
)
from whoopy.audio.renderer import AudioRenderError, TimelineWaveRenderer
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    InvalidSynthesisOutput,
    SpeechSynthesizer,
    SynthesisError,
    TransientSynthesisError,
)

__all__ = [
    "AudioExportError",
    "AudioManifest",
    "AudioQualityReport",
    "AudioRenderError",
    "ExportArtifact",
    "ExportManifest",
    "ExportMetadata",
    "FatalSynthesisError",
    "InvalidSynthesisOutput",
    "QualityCheck",
    "RenderedWave",
    "SegmentAudioSpan",
    "SpeechSynthesizer",
    "SynthesisError",
    "TimelineWaveRenderer",
    "TransientSynthesisError",
    "discover_ffmpeg",
    "export_finished_audio",
    "load_export_manifest",
]
