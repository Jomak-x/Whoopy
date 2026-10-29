"""Optional Apache-2.0 MOSS-TTS v1.5 adapters for capable local Macs."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import unicodedata
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
from whoopy.hardware import inspect_hardware
from whoopy.model_packs import HardwareRequirement, hardware_requirement_error
from whoopy.model_packs.operations import (
    HeavyweightModelSlot,
    HeavyweightModelSlotUnavailable,
    models_root_for_runtime,
)
from whoopy.ports import AdapterMetadata, SpeechCapabilities
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
DELIVERY_INSTRUCTIONS = {
    "neutral": "Use a natural, steady delivery.",
    "warm": "Sound warm and welcoming.",
    "calm": "Use a calm, settled tone.",
    "soothing": "Use a soft, soothing tone without whispering.",
    "grounded": "Sound grounded, steady, and present.",
    "compassionate": "Use a gentle, compassionate tone.",
    "sleepy": "Use a very soft, sleepy delivery that tapers gently.",
    "encouraging": "Sound quietly encouraging without becoming energetic.",
}
PACE_INSTRUCTIONS = {
    "natural": "",
    "unhurried": "Keep an unhurried pace.",
    "slow": "Speak slowly with natural phrasing.",
    "very_slow": "Speak very slowly while keeping phrases connected.",
}
MOSS_DURATION_BUDGET_STRATEGY = "moss-text-pace-v1"
MOSS_GENERATION_CAP_STRATEGY = "duration-margin-v1"
MOSS_AUDIO_TOKENS_PER_SECOND = 12.5
MOSS_DEFAULT_TOKENS_PER_CHARACTER = 0.8673376262755219
MOSS_CJK_TOKENS_PER_CHARACTER = 3.098411951313033
MOSS_PACE_DURATION_MULTIPLIERS = {
    "natural": 1.15,
    "unhurried": 1.30,
    "slow": 1.50,
    "very_slow": 1.80,
}
MOSS_MIN_FALLBACK_DURATION_TOKENS = 19
MOSS_MAX_DURATION_TOKENS = 1_500
MOSS_MIN_GENERATION_TOKENS = 96
MOSS_MAX_GENERATION_TOKENS = 1_536


@dataclass(frozen=True)
class MossDurationBudget:
    """One reproducible MOSS duration request and hard generation ceiling."""

    duration_tokens: int
    max_new_tokens: int
    source: Literal["explicit_target", "text_pace_fallback"]


def _contains_cjk_script(text: str) -> bool:
    return any(
        "\u3400" <= character <= "\u4dbf"
        or "\u4e00" <= character <= "\u9fff"
        or "\u3040" <= character <= "\u30ff"
        or "\uac00" <= character <= "\ud7af"
        for character in text
    )


def moss_duration_budget(segment: SpeechSegment) -> MossDurationBudget:
    """Derive an exact duration hint and hard cap for every MOSS request.

    Explicit timeline duration always wins. Otherwise the reviewed v1 policy
    estimates MOSS audio tokens from normalized visible characters and expands
    the estimate using the typed adapter-neutral pace intent. The generation
    ceiling leaves bounded protocol/EOS headroom but can never return to the
    historical unbounded 2,048-token default.
    """

    if segment.target_duration_ms is not None:
        duration_tokens = min(
            MOSS_MAX_DURATION_TOKENS,
            max(
                1,
                math.ceil(segment.target_duration_ms * MOSS_AUDIO_TOKENS_PER_SECOND / 1_000),
            ),
        )
        source: Literal["explicit_target", "text_pace_fallback"] = "explicit_target"
    else:
        normalized = unicodedata.normalize("NFKC", segment.text)
        character_count = max(1, sum(not character.isspace() for character in normalized))
        character_factor = (
            MOSS_CJK_TOKENS_PER_CHARACTER
            if _contains_cjk_script(normalized)
            else MOSS_DEFAULT_TOKENS_PER_CHARACTER
        )
        duration_tokens = min(
            MOSS_MAX_DURATION_TOKENS,
            max(
                MOSS_MIN_FALLBACK_DURATION_TOKENS,
                math.ceil(
                    character_count
                    * character_factor
                    * MOSS_PACE_DURATION_MULTIPLIERS[segment.pace]
                ),
            ),
        )
        source = "text_pace_fallback"
    max_new_tokens = min(
        MOSS_MAX_GENERATION_TOKENS,
        max(MOSS_MIN_GENERATION_TOKENS, math.ceil(duration_tokens * 1.25) + 64),
    )
    return MossDurationBudget(
        duration_tokens=duration_tokens,
        max_new_tokens=max_new_tokens,
        source=source,
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
    reference_audio: Path | None
    language: str = "English"
    instruction: str = "Speak slowly, softly, and warmly, with a meditative delivery."
    use_reference: bool = True
    seed: int = 42
    startup_timeout_seconds: float = 900
    request_timeout_seconds: float = 600
    shutdown_timeout_seconds: float = 5
    models_root: Path | None = None
    hardware_requirement: HardwareRequirement | None = None

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
    capabilities = SpeechCapabilities(
        voice_modes=("reference", "designed"),
        delivery_styles=(
            "neutral",
            "warm",
            "calm",
            "soothing",
            "grounded",
            "compassionate",
            "sleepy",
            "encouraging",
        ),
        intensity_levels=("subtle", "moderate"),
        pace_intents=("natural", "unhurried", "slow", "very_slow"),
        languages=MOSS_LANGUAGES,
        supports_freeform_instruction=True,
        supports_pronunciation_overrides=True,
        supports_duration_control=True,
    )

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
        reference_hash = "none"
        if settings.use_reference:
            reference_hash = (
                hashlib.sha256(settings.reference_audio.read_bytes()).hexdigest()
                if settings.reference_audio is not None and settings.reference_audio.is_file()
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
                f"duration_budget_strategy={MOSS_DURATION_BUDGET_STRATEGY}",
                f"generation_cap_strategy={MOSS_GENERATION_CAP_STRATEGY}",
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    @staticmethod
    def availability_error(
        runtime_directory: Path,
        model_directory: Path,
        codec_directory: Path,
        reference_audio: Path | None,
        *,
        use_reference: bool = True,
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
        if use_reference and (reference_audio is None or not reference_audio.is_file()):
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
            use_reference=self.settings.use_reference,
        )
        if error is not None:
            raise FatalSynthesisError(f"{self.settings.variant} is not ready: {error}.")
        if self.settings.hardware_requirement is not None:
            live_error = hardware_requirement_error(
                self.settings.hardware_requirement,
                inspect_hardware(self.settings.runtime_directory),
            )
            if live_error is not None:
                raise TransientSynthesisError(
                    f"{self.settings.variant} live resource check failed immediately before "
                    f"load: {live_error}. Close other applications or use Kokoro."
                )
        command = [
            str(self.settings.runtime_directory / ".venv" / "bin" / "python"),
            str(self.settings.worker_script),
            "--model",
            str(self.settings.model_directory),
            "--codec",
            str(self.settings.codec_directory),
        ]
        if self.settings.use_reference:
            assert self.settings.reference_audio is not None
            command.extend(["--reference-audio", str(self.settings.reference_audio)])
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
        duration_budget = moss_duration_budget(segment)
        pronunciation_instruction = ""
        if segment.pronunciation_overrides:
            pronunciation_instruction = (
                "Apply these exact pronunciation overrides. Each source is present in the "
                "spoken text; the replacement is intended Pinyin, IPA, or a phonetic "
                "rendering: "
                + json.dumps(
                    [
                        override.model_dump(mode="json")
                        for override in segment.pronunciation_overrides
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                + "."
            )
        delivery_instruction = " ".join(
            part
            for part in (
                self.settings.instruction.strip(),
                DELIVERY_INSTRUCTIONS[segment.delivery],
                "Keep the expression subtle."
                if segment.intensity == "subtle"
                else "Use a clearly expressive but natural delivery.",
                PACE_INSTRUCTIONS[segment.pace],
                pronunciation_instruction,
            )
            if part
        )
        language = segment.language if segment.language != "English" else self.settings.language
        try:
            response = controller.request(
                {
                    "text": segment.text,
                    "seed": self.settings.seed,
                    "language": language,
                    "instruction": delivery_instruction,
                    "use_reference": self.settings.use_reference,
                    "duration_tokens": duration_budget.duration_tokens,
                    "max_new_tokens": duration_budget.max_new_tokens,
                    "duration_budget_source": duration_budget.source,
                    "duration_budget_strategy": MOSS_DURATION_BUDGET_STRATEGY,
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
