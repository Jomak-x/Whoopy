"""Lazy local Kokoro speech synthesis through verified sherpa-onnx assets."""

from __future__ import annotations

import importlib
import math
import sys
from array import array
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from sys import byteorder
from typing import Any

from whoopy.artifacts import ArtifactLock, ArtifactSpec, ArtifactStore, TargetPlatform
from whoopy.audio.models import SAMPLE_RATE, PcmAudio
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    InvalidSynthesisOutput,
    TransientSynthesisError,
)
from whoopy.ports import AdapterMetadata
from whoopy.timeline import SpeechSegment

ModuleLoader = Callable[[], Any]


@dataclass(frozen=True)
class SherpaOnnxSettings:
    """Kokoro controls that affect generated PCM and therefore cache identity."""

    voice_name: str = "af_heart"
    speaker_id: int = 3
    speed: float = 0.9
    num_threads: int = 2
    provider: str = "cpu"
    language: str = "en-us"

    def __post_init__(self) -> None:
        if not self.voice_name:
            raise ValueError("Kokoro voice name cannot be empty")
        if self.speaker_id < 0:
            raise ValueError("Kokoro speaker ID cannot be negative")
        if self.speed <= 0:
            raise ValueError("Kokoro speed must be positive")
        if self.num_threads < 1:
            raise ValueError("sherpa-onnx thread count must be positive")
        if not self.provider:
            raise ValueError("sherpa-onnx provider cannot be empty")


def _load_sherpa_onnx() -> Any:
    return importlib.import_module("sherpa_onnx")


def _loader_from_directory(directory: Path) -> ModuleLoader:
    def load() -> Any:
        import_path = str(directory)
        if import_path not in sys.path:
            sys.path.insert(0, import_path)
        importlib.invalidate_caches()
        return importlib.import_module("sherpa_onnx")

    return load


def _component(artifacts: Sequence[ArtifactSpec], component: str) -> ArtifactSpec:
    matches = [artifact for artifact in artifacts if artifact.component == component]
    if len(matches) != 1:
        raise FatalSynthesisError(
            f"Expected one resolved {component} artifact, found {len(matches)}."
        )
    return matches[0]


def _kokoro_root(installed_path: Path) -> Path:
    models = sorted(installed_path.rglob("model.onnx"))
    valid = [
        model.parent
        for model in models
        if all(
            (model.parent / filename).is_file()
            for filename in (
                "voices.bin",
                "tokens.txt",
                "lexicon-us-en.txt",
            )
        )
        and (model.parent / "espeak-ng-data").is_dir()
    ]
    if len(valid) != 1:
        raise FatalSynthesisError(
            f"Expected one complete Kokoro model directory under {installed_path}, "
            f"found {len(valid)}."
        )
    return valid[0]


class SherpaOnnxKokoroAdapter:
    """Generate Whoopy PCM while importing and initializing sherpa only on first use."""

    sample_rate: int = SAMPLE_RATE

    def __init__(
        self,
        *,
        model_directory: Path,
        model_version: str,
        runtime_version: str,
        license_id: str,
        settings: SherpaOnnxSettings | None = None,
        module_loader: ModuleLoader | None = None,
    ) -> None:
        self.model_directory = _kokoro_root(model_directory)
        self.settings = settings or SherpaOnnxSettings()
        self.module_loader = module_loader or _load_sherpa_onnx
        self._engine: Any | None = None
        self.metadata = AdapterMetadata(
            adapter_id="whoopy.sherpa_onnx_kokoro",
            versioned_model_id=f"Kokoro-multilang@{model_version}",
            runtime_id="sherpa-onnx",
            runtime_version=runtime_version,
            license_id=license_id,
            device=self.settings.provider,
            settings=(
                f"voice={self.settings.voice_name}",
                f"speaker_id={self.settings.speaker_id}",
                f"speed={self.settings.speed}",
                f"threads={self.settings.num_threads}",
                f"language={self.settings.language}",
                f"sample_rate={self.sample_rate}",
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    @classmethod
    def from_artifact_store(
        cls,
        *,
        artifact_lock: ArtifactLock,
        store: ArtifactStore,
        profile_name: str,
        target: TargetPlatform,
        settings: SherpaOnnxSettings | None = None,
        module_loader: ModuleLoader | None = None,
    ) -> SherpaOnnxKokoroAdapter:
        """Resolve and reverify the model and both platform wheels before use."""

        artifacts = artifact_lock.resolve(profile_name, target)
        model = _component(artifacts, "tts_model")
        python_wheel = _component(artifacts, "sherpa_onnx_python")
        native_wheel = _component(artifacts, "sherpa_onnx_core")
        model_path = store.require(model)
        environment = store.materialize_python_wheels(
            [python_wheel, native_wheel],
            environment_name=(
                f"sherpa_onnx_{python_wheel.version}_"
                f"{target.operating_system}_{target.architecture}"
            ).replace(".", "_"),
        )
        return cls(
            model_directory=model_path,
            model_version=model.version,
            runtime_version=python_wheel.version,
            license_id=model.license_id,
            settings=settings,
            module_loader=module_loader or _loader_from_directory(environment),
        )

    def _load_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        try:
            sherpa = self.module_loader()
        except (ImportError, OSError) as error:
            raise FatalSynthesisError(
                "sherpa-onnx 1.13.4 is not installed in the active Python environment"
            ) from error
        installed_version = getattr(sherpa, "__version__", None)
        if installed_version != self.metadata.runtime_version:
            raise FatalSynthesisError(
                f"Expected sherpa-onnx {self.metadata.runtime_version}, "
                f"found {installed_version or 'an unknown version'}."
            )

        root = self.model_directory
        try:
            kokoro = sherpa.OfflineTtsKokoroModelConfig(
                model=str(root / "model.onnx"),
                voices=str(root / "voices.bin"),
                tokens=str(root / "tokens.txt"),
                lexicon=str(root / "lexicon-us-en.txt"),
                data_dir=str(root / "espeak-ng-data"),
                length_scale=1.0,
                lang=self.settings.language,
            )
            model = sherpa.OfflineTtsModelConfig(
                kokoro=kokoro,
                num_threads=self.settings.num_threads,
                provider=self.settings.provider,
            )
            config = sherpa.OfflineTtsConfig(model=model, max_num_sentences=1)
            config.validate()
            self._engine = sherpa.OfflineTts(config)
        except Exception as error:
            raise FatalSynthesisError(f"Could not initialize Kokoro: {error}") from error
        return self._engine

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        engine = self._load_engine()
        try:
            generated = engine.generate(
                segment.text,
                sid=self.settings.speaker_id,
                speed=self.settings.speed,
            )
        except Exception as error:
            raise TransientSynthesisError(f"Kokoro generation failed: {error}") from error

        generated_rate = getattr(generated, "sample_rate", None)
        samples = getattr(generated, "samples", None)
        if generated_rate != self.sample_rate:
            raise InvalidSynthesisOutput(
                f"Kokoro returned {generated_rate} Hz; Whoopy requires {self.sample_rate} Hz."
            )
        if samples is None:
            raise InvalidSynthesisOutput("Kokoro returned no sample collection")

        pcm = array("h")
        for raw_sample in samples:
            sample = float(raw_sample)
            if not math.isfinite(sample):
                raise InvalidSynthesisOutput("Kokoro returned a non-finite audio sample")
            clamped = max(-1.0, min(1.0, sample))
            pcm.append(round(clamped * 32_767.0))
        if not pcm:
            raise InvalidSynthesisOutput("Kokoro returned empty audio")
        if byteorder != "little":
            pcm.byteswap()
        return PcmAudio(pcm_s16le=pcm.tobytes(), sample_rate=self.sample_rate)

    def prepare(self) -> None:
        """Load and validate the in-process Kokoro engine before timing a render."""

        self._load_engine()

    @property
    def runtime_device(self) -> str:
        """Report the provider used by the in-process Kokoro engine."""

        return self.settings.provider

    def close(self) -> None:
        """Release the in-process Kokoro engine so its memory can be reclaimed."""

        self._engine = None
