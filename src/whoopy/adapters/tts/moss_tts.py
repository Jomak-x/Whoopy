"""Optional Apache-2.0 MOSS-TTS v1.5 adapters for capable local Macs."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from whoopy.adapters.tts._json_process import (
    BoundedDiagnostics,
    JsonLineProcessController,
    WorkerProcessError,
    WorkerTimeoutError,
)
from whoopy.audio.models import SAMPLE_RATE, PcmAudio
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    InvalidSynthesisOutput,
    TransientSynthesisError,
)
from whoopy.model_packs.operations import (
    HeavyweightModelSlot,
    HeavyweightModelSlotUnavailable,
    models_root_for_runtime,
)
from whoopy.ports import AdapterMetadata
from whoopy.timeline import SpeechSegment

MossVariant = Literal["moss-local-v1.5", "moss-v1.5"]
MOSS_LANGUAGES = (
    "Arabic",
    "Cantonese",
    "Chinese",
    "Czech",
    "Danish",
    "Dutch",
    "English",
    "Finnish",
    "French",
    "German",
    "Greek",
    "Hebrew",
    "Hindi",
    "Hungarian",
    "Italian",
    "Japanese",
    "Korean",
    "Macedonian",
    "Malay",
    "Persian (Farsi)",
    "Polish",
    "Portuguese",
    "Romanian",
    "Russian",
    "Spanish",
    "Swahili",
    "Swedish",
    "Tagalog",
    "Thai",
    "Turkish",
    "Vietnamese",
)


def _checkpoint_is_complete(directory: Path) -> bool:
    """Accept a complete single-file or sharded safetensors checkpoint only."""
    single_file = directory / "model.safetensors"
    if single_file.is_file() and single_file.stat().st_size > 0:
        return True

    index_file = directory / "model.safetensors.index.json"
    if not index_file.is_file():
        return False
    try:
        index = json.loads(index_file.read_text(encoding="utf-8"))
        shards = set(index["weight_map"].values())
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(shards) and all(
        (directory / shard).is_file() and (directory / shard).stat().st_size > 0 for shard in shards
    )


@dataclass(frozen=True)
class MossTTSSettings:
    runtime_directory: Path
    worker_script: Path
    model_directory: Path
    codec_directory: Path
    variant: MossVariant
    reference_audio: Path
    language: str = "English"
    instruction: str = "Speak slowly, softly, and warmly, with a meditative delivery."
    use_reference: bool = True
    seed: int = 42
    startup_timeout_seconds: float = 900
    request_timeout_seconds: float = 600
    shutdown_timeout_seconds: float = 5
    models_root: Path | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("startup", self.startup_timeout_seconds),
            ("request", self.request_timeout_seconds),
            ("shutdown", self.shutdown_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} timeout must be positive")


class MossTTSAdapter:
    """Map either MOSS v1.5 architecture to Whoopy's PCM contract."""

    sample_rate: int = SAMPLE_RATE

    def __init__(self, settings: MossTTSSettings) -> None:
        self.settings = settings
        self._controller: JsonLineProcessController | None = None
        self._runtime_device: str | None = None
        self._diagnostics = BoundedDiagnostics()
        pack_id = "moss-local-5b" if settings.variant == "moss-local-v1.5" else "moss-8b"
        self._runtime_slot = HeavyweightModelSlot(
            settings.models_root or models_root_for_runtime(settings.runtime_directory),
            pack_id,
        )
        reference_hash = (
            hashlib.sha256(settings.reference_audio.read_bytes()).hexdigest()
            if settings.reference_audio.is_file()
            else "missing"
        )
        model_id = (
            "MOSS-TTS-Local-Transformer@1.5"
            if settings.variant == "moss-local-v1.5"
            else "MOSS-TTS@1.5"
        )
        self.metadata = AdapterMetadata(
            adapter_id=f"whoopy.{settings.variant}",
            versioned_model_id=model_id,
            runtime_id="transformers-isolated-python",
            runtime_version="5.0.0",
            license_id="Apache-2.0",
            device="mps-or-cpu",
            settings=(
                f"language={settings.language}",
                f"instruction={settings.instruction}",
                f"use_reference={settings.use_reference}",
                f"reference_sha256={reference_hash}",
                f"seed={settings.seed}",
                f"sample_rate={self.sample_rate}",
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    @staticmethod
    def availability_error(
        runtime_directory: Path,
        model_directory: Path,
        codec_directory: Path,
        reference_audio: Path,
    ) -> str | None:
        python = runtime_directory / ".venv" / "bin" / "python"
        missing: list[str] = []
        if not python.is_file():
            missing.append("isolated Python runtime")
        if not (model_directory / "config.json").is_file() or not _checkpoint_is_complete(
            model_directory
        ):
            missing.append("model checkpoint")
        if not (codec_directory / "config.json").is_file() or not _checkpoint_is_complete(
            codec_directory
        ):
            missing.append("audio tokenizer")
        if not reference_audio.is_file():
            missing.append("reference voice")
        return None if not missing else f"missing {', '.join(missing)}"

    def _start(self) -> JsonLineProcessController:
        if self._controller is not None and self._controller.running:
            return self._controller
        if self._controller is not None:
            # A worker can die between requests. Closing only the controller
            # would strand this adapter's heavyweight slot until destruction.
            self._drop_controller()
        error = self.availability_error(
            self.settings.runtime_directory,
            self.settings.model_directory,
            self.settings.codec_directory,
            self.settings.reference_audio,
        )
        if error is not None:
            raise FatalSynthesisError(f"{self.settings.variant} is not ready: {error}.")
        command = [
            str(self.settings.runtime_directory / ".venv" / "bin" / "python"),
            str(self.settings.worker_script),
            "--model",
            str(self.settings.model_directory),
            "--codec",
            str(self.settings.codec_directory),
            "--reference-audio",
            str(self.settings.reference_audio),
        ]
        controller = JsonLineProcessController(
            command=command,
            label="MOSS-TTS",
            startup_timeout_seconds=self.settings.startup_timeout_seconds,
            request_timeout_seconds=self.settings.request_timeout_seconds,
            shutdown_timeout_seconds=self.settings.shutdown_timeout_seconds,
            diagnostics=self._diagnostics,
        )
        self._controller = controller
        try:
            self._runtime_slot.acquire()
            ready = controller.start()
        except HeavyweightModelSlotUnavailable as error:
            self._drop_controller()
            raise TransientSynthesisError(str(error)) from error
        except WorkerTimeoutError as error:
            self._drop_controller()
            raise TransientSynthesisError(f"MOSS-TTS startup failed: {error}") from error
        except WorkerProcessError as error:
            self._drop_controller()
            raise FatalSynthesisError(f"Could not start MOSS-TTS: {error}") from error
        if ready.get("status") != "ready" or ready.get("sample_rate") != self.sample_rate:
            self._drop_controller()
            raise FatalSynthesisError(f"MOSS-TTS did not become ready: {ready}")
        self._runtime_device = str(ready.get("device") or "unknown")
        return controller

    def prepare(self) -> None:
        """Load and validate the isolated runtime without rendering audio yet."""

        self._start()

    @property
    def runtime_process_id(self) -> int | None:
        """Expose only the owned worker PID for resource measurement."""

        return self._controller.process_id if self._controller is not None else None

    @property
    def runtime_device(self) -> str | None:
        """Report the accelerator selected by the isolated runtime."""

        return self._runtime_device

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        controller = self._start()
        try:
            response = controller.request(
                {
                    "text": segment.text,
                    "seed": self.settings.seed,
                    "language": self.settings.language,
                    "instruction": self.settings.instruction,
                    "use_reference": self.settings.use_reference,
                }
            )
        except WorkerProcessError as error:
            self._drop_controller()
            raise TransientSynthesisError(f"MOSS-TTS process failed: {error}") from error
        if response.get("status") != "ok":
            self._drop_controller()
            raise TransientSynthesisError(
                f"MOSS-TTS generation failed: {response.get('error', 'unknown error')}"
            )
        try:
            encoded_pcm = response["pcm_s16le"]
            if not isinstance(encoded_pcm, (str, bytes)):
                raise TypeError("pcm_s16le must be text or bytes")
            pcm = base64.b64decode(encoded_pcm, validate=True)
        except (KeyError, TypeError, ValueError) as error:
            self._drop_controller()
            raise InvalidSynthesisOutput("MOSS-TTS returned invalid PCM data") from error
        if not pcm:
            self._drop_controller()
            raise InvalidSynthesisOutput("MOSS-TTS returned empty audio")
        return PcmAudio(pcm_s16le=pcm, sample_rate=self.sample_rate)

    def diagnostics(self) -> tuple[str, ...]:
        """Return bounded worker diagnostics retained across process restarts."""

        return self._diagnostics.snapshot()

    def _drop_controller(self) -> None:
        controller, self._controller = self._controller, None
        try:
            if controller is not None:
                controller.close()
        finally:
            self._runtime_slot.release()

    def close(self) -> None:
        self._drop_controller()

    def __del__(self) -> None:
        self.close()
