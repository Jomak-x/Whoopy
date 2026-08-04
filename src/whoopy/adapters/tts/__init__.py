"""Local speech-synthesis adapter implementations."""

from whoopy.adapters.tts.fish_speech import FishSpeech14Adapter, FishSpeechSettings
from whoopy.adapters.tts.moss_tts import (
    MOSS_LANGUAGES,
    MossTTSAdapter,
    MossTTSSettings,
    MossVariant,
)
from whoopy.adapters.tts.sherpa_onnx import SherpaOnnxKokoroAdapter, SherpaOnnxSettings

__all__ = [
    "MOSS_LANGUAGES",
    "FishSpeech14Adapter",
    "FishSpeechSettings",
    "MossTTSAdapter",
    "MossTTSSettings",
    "MossVariant",
    "SherpaOnnxKokoroAdapter",
    "SherpaOnnxSettings",
]
