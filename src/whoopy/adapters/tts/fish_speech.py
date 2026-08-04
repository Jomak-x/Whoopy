"""Optional non-commercial Fish Speech 1.4 adapter for local Mac experiments."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path

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


@dataclass(frozen=True)
class FishSpeechSettings:
    """Paths and reproducibility controls for the optional Fish installation."""

    runtime_directory: Path
    worker_script: Path
    reference_audio: Path
    reference_text: Path
    checkpoint_directory: Path | None = None
    seed: int = 42
    startup_timeout_seconds: float = 600
    request_timeout_seconds: float = 300
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


class FishSpeech14Adapter:
    """Use Fish 1.4 behind Whoopy's ordinary speech-synthesizer contract."""

    sample_rate: int = SAMPLE_RATE

    def __init__(self, settings: FishSpeechSettings) -> None:
        self.settings = settings
        self._controller: JsonLineProcessController | None = None
        self._runtime_device: str | None = None
        self._diagnostics = BoundedDiagnostics()
        self._runtime_slot = HeavyweightModelSlot(
            settings.models_root or models_root_for_runtime(settings.runtime_directory),
            "fish-speech-1.4",
        )
        reference_hash = hashlib.sha256()
        for path in (settings.reference_audio, settings.reference_text):
            if path.is_file():
                reference_hash.update(path.read_bytes())
            else:
                reference_hash.update(str(path).encode())
        self.metadata = AdapterMetadata(
            adapter_id="whoopy.fish_speech_1_4",
            versioned_model_id="Fish-Speech@1.4",
            runtime_id="fish-speech-isolated-python",
            runtime_version="1.4",
            license_id="CC-BY-NC-SA-4.0",
            device="mps",
            settings=(
                f"reference_audio={settings.reference_audio.name}",
                f"reference_text={settings.reference_text.name}",
                f"reference_sha256={reference_hash.hexdigest()}",
                f"seed={settings.seed}",
                f"sample_rate={self.sample_rate}",
                "expression_control=reference_audio",
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    @staticmethod
    def availability_error(
        runtime_directory: Path,
        checkpoint_directory: Path | None = None,
        reference_audio: Path | None = None,
        reference_text: Path | None = None,
    ) -> str | None:
        """Return a human-readable missing component instead of loading a model."""

        checkpoint = checkpoint_directory or (runtime_directory / "checkpoints" / "fish-speech-1.4")
        selected_audio = reference_audio or (runtime_directory / "whoopy-reference.wav")
        selected_text = reference_text or (runtime_directory / "whoopy-reference.txt")
        required = (
            runtime_directory / ".venv" / "bin" / "python",
            checkpoint / "model.pth",
            checkpoint / "firefly-gan-vq-fsq-8x1024-21hz-generator.pth",
            selected_audio,
            selected_text,
        )
        missing = [path.name for path in required if not path.is_file()]
        return None if not missing else f"missing {', '.join(missing)}"

    def _start(self) -> JsonLineProcessController:
        if self._controller is not None and self._controller.running:
            return self._controller
        if self._controller is not None:
            # A worker can die between requests. Closing only the controller
            # would strand this adapter's heavyweight slot until destruction.
            self._drop_controller()
        runtime = self.settings.runtime_directory
        checkpoint = self.settings.checkpoint_directory or (
            runtime / "checkpoints" / "fish-speech-1.4"
        )
        error = self.availability_error(
            runtime,
            checkpoint,
            self.settings.reference_audio,
            self.settings.reference_text,
        )
        if error is not None:
            raise FatalSynthesisError(f"Fish Speech 1.4 is not ready: {error}.")
        command = [
            str(runtime / ".venv" / "bin" / "python"),
            str(self.settings.worker_script),
            "--runtime",
            str(runtime),
            "--checkpoint",
            str(checkpoint),
            "--reference-audio",
            str(self.settings.reference_audio),
            "--reference-text",
            str(self.settings.reference_text),
        ]
        controller = JsonLineProcessController(
            command=command,
            label="Fish Speech 1.4",
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
            raise TransientSynthesisError(f"Fish Speech startup failed: {error}") from error
        except WorkerProcessError as error:
            self._drop_controller()
            raise FatalSynthesisError(f"Could not start Fish Speech 1.4: {error}") from error
        if ready.get("status") != "ready" or ready.get("sample_rate") != self.sample_rate:
            self._drop_controller()
            raise FatalSynthesisError(f"Fish Speech 1.4 did not become ready: {ready}")
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
                }
            )
        except WorkerProcessError as error:
            self._drop_controller()
            raise TransientSynthesisError(f"Fish Speech process failed: {error}") from error
        if response.get("status") != "ok":
            self._drop_controller()
            raise TransientSynthesisError(
                f"Fish Speech generation failed: {response.get('error', 'unknown error')}"
            )
        try:
            encoded_pcm = response["pcm_s16le"]
            if not isinstance(encoded_pcm, (str, bytes)):
                raise TypeError("pcm_s16le must be text or bytes")
            pcm = base64.b64decode(encoded_pcm, validate=True)
        except (KeyError, TypeError, ValueError) as error:
            self._drop_controller()
            raise InvalidSynthesisOutput("Fish Speech returned invalid PCM data") from error
        if not pcm:
            self._drop_controller()
            raise InvalidSynthesisOutput("Fish Speech returned empty audio")
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
