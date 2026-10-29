"""Isolated adapters for the Apache-2.0 Qwen3-TTS 12 Hz family."""

from __future__ import annotations

import base64
import hashlib
import json
import os
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
from whoopy.locations import default_config_directory
from whoopy.model_packs.operations import (
    HeavyweightModelSlot,
    HeavyweightModelSlotUnavailable,
    models_root_for_runtime,
)
from whoopy.model_packs.registry import ModelPackState, load_model_pack_registry
from whoopy.model_packs.runtime import RuntimeInstallReceipt
from whoopy.ports import AdapterMetadata, SpeechCapabilities
from whoopy.timeline import SpeechSegment, VoiceMode

Qwen3TTSVariant = Literal[
    "custom-voice-0.6b",
    "custom-voice-1.7b",
    "voice-design-1.7b",
    "base-clone-0.6b",
    "base-clone-1.7b",
]
Qwen3TTSTask = Literal["custom_voice", "voice_design", "voice_clone"]
Qwen3VoiceDesignPreset = Literal[
    "warm_meditation",
    "grounded_meditation",
    "sleep_meditation",
]
QWEN3_TTS_LANGUAGES = (
    "Auto",
    "Chinese",
    "English",
    "Japanese",
    "Korean",
    "German",
    "French",
    "Russian",
    "Portuguese",
    "Spanish",
    "Italian",
)
QWEN3_TTS_SPEAKERS = (
    "Vivian",
    "Serena",
    "Uncle_Fu",
    "Dylan",
    "Eric",
    "Ryan",
    "Aiden",
    "Ono_Anna",
    "Sohee",
)
_VARIANTS: dict[Qwen3TTSVariant, tuple[Qwen3TTSTask, str]] = {
    "custom-voice-0.6b": ("custom_voice", "Qwen3-TTS-12Hz-0.6B-CustomVoice"),
    "custom-voice-1.7b": ("custom_voice", "Qwen3-TTS-12Hz-1.7B-CustomVoice"),
    "voice-design-1.7b": ("voice_design", "Qwen3-TTS-12Hz-1.7B-VoiceDesign"),
    "base-clone-0.6b": ("voice_clone", "Qwen3-TTS-12Hz-0.6B-Base"),
    "base-clone-1.7b": ("voice_clone", "Qwen3-TTS-12Hz-1.7B-Base"),
}
_DELIVERY_INSTRUCTIONS = {
    "neutral": "Use a natural, steady delivery.",
    "warm": "Use a warm and welcoming delivery.",
    "calm": "Speak in a calm, settled tone.",
    "soothing": "Use a soft, soothing tone without whispering.",
    "grounded": "Sound grounded, steady, and present.",
    "compassionate": "Use a gentle, compassionate tone.",
    "sleepy": "Use a very soft, sleepy delivery that tapers gently.",
    "encouraging": "Sound quietly encouraging without becoming energetic.",
}
_PACE_INSTRUCTIONS = {
    "natural": "",
    "unhurried": "Keep an unhurried pace.",
    "slow": "Speak slowly with natural phrasing.",
    "very_slow": "Speak very slowly while keeping phrases connected.",
}
_CONSENT_SCOPE = "local_voice_cloning_experiment_only"
_VOICE_DESIGN_PRESETS: dict[Qwen3VoiceDesignPreset, str] = {
    "warm_meditation": (
        "A warm, calm adult narrator with a natural low-energy meditation delivery."
    ),
    "grounded_meditation": (
        "A grounded adult narrator with a steady, reassuring lower register and clear diction."
    ),
    "sleep_meditation": (
        "A soft adult sleep narrator with low energy, gentle phrasing, and no whispering."
    ),
}


def _checkpoint_is_complete(directory: Path) -> bool:
    single = directory / "model.safetensors"
    if single.is_file() and single.stat().st_size > 0:
        return True
    index_path = directory / "model.safetensors.index.json"
    if not index_path.is_file():
        return False
    try:
        document = json.loads(index_path.read_text(encoding="utf-8"))
        shards = set(document["weight_map"].values())
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return bool(shards) and all(
        (directory / shard).is_file() and (directory / shard).stat().st_size > 0 for shard in shards
    )


def _runtime_python(runtime_directory: Path) -> Path:
    windows = runtime_directory / ".venv" / "Scripts" / "python.exe"
    return (
        windows if os.name == "nt" or windows.is_file() else runtime_directory / ".venv/bin/python"
    )


@dataclass(frozen=True)
class Qwen3TTSSettings:
    """Pinned paths and reviewed controls for one isolated Qwen runtime."""

    runtime_directory: Path
    worker_script: Path
    model_directory: Path
    variant: Qwen3TTSVariant
    model_revision: str
    runtime_revision: str
    speaker: str = "Serena"
    language: str = "English"
    voice_design_preset: Qwen3VoiceDesignPreset = "warm_meditation"
    reference_audio: Path | None = None
    reference_text: Path | None = None
    consent_scope: Literal["local_voice_cloning_experiment_only"] | None = None
    seed: int = 42
    startup_timeout_seconds: float = 600
    request_timeout_seconds: float = 300
    shutdown_timeout_seconds: float = 5
    models_root: Path | None = None

    def __post_init__(self) -> None:
        task, _ = _VARIANTS[self.variant]
        if not self.model_revision.strip() or not self.runtime_revision.strip():
            raise ValueError("model and runtime revisions must be pinned")
        for name, value in (
            ("startup", self.startup_timeout_seconds),
            ("request", self.request_timeout_seconds),
            ("shutdown", self.shutdown_timeout_seconds),
        ):
            if value <= 0:
                raise ValueError(f"{name} timeout must be positive")
        if self.language not in QWEN3_TTS_LANGUAGES:
            raise ValueError(f"unsupported Qwen3-TTS language: {self.language}")
        if self.voice_design_preset not in _VOICE_DESIGN_PRESETS:
            raise ValueError(
                f"unsupported reviewed voice design preset: {self.voice_design_preset}"
            )
        if task == "custom_voice" and self.speaker not in QWEN3_TTS_SPEAKERS:
            raise ValueError(f"unsupported Qwen3-TTS preset speaker: {self.speaker}")
        if task == "voice_clone":
            if self.reference_audio is None or self.reference_text is None:
                raise ValueError("voice cloning requires reference audio and its transcript")
            if self.consent_scope != _CONSENT_SCOPE:
                raise ValueError("voice cloning requires explicit local experiment consent")
        elif any(
            item is not None
            for item in (self.reference_audio, self.reference_text, self.consent_scope)
        ):
            raise ValueError("voice references are accepted only by Qwen Base cloning models")


@dataclass(frozen=True)
class Qwen3TTSInstallation:
    """A locally declared Lab installation; this is not model-pack readiness evidence."""

    runtime_directory: Path
    model_directory: Path
    runtime_revision: str
    model_revision: str


def resolve_qwen3_tts_installation(
    models_root: Path,
    variant: Qwen3TTSVariant,
    *,
    registry_path: Path | None = None,
) -> Qwen3TTSInstallation:
    """Resolve only a registry-verified, current-machine READY Qwen installation."""

    pack_id = f"qwen3-tts-{variant}"
    active_registry = registry_path or default_config_directory() / "model_packs.yaml"
    try:
        registry = load_model_pack_registry(active_registry, models_root=models_root)
        pack = registry.get(pack_id)
        status = registry.inspect(pack_id)
    except (OSError, ValueError) as error:
        raise FatalSynthesisError(
            f"Qwen3-TTS {variant} has no reviewed model-pack integration: {error}"
        ) from error
    if status.state is not ModelPackState.READY:
        failed = next((check.message for check in status.checks if not check.passed), None)
        remedy = failed or "run the verified offline production smoke test"
        raise FatalSynthesisError(
            f"Qwen3-TTS {variant} is {status.state}; {remedy}. "
            f"Run `whoopy models pack list` and smoke-test {pack_id} first."
        )
    runtime = registry.runtime_directory(pack)
    if runtime is None:
        raise FatalSynthesisError(
            f"Qwen3-TTS {variant} has no complete isolated runtime for revision "
            f"{pack.runtime.revision}."
        )
    model = status.selected_directory
    runtime_manifest = runtime / ".whoopy-runtime.json"
    try:
        runtime_receipt = RuntimeInstallReceipt.model_validate_json(
            runtime_manifest.read_text(encoding="utf-8")
        )
        if runtime_receipt.runtime_id != "qwen3-tts":
            raise ValueError("runtime receipt does not identify qwen3-tts")
        runtime_revision = runtime_receipt.revision.strip()
        if runtime_revision != pack.runtime.revision:
            raise ValueError("runtime receipt revision does not match the reviewed model pack")
    except (OSError, ValueError) as error:
        raise FatalSynthesisError(f"Invalid Qwen3-TTS runtime receipt: {error}") from error
    return Qwen3TTSInstallation(
        runtime_directory=runtime,
        model_directory=model,
        runtime_revision=runtime_revision,
        model_revision=pack.revision,
    )


class Qwen3TTSAdapter:
    """Translate typed Whoopy delivery intent into Qwen's three task APIs."""

    sample_rate: int = SAMPLE_RATE

    def __init__(self, settings: Qwen3TTSSettings) -> None:
        self.settings = settings
        self.task, official_name = _VARIANTS[settings.variant]
        self._controller: JsonLineProcessController | None = None
        self._runtime_device: str | None = None
        self._diagnostics = BoundedDiagnostics()
        pack_id = f"qwen3-tts-{settings.variant}"
        self._runtime_slot = HeavyweightModelSlot(
            settings.models_root or models_root_for_runtime(settings.runtime_directory), pack_id
        )
        if self.task == "voice_clone":
            assert settings.reference_audio is not None
            assert settings.reference_text is not None
            reference_hash = hashlib.sha256()
            for path in (settings.reference_audio, settings.reference_text):
                reference_hash.update(path.read_bytes() if path.is_file() else str(path).encode())
            reference_identity = reference_hash.hexdigest()
            voice_mode: VoiceMode = "reference"
        elif self.task == "voice_design":
            reference_identity = "none"
            voice_mode = "designed"
        else:
            reference_identity = "none"
            voice_mode = "preset"
        expressive = self.task != "voice_clone" and settings.variant != "custom-voice-0.6b"
        self.capabilities = SpeechCapabilities(
            voice_modes=(voice_mode,),
            delivery_styles=(
                (
                    "neutral",
                    "warm",
                    "calm",
                    "soothing",
                    "grounded",
                    "compassionate",
                    "sleepy",
                    "encouraging",
                )
                if expressive
                else ("neutral",)
            ),
            intensity_levels=(("subtle", "moderate") if expressive else ("subtle",)),
            pace_intents=(
                ("natural", "unhurried", "slow", "very_slow") if expressive else ("natural",)
            ),
            languages=QWEN3_TTS_LANGUAGES,
            supports_freeform_instruction=expressive,
            supports_pronunciation_overrides=False,
        )
        self.metadata = AdapterMetadata(
            adapter_id=f"whoopy.qwen3_tts.{settings.variant}",
            versioned_model_id=f"Qwen/{official_name}@{settings.model_revision}",
            runtime_id="qwen-tts-isolated-python",
            runtime_version=settings.runtime_revision,
            license_id="Apache-2.0",
            device="cuda-or-mps-or-cpu",
            settings=(
                f"task={self.task}",
                f"speaker={settings.speaker if self.task == 'custom_voice' else 'none'}",
                f"language={settings.language}",
                "voice_design_profile="
                + (settings.voice_design_preset if self.task == "voice_design" else "none"),
                f"reference_sha256={reference_identity}",
                f"consent_scope={settings.consent_scope or 'none'}",
                f"seed={settings.seed}",
                f"sample_rate={self.sample_rate}",
                "generation_policy=sampled_then_greedy_on_invalid_probabilities@1",
                "max_new_tokens_policy=text_units_x12_plus96_clamped_192_2048@1",
                "instruction_support="
                + ("none" if settings.variant == "custom-voice-0.6b" else "reviewed"),
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    @staticmethod
    def availability_error(settings: Qwen3TTSSettings) -> str | None:
        missing: list[str] = []
        if not _runtime_python(settings.runtime_directory).is_file():
            missing.append("isolated Python runtime")
        if not settings.worker_script.is_file():
            missing.append("Qwen3-TTS worker")
        if not (settings.model_directory / "config.json").is_file() or not _checkpoint_is_complete(
            settings.model_directory
        ):
            missing.append("model checkpoint")
        if _VARIANTS[settings.variant][0] == "voice_clone":
            if settings.reference_audio is None or not settings.reference_audio.is_file():
                missing.append("consented reference audio")
            if settings.reference_text is None or not settings.reference_text.is_file():
                missing.append("reference transcript")
        return None if not missing else f"missing {', '.join(missing)}"

    def translate_segment(self, segment: SpeechSegment) -> dict[str, object]:
        """Return the inspectable worker request without adding controls to text."""

        required_mode = {
            "custom_voice": "preset",
            "voice_design": "designed",
            "voice_clone": "reference",
        }[self.task]
        if segment.voice_mode != required_mode:
            raise FatalSynthesisError(
                f"{self.settings.variant} requires voice mode {required_mode}, "
                f"not {segment.voice_mode}"
            )
        if segment.language not in self.capabilities.languages:
            raise FatalSynthesisError(f"Qwen3-TTS does not support language {segment.language}")
        if segment.pronunciation_overrides:
            raise FatalSynthesisError("Qwen3-TTS does not support pronunciation overrides")
        if segment.delivery not in self.capabilities.delivery_styles:
            raise FatalSynthesisError(
                f"{self.settings.variant} does not support delivery {segment.delivery}"
            )
        if segment.pace not in self.capabilities.pace_intents:
            raise FatalSynthesisError(
                f"{self.settings.variant} does not support pace {segment.pace}"
            )
        language = segment.language if segment.language != "English" else self.settings.language
        request: dict[str, object] = {
            "task": self.task,
            "text": segment.text,
            "language": language,
            "seed": self.settings.seed,
        }
        if self.task == "custom_voice":
            request["speaker"] = self.settings.speaker
        if self.task != "voice_clone" and self.settings.variant != "custom-voice-0.6b":
            instruction = " ".join(
                part
                for part in (
                    _VOICE_DESIGN_PRESETS[self.settings.voice_design_preset]
                    if self.task == "voice_design"
                    else "",
                    _DELIVERY_INSTRUCTIONS[segment.delivery],
                    "Keep the expression subtle."
                    if segment.intensity == "subtle"
                    else "Use clearly expressive but natural emphasis.",
                    _PACE_INSTRUCTIONS[segment.pace],
                )
                if part
            )
            request["instruction"] = instruction
        return request

    def _start(self) -> JsonLineProcessController:
        if self._controller is not None and self._controller.running:
            return self._controller
        if self._controller is not None:
            self._drop_controller()
        error = self.availability_error(self.settings)
        if error is not None:
            raise FatalSynthesisError(f"{self.settings.variant} is not ready: {error}.")
        command = [
            str(_runtime_python(self.settings.runtime_directory)),
            str(self.settings.worker_script),
            "--model",
            str(self.settings.model_directory),
            "--task",
            self.task,
        ]
        if self.task == "voice_clone":
            assert self.settings.reference_audio is not None
            assert self.settings.reference_text is not None
            command.extend(
                [
                    "--reference-audio",
                    str(self.settings.reference_audio),
                    "--reference-text",
                    str(self.settings.reference_text),
                ]
            )
        controller = JsonLineProcessController(
            command=command,
            label="Qwen3-TTS",
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
            raise TransientSynthesisError(f"Qwen3-TTS startup failed: {error}") from error
        except WorkerProcessError as error:
            self._drop_controller()
            raise FatalSynthesisError(f"Could not start Qwen3-TTS: {error}") from error
        if ready.get("status") != "ready" or ready.get("sample_rate") != self.sample_rate:
            self._drop_controller()
            raise FatalSynthesisError(f"Qwen3-TTS did not become ready: {ready}")
        self._runtime_device = str(ready.get("device") or "unknown")
        return controller

    def prepare(self) -> None:
        self._start()

    @property
    def runtime_process_id(self) -> int | None:
        return self._controller.process_id if self._controller is not None else None

    @property
    def runtime_device(self) -> str | None:
        return self._runtime_device

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        controller = self._start()
        try:
            response = controller.request(self.translate_segment(segment))
        except WorkerProcessError as error:
            self._drop_controller()
            raise TransientSynthesisError(f"Qwen3-TTS process failed: {error}") from error
        if response.get("status") != "ok":
            self._drop_controller()
            raise TransientSynthesisError(
                f"Qwen3-TTS generation failed: {response.get('error', 'unknown error')}"
            )
        generation = response.get("generation")
        if isinstance(generation, dict):
            self._diagnostics.append(
                "generation",
                json.dumps(generation, sort_keys=True, separators=(",", ":")),
            )
        try:
            encoded_pcm = response["pcm_s16le"]
            if not isinstance(encoded_pcm, (str, bytes)):
                raise TypeError("pcm_s16le must be text or bytes")
            pcm = base64.b64decode(encoded_pcm, validate=True)
        except (KeyError, TypeError, ValueError) as error:
            self._drop_controller()
            raise InvalidSynthesisOutput("Qwen3-TTS returned invalid PCM data") from error
        if not pcm:
            self._drop_controller()
            raise InvalidSynthesisOutput("Qwen3-TTS returned empty audio")
        return PcmAudio(pcm_s16le=pcm, sample_rate=self.sample_rate)

    def diagnostics(self) -> tuple[str, ...]:
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
