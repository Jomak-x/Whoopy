"""Explicit experimental MLX Qwen speech, separate from managed/native Qwen."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
import sys
import tempfile
from array import array
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from whoopy.adapters.tts._json_process import (
    BoundedDiagnostics,
    JsonLineProcessController,
    WorkerProcessError,
)
from whoopy.adapters.tts.mlx_installation import MlxQwenInstallation
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import (
    ProcessedSpeechSynthesizer,
    SpeechProcessingSettings,
    processed_speech_metadata,
)
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    InvalidSynthesisOutput,
    TransientSynthesisError,
    cache_key_for,
    cache_key_for_identity,
)
from whoopy.model_packs.operations import HeavyweightModelSlot
from whoopy.pipeline.cache import CachedSegment, SegmentCache
from whoopy.pipeline.checkpoints import CheckpointedSegment, SegmentCheckpointStore
from whoopy.ports import AdapterMetadata, SpeechCapabilities, SpeechSynthesizer
from whoopy.timeline import SpeechSegment

MLX_QWEN_BACKENDS = {
    "mlx-qwen3-custom-voice-1.7b-8bit": "custom_voice",
    "mlx-qwen3-voice-design-1.7b-8bit": "voice_design",
}
MLX_QWEN_SPEAKERS = ("Ryan", "Aiden", "Serena")
_DELIVERY = {
    "neutral": "Use a natural, steady delivery.",
    "warm": "Use a warm and welcoming delivery.",
    "calm": "Speak in a calm, settled tone.",
    "soothing": "Use a soft, soothing tone without whispering.",
    "grounded": "Sound grounded, steady, and present.",
    "compassionate": "Use a gentle, compassionate tone.",
    "sleepy": "Use a very soft, sleepy delivery that tapers gently.",
    "encouraging": "Sound quietly encouraging without becoming energetic.",
}
_PACE = {
    "natural": "",
    "unhurried": "Keep an unhurried pace.",
    "slow": "Speak slowly with natural phrasing.",
    "very_slow": "Speak very slowly while keeping phrases connected.",
}
_DESIGN = {
    "warm_meditation": "A warm, calm adult narrator with a natural low-energy meditation delivery.",
    "grounded_meditation": (
        "An adult man with a deep warm resonant baritone voice, clear English, "
        "calm natural delivery, relaxed pacing and gentle meditation guidance. No whispering."
    ),
    "sleep_meditation": "A soft adult sleep narrator with low energy and gentle phrasing.",
}


def source_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def adapter_source_sha256() -> str:
    return source_sha256(Path(__file__))


@dataclass(frozen=True)
class MlxQwenSettings:
    installation: MlxQwenInstallation
    worker_script: Path
    raw_output_directory: Path
    models_root: Path
    voice: str = "Ryan"
    language: str = "English"
    seed: int = 42
    startup_timeout_seconds: float = 120
    request_timeout_seconds: float = 480

    def __post_init__(self) -> None:
        if self.installation.backend not in MLX_QWEN_BACKENDS:
            raise ValueError("Unsupported experimental MLX Qwen backend")
        if self.installation.task != MLX_QWEN_BACKENDS[self.installation.backend]:
            raise ValueError("Experimental MLX task does not match backend")
        voices = MLX_QWEN_SPEAKERS if self.installation.task == "custom_voice" else _DESIGN
        if self.voice not in voices or self.language != "English":
            raise ValueError("Unsupported experimental MLX voice or language")
        if not 0 <= self.seed < 2**32:
            raise ValueError("MLX seed must be unsigned 32-bit")


class MlxQwenAdapter:
    sample_rate = 24_000

    def __init__(self, settings: MlxQwenSettings) -> None:
        self.settings = settings
        install = settings.installation
        self._controller: JsonLineProcessController | None = None
        self._bytecode: tempfile.TemporaryDirectory[str] | None = None
        self._diagnostics = BoundedDiagnostics()
        self._slot = HeavyweightModelSlot(settings.models_root, install.backend)
        self.last_evidence: dict[str, Any] | None = None
        self.capabilities = SpeechCapabilities(
            voice_modes=("preset",) if install.task == "custom_voice" else ("designed",),
            delivery_styles=tuple(_DELIVERY),  # type: ignore[arg-type]
            intensity_levels=("subtle", "moderate"),
            pace_intents=tuple(_PACE),  # type: ignore[arg-type]
            languages=("English",),
            supports_freeform_instruction=False,
        )
        self.metadata = AdapterMetadata(
            adapter_id="whoopy.experimental_mlx_qwen3." + install.task,
            versioned_model_id=f"{install.repository}@{install.model_revision}",
            runtime_id="experimental-mlx-audio-isolated-python",
            runtime_version=install.runtime_revision,
            license_id="Apache-2.0",
            device="mlx-metal",
            settings=(
                "installation_classification=experimental_local",
                "managed_ready=false",
                f"installation_sha256={install.installation_sha256}",
                f"worker_sha256={source_sha256(settings.worker_script)}",
                f"adapter_sha256={adapter_source_sha256()}",
                f"voice={settings.voice}",
                f"language={settings.language}",
                f"seed={settings.seed}",
                "runtime_license=MIT;dependency_licenses=separate",
                "generation_policy=mlx-qwen-native-eos@1",
                "token_budget=text_units_x12_plus96_clamped_192_2048@1",
                "pcm_conversion=float32_clip_scale_rint_even_s16le@1",
                "cache_scope=run;native_evidence=required@2",
            ),
        )
        self.cache_identity = self.metadata.cache_identity

    def translate_segment(self, segment: SpeechSegment) -> dict[str, object]:
        if segment.voice_mode not in self.capabilities.voice_modes:
            raise FatalSynthesisError("MLX Qwen voice mode does not match checkpoint")
        if segment.language != "English" or segment.pronunciation_overrides:
            raise FatalSynthesisError("MLX Qwen requires English without pronunciation overrides")
        if segment.delivery not in _DELIVERY or segment.pace not in _PACE:
            raise FatalSynthesisError("Unsupported MLX Qwen delivery control")
        instruction = " ".join(
            filter(
                None,
                (
                    _DESIGN[self.settings.voice]
                    if self.settings.installation.task == "voice_design"
                    else "",
                    _DELIVERY[segment.delivery],
                    "Keep the expression subtle."
                    if segment.intensity == "subtle"
                    else "Use clearly expressive but natural emphasis.",
                    _PACE[segment.pace],
                ),
            )
        )
        request: dict[str, object] = {
            "task": self.settings.installation.task,
            "text": segment.text,
            "language": self.settings.language,
            "seed": self.settings.seed,
            "instruction": instruction,
            "segment_id": segment.id,
            "native_artifact_key": cache_key_for(segment, self),
        }
        if self.settings.installation.task == "custom_voice":
            request["speaker"] = self.settings.voice
        return request

    def _start(self) -> JsonLineProcessController:
        if self._controller is not None and self._controller.running:
            return self._controller
        self.close()
        self._bytecode = tempfile.TemporaryDirectory(prefix="whoopy-mlx-bytecode-")
        raw_output = self.settings.raw_output_directory.absolute()
        raw_output.mkdir(parents=True, exist_ok=True)
        command = [
            str(self.settings.installation.python_executable),
            "-I",
            "-B",
            "-X",
            f"pycache_prefix={self._bytecode.name}",
            str(self.settings.worker_script),
            "--model",
            str(self.settings.installation.model_directory),
            "--task",
            self.settings.installation.task,
            "--raw-output-dir",
            str(raw_output),
        ]
        self._controller = JsonLineProcessController(
            command=command,
            label="Experimental MLX Qwen",
            startup_timeout_seconds=self.settings.startup_timeout_seconds,
            request_timeout_seconds=self.settings.request_timeout_seconds,
            shutdown_timeout_seconds=5,
            diagnostics=self._diagnostics,
        )
        try:
            self._slot.acquire()
            ready = self._controller.start()
            if (ready.get("status"), ready.get("sample_rate"), ready.get("device")) != (
                "ready",
                24_000,
                "mlx-metal",
            ):
                raise FatalSynthesisError(
                    "Experimental MLX worker did not become ready: "
                    + str(ready.get("error") or ready)[:2000]
                )
            return self._controller
        except Exception:
            self.close()
            raise

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        self.last_evidence = None
        request = self.translate_segment(segment)
        try:
            response = self._start().request(request)
        except WorkerProcessError as error:
            self.close()
            raise TransientSynthesisError(f"Experimental MLX worker failed: {error}") from error
        evidence = response.get("raw_evidence")
        if isinstance(evidence, dict):
            self._diagnostics.append("native_evidence", json.dumps(evidence, sort_keys=True))
        if response.get("status") != "ok":
            self.close()
            raise InvalidSynthesisOutput(f"MLX generation failed: {response.get('error')}")
        if not isinstance(evidence, dict) or not evidence.get("eos_observed"):
            raise InvalidSynthesisOutput("MLX native evidence or codec EOS is missing")
        if evidence.get("native_artifact_key") != request["native_artifact_key"]:
            raise InvalidSynthesisOutput("MLX evidence has a different synthesis input")
        native_pcm = validate_native_evidence(
            evidence, self.settings.raw_output_directory, expected_request=request
        )
        try:
            encoded = response["pcm_s16le"]
            if not isinstance(encoded, str):
                raise ValueError("PCM must be base64 text")
            pcm = base64.b64decode(encoded, validate=True)
            audio = PcmAudio(pcm_s16le=pcm, sample_rate=self.sample_rate)
        except (KeyError, TypeError, ValueError) as error:
            raise InvalidSynthesisOutput("MLX returned invalid PCM") from error
        if not pcm or audio.frame_count != evidence.get("frames"):
            raise InvalidSynthesisOutput("MLX PCM length does not match native evidence")
        if pcm != native_pcm:
            raise InvalidSynthesisOutput("MLX returned PCM differs from preserved native waveform")
        self.last_evidence = evidence
        return audio

    def prepare(self) -> None:
        self._start()

    @property
    def runtime_process_id(self) -> int | None:
        return self._controller.process_id if self._controller else None

    @property
    def runtime_device(self) -> str | None:
        return "mlx-metal" if self._controller and self._controller.running else None

    def diagnostics(self) -> tuple[str, ...]:
        return self._diagnostics.snapshot()

    def close(self) -> None:
        controller, self._controller = self._controller, None
        try:
            if controller is not None:
                controller.close()
        finally:
            self._slot.release()
            if self._bytecode is not None:
                self._bytecode.cleanup()
                self._bytecode = None


def pcm_from_native_float_wave(payload: bytes) -> bytes:
    """Independently decode the worker's narrow RIFF/FLOAT contract using stdlib.

    Match NumPy float32 multiplication before ties-to-even rounding exactly;
    multiplying as Python float64 alone differs near half-integer boundaries.
    """
    if len(payload) < 12 or payload[:4] != b"RIFF" or payload[8:12] != b"WAVE":
        raise InvalidSynthesisOutput("Native evidence is not a RIFF WAVE")
    if struct.unpack_from("<I", payload, 4)[0] + 8 != len(payload):
        raise InvalidSynthesisOutput("Native WAVE size is inconsistent")
    offset, fmt, data = 12, None, None
    while offset < len(payload):
        if offset + 8 > len(payload):
            raise InvalidSynthesisOutput("Native WAVE chunk header is truncated")
        name, size = struct.unpack_from("<4sI", payload, offset)
        start, end = offset + 8, offset + 8 + size
        if end + (size % 2) > len(payload):
            raise InvalidSynthesisOutput("Native WAVE chunk is truncated")
        if name == b"fmt ":
            if fmt is not None or size < 16:
                raise InvalidSynthesisOutput("Native WAVE has invalid format chunks")
            fmt = struct.unpack_from("<HHIIHH", payload, start)
        elif name == b"data":
            if data is not None:
                raise InvalidSynthesisOutput("Native WAVE has multiple audio payloads")
            data = payload[start:end]
        offset = end + (size % 2)
    if fmt != (3, 1, 24000, 96000, 4, 32) or not data or len(data) % 4:
        raise InvalidSynthesisOutput("Native WAVE must be nonempty mono 24 kHz IEEE float32")
    output = array("h")
    for (value,) in struct.iter_unpack("<f", data):
        if not math.isfinite(value):
            raise InvalidSynthesisOutput("Native WAVE contains nonfinite samples")
        clipped = min(1.0, max(-1.0, value))
        scaled = struct.unpack("<f", struct.pack("<f", clipped * 32767.0))[0]
        output.append(round(scaled))
    if sys.byteorder != "little":
        output.byteswap()
    return output.tobytes()


def validate_native_evidence(
    evidence: dict[str, Any], root: Path, *, expected_request: dict[str, object] | None = None
) -> bytes:
    try:
        contents = {}
        for field, digest_field in (("wav_path", "sha256"), ("sidecar_path", "sidecar_sha256")):
            path = Path(evidence[field])
            if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
                raise InvalidSynthesisOutput("MLX native evidence escapes the run")
            contents[field] = path.read_bytes()
            if hashlib.sha256(contents[field]).hexdigest() != evidence[digest_field]:
                raise InvalidSynthesisOutput("MLX native evidence hash differs")
        if evidence.get("sample_rate") != 24_000 or evidence.get("subtype") != "FLOAT":
            raise InvalidSynthesisOutput("MLX native evidence is not native 24 kHz FLOAT")
        if evidence.get("pcm_conversion") != "float32_clip_scale_rint_even_s16le@1":
            raise InvalidSynthesisOutput("MLX native PCM conversion policy is not recognized")
        sidecar = json.loads(contents["sidecar_path"])
        for key in (
            "sha256",
            "frames",
            "sample_rate",
            "subtype",
            "eos_observed",
            "native_artifact_key",
            "pcm_s16le_sha256",
            "pcm_conversion",
        ):
            if sidecar[key] != evidence[key]:
                raise InvalidSynthesisOutput("MLX response differs from immutable native sidecar")
        completion = sidecar["stop_evidence"]
        if completion.get("completion_valid") is not True or not completion.get("segments"):
            raise InvalidSynthesisOutput("MLX sidecar lacks valid native completion evidence")
        for item in completion["segments"]:
            steps, budget = item["generated_codec_steps"], item["max_tokens"]
            if (
                item.get("codec_eos_observed") is not True
                or not isinstance(steps, int)
                or isinstance(steps, bool)
                or not isinstance(budget, int)
                or isinstance(budget, bool)
                or not 0 < steps < budget
            ):
                raise InvalidSynthesisOutput("MLX sidecar EOS/code-count/budget is inconsistent")
        if expected_request is not None:
            for key, value in expected_request.items():
                if sidecar.get(key) != value:
                    raise InvalidSynthesisOutput(
                        "MLX native sidecar belongs to a different request"
                    )
        pcm = pcm_from_native_float_wave(contents["wav_path"])
        if len(pcm) != evidence["frames"] * 2:
            raise InvalidSynthesisOutput("MLX native WAV frame count differs from evidence")
        if hashlib.sha256(pcm).hexdigest() != evidence["pcm_s16le_sha256"]:
            raise InvalidSynthesisOutput("MLX declared PCM hash differs from native conversion")
        return pcm
    except (KeyError, OSError, TypeError, ValueError, struct.error) as error:
        raise InvalidSynthesisOutput(f"MLX native evidence is incomplete: {error}") from error


class MlxProcessedSpeech(ProcessedSpeechSynthesizer):
    """Retain the native-to-processed relationship alongside the run-local cache."""

    inner: MlxQwenAdapter

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        audio = super().synthesize(segment)
        evidence = self.inner.last_evidence
        if evidence is None:
            raise InvalidSynthesisOutput("MLX native evidence was not retained")
        receipt = {
            "schema_version": 2,
            "cache_key": cache_key_for(segment, self),
            "native_cache_key": cache_key_for(segment, self.inner),
            "segment": segment.model_dump(mode="json"),
            "processing": asdict(self.settings),
            "processed_pcm_sha256": hashlib.sha256(audio.pcm_s16le).hexdigest(),
            "native": evidence,
            "segment_id": segment.id,
            "text": segment.text,
            "adapter": self.inner.metadata.model_dump(mode="json"),
        }
        folder = self.inner.settings.raw_output_directory / "cache-references"
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{receipt['cache_key']}.json"
        with tempfile.NamedTemporaryFile(mode="w", dir=folder, delete=False) as stream:
            json.dump(receipt, stream, indent=2)
            temporary = Path(stream.name)
        temporary.replace(destination)
        return audio


class MlxEvidenceCache(SegmentCache):
    """A PCM hit is usable only while its immutable native evidence still verifies."""

    def __init__(self, root: Path, native_root: Path) -> None:
        super().__init__(root)
        self.native_root = native_root

    def load(
        self, cache_key: str, *, expected_synthesizer: SpeechSynthesizer | None = None
    ) -> CachedSegment | None:
        cached = super().load(cache_key, expected_synthesizer=expected_synthesizer)
        if cached is None:
            return None
        return (
            cached
            if valid_cached_native_evidence(self.native_root, cache_key, cached.metadata.pcm_sha256)
            else None
        )


@dataclass
class _RetainedPcm:
    """Feed verified retained samples through the normal processor without inference."""

    metadata: AdapterMetadata
    audio: PcmAudio
    sample_rate: int = 24000

    cache_identity: str = field(init=False)

    def __post_init__(self) -> None:
        self.cache_identity = self.metadata.cache_identity

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        return self.audio

    def close(self) -> None:
        pass


def valid_cached_native_evidence(root: Path, cache_key: str, pcm_sha256: str | None) -> bool:
    try:
        receipt = json.loads((root / "cache-references" / f"{cache_key}.json").read_text())
        if receipt["cache_key"] != cache_key or receipt["processed_pcm_sha256"] != pcm_sha256:
            return False
        if receipt["schema_version"] != 2 or not receipt["native"]["eos_observed"]:
            return False
        segment = SpeechSegment.model_validate(receipt["segment"])
        inner = AdapterMetadata.model_validate(receipt["adapter"])
        settings = SpeechProcessingSettings(**receipt["processing"])
        processed = processed_speech_metadata(
            inner,
            inner_cache_identity=inner.cache_identity,
            settings=settings,
        )
        native_key = cache_key_for_identity(
            segment, synthesizer_identity=inner.cache_identity, sample_rate=24000
        )
        processed_key = cache_key_for_identity(
            segment, synthesizer_identity=processed.cache_identity, sample_rate=24000
        )
        if (
            native_key != receipt["native_cache_key"]
            or native_key != receipt["native"]["native_artifact_key"]
            or processed_key != cache_key
        ):
            return False
        native_pcm = validate_native_evidence(receipt["native"], root)
        regenerated = ProcessedSpeechSynthesizer(
            _RetainedPcm(inner, PcmAudio(native_pcm)), settings=settings
        ).synthesize(segment)
        if hashlib.sha256(regenerated.pcm_s16le).hexdigest() != pcm_sha256:
            return False
    except (OSError, KeyError, ValueError, TypeError, InvalidSynthesisOutput):
        return False
    return True


class MlxEvidenceCheckpoints(SegmentCheckpointStore):
    """Apply the same native-evidence rule to resume's checkpoint-first path."""

    def load_completed(
        self, run_id: UUID | str, segment_id: str, *, cache_key: str
    ) -> CheckpointedSegment | None:
        hit = super().load_completed(run_id, segment_id, cache_key=cache_key)
        if hit is None:
            return None
        root = self.run_store.run_directory(run_id) / "native-speech"
        return (
            hit
            if valid_cached_native_evidence(root, cache_key, hit.checkpoint.pcm_sha256)
            else None
        )
