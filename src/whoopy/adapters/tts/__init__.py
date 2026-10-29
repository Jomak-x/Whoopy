"""Local speech-synthesis adapter implementations."""

from whoopy.adapters.tts.fish_speech import FishSpeech14Adapter, FishSpeechSettings
from whoopy.adapters.tts.moss_tts import (
    MOSS_LANGUAGES,
    MossTTSAdapter,
    MossTTSSettings,
    MossVariant,
)
from whoopy.adapters.tts.qwen3_tts import (
    QWEN3_TTS_LANGUAGES,
    QWEN3_TTS_SPEAKERS,
    Qwen3TTSAdapter,
    Qwen3TTSInstallation,
    Qwen3TTSSettings,
    Qwen3TTSTask,
    Qwen3TTSVariant,
    Qwen3VoiceDesignPreset,
    resolve_qwen3_tts_installation,
)
from whoopy.adapters.tts.sherpa_onnx import SherpaOnnxKokoroAdapter, SherpaOnnxSettings

__all__ = [
    "MOSS_LANGUAGES",
    "QWEN3_TTS_LANGUAGES",
    "QWEN3_TTS_SPEAKERS",
    "FishSpeech14Adapter",
    "FishSpeechSettings",
    "MossTTSAdapter",
    "MossTTSSettings",
    "MossVariant",
    "Qwen3TTSAdapter",
    "Qwen3TTSInstallation",
    "Qwen3TTSSettings",
    "Qwen3TTSTask",
    "Qwen3TTSVariant",
    "Qwen3VoiceDesignPreset",
    "SherpaOnnxKokoroAdapter",
    "SherpaOnnxSettings",
    "resolve_qwen3_tts_installation",
]
