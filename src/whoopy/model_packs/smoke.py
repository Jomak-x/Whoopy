"""Production, networking-disabled smoke probes for Whoopy model packs.

The registry deliberately does not import model runtimes. This module is the
explicit boundary that does: it resolves only pinned local directories, starts
the same adapters used by real renders, synthesizes one short sentence, closes
the runtime, and stores comparable performance evidence.

The MOSS Audio Tokenizer cannot produce speech by itself. Its probe therefore
runs the MOSS Local 5B adapter with the selected tokenizer. That exercises the
tokenizer's reference encode and generated-audio decode paths rather than
claiming readiness from a shallow import check.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from whoopy.adapters.tts import (
    FishSpeech14Adapter,
    FishSpeechSettings,
    MossTTSAdapter,
    MossTTSSettings,
    MossVariant,
    SherpaOnnxKokoroAdapter,
    SherpaOnnxSettings,
)
from whoopy.adapters.tts.sherpa_onnx import _loader_from_directory
from whoopy.audio.models import PcmAudio
from whoopy.hardware import inspect_hardware
from whoopy.model_packs.manager import SmokeResult
from whoopy.model_packs.operations import (
    AcceleratorUsage,
    ModelPackOperationError,
    ModelPerformanceRecorder,
    PerformanceRecordStore,
)
from whoopy.model_packs.registry import (
    ModelPackRegistry,
    ModelPackSpec,
    ModelPackState,
)
from whoopy.model_packs.resolution import resolve_voice_reference
from whoopy.timeline import SpeechSegment

SMOKE_TEXT = "Welcome. Let your shoulders soften."
_RUNNABLE_STATES = {ModelPackState.INSTALLED, ModelPackState.READY}
_OFFLINE_ENVIRONMENT_LOCK = threading.RLock()


class SmokeAdapter(Protocol):
    """Minimal lifecycle shared by real and lightweight test adapters."""

    def prepare(self) -> None: ...

    def synthesize(self, segment: SpeechSegment) -> PcmAudio: ...

    def close(self) -> None: ...


SmokeAdapterFactory = Callable[[ModelPackSpec, Path], SmokeAdapter]


@contextmanager
def _offline_environment() -> Iterator[None]:
    """Force local-only mode without racing process-global environment state."""

    names = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE")
    # ``os.environ`` belongs to the whole process, so two concurrent smoke
    # calls could otherwise restore each other's values out of order. Keep the
    # lock for the complete model operation, not merely the assignments.
    with _OFFLINE_ENVIRONMENT_LOCK:
        previous = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ[name] = "1"
            yield
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


class OfflineModelPackSmokeRunner:
    """Dispatch the five PR14 packs through their real local adapter paths."""

    supported_pack_ids = frozenset(
        {
            "kokoro",
            "fish-speech-1.4",
            "moss-audio-tokenizer-v2",
            "moss-local-5b",
            "moss-8b",
        }
    )

    def __init__(
        self,
        registry: ModelPackRegistry,
        *,
        adapter_factories: Mapping[str, SmokeAdapterFactory] | None = None,
        project_root: Path | None = None,
        references_path: Path | None = None,
    ) -> None:
        self.registry = registry
        self.models_root = registry.models_root
        self.project_root = project_root or Path(__file__).resolve().parents[3]
        self.references_path = references_path or (
            self.project_root / "config" / "voice_references.yaml"
        )
        self._factories = dict(adapter_factories or {})

    def __call__(self, pack: ModelPackSpec, selected_directory: Path) -> SmokeResult:
        if pack.pack_id not in self.supported_pack_ids:
            raise ModelPackOperationError(
                f"No production offline smoke probe exists for {pack.pack_id}."
            )

        recorder = ModelPerformanceRecorder(
            pack_id=pack.pack_id,
            revision=pack.revision,
            accelerator=self._accelerator_for(pack.pack_id),
        )
        adapter: SmokeAdapter | None = None
        rendered: PcmAudio | None = None
        operation_error: BaseException | None = None
        unload_started = 0.0
        unload_succeeded = False
        close_error: BaseException | None = None
        try:
            with _offline_environment():
                adapter = self._adapter(pack, selected_directory)
                adapter.prepare()
                runtime_process_id = getattr(adapter, "runtime_process_id", None)
                if isinstance(runtime_process_id, int):
                    recorder.track_process(runtime_process_id)
                runtime_device = getattr(adapter, "runtime_device", None)
                if isinstance(runtime_device, str):
                    recorder.set_accelerator(self._accelerator_from_device(runtime_device))
                recorder.mark_model_ready()
                recorder.begin_render()
                rendered = adapter.synthesize(SpeechSegment(id="pack-smoke", text=SMOKE_TEXT))
                recorder.end_render()
        except BaseException as error:
            operation_error = error
        finally:
            unload_started = time.monotonic()
            if adapter is not None:
                try:
                    adapter.close()
                    unload_succeeded = True
                except BaseException as error:
                    close_error = error

        if operation_error is not None:
            recorder.close()
            if close_error is not None:
                operation_error.add_note(f"Adapter unload also failed: {close_error}")
            raise operation_error
        if rendered is None:
            recorder.close()
            if close_error is not None:
                raise close_error
            raise ModelPackOperationError("The offline smoke probe produced no audio.")

        duration = rendered.frame_count / rendered.sample_rate
        record = recorder.finish_unload(
            rendered_audio_seconds=duration,
            unload_started_at=unload_started,
            unload_succeeded=unload_succeeded,
        )
        PerformanceRecordStore(self.registry.records_directory(pack.pack_id)).write(record)
        if close_error is not None:
            raise ModelPackOperationError(
                f"Audio rendered, but {pack.pack_id} did not unload cleanly: {close_error}"
            ) from close_error
        if not unload_succeeded:
            raise ModelPackOperationError(f"{pack.pack_id} did not unload cleanly.")

        codec_note = (
            " MOSS Local 5B exercised tokenizer reference encoding and output decoding."
            if pack.pack_id == "moss-audio-tokenizer-v2"
            else ""
        )
        return SmokeResult(
            pcm_s16le=rendered.pcm_s16le,
            sample_rate=rendered.sample_rate,
            message=(
                f"Offline {pack.display_name} probe rendered {duration:.2f}s and unloaded."
                f"{codec_note}"
            ),
            validated_dependencies=tuple(pack.dependencies),
        )

    def _adapter(self, pack: ModelPackSpec, selected_directory: Path) -> SmokeAdapter:
        injected = self._factories.get(pack.pack_id)
        if injected is not None:
            return injected(pack, selected_directory)
        if pack.pack_id == "kokoro":
            return self._kokoro(pack, selected_directory)
        if pack.pack_id == "fish-speech-1.4":
            return self._fish(pack, selected_directory)
        if pack.pack_id in {"moss-local-5b", "moss-8b", "moss-audio-tokenizer-v2"}:
            return self._moss(pack, selected_directory)
        raise ModelPackOperationError(f"Unsupported smoke-test pack: {pack.pack_id}")

    def _kokoro(self, pack: ModelPackSpec, selected_directory: Path) -> SmokeAdapter:
        runtime = self._runtime_directory(pack)
        return SherpaOnnxKokoroAdapter(
            model_directory=selected_directory,
            model_version=pack.revision,
            runtime_version=pack.runtime.revision,
            license_id=pack.license_id,
            settings=SherpaOnnxSettings(speed=0.9),
            module_loader=_loader_from_directory(runtime),
        )

    def _fish(self, pack: ModelPackSpec, selected_directory: Path) -> SmokeAdapter:
        runtime = self._runtime_directory(pack)
        reference = resolve_voice_reference(self.references_path, models_root=self.models_root)
        return FishSpeech14Adapter(
            FishSpeechSettings(
                runtime_directory=runtime,
                worker_script=self.project_root / "scripts" / "fish_speech_1_4_worker.py",
                reference_audio=reference.audio_path,
                reference_text=reference.transcript_path,
                checkpoint_directory=selected_directory,
                models_root=self.models_root,
            )
        )

    def _moss(self, pack: ModelPackSpec, selected_directory: Path) -> SmokeAdapter:
        runtime = self._runtime_directory(pack)
        reference = resolve_voice_reference(self.references_path, models_root=self.models_root)
        codec_status = self.registry.inspect("moss-audio-tokenizer-v2")
        if codec_status.state not in _RUNNABLE_STATES:
            raise ModelPackOperationError(
                "MOSS Audio Tokenizer v2 must be installed and pass hardware preflight."
            )

        if pack.pack_id == "moss-audio-tokenizer-v2":
            model_status = self.registry.inspect("moss-local-5b")
            if model_status.state not in _RUNNABLE_STATES:
                raise ModelPackOperationError(
                    "The tokenizer smoke probe requires the installed MOSS Local 5B pack "
                    "to exercise real encode and decode paths."
                )
            model_directory = model_status.selected_directory
            codec_directory = selected_directory
            variant: MossVariant = "moss-local-v1.5"
        else:
            model_directory = selected_directory
            codec_directory = codec_status.selected_directory
            variant = "moss-local-v1.5" if pack.pack_id == "moss-local-5b" else "moss-v1.5"

        return MossTTSAdapter(
            MossTTSSettings(
                runtime_directory=runtime,
                worker_script=self.project_root / "scripts" / "moss_tts_worker.py",
                model_directory=model_directory,
                codec_directory=codec_directory,
                variant=variant,
                reference_audio=reference.audio_path,
                language="English",
                instruction="Speak slowly, softly, and warmly.",
                use_reference=True,
                models_root=self.models_root,
            )
        )

    def _runtime_directory(self, pack: ModelPackSpec) -> Path:
        for relative in pack.runtime.candidate_directories:
            candidate = self.models_root / relative
            if all((candidate / marker).exists() for marker in pack.runtime.required_markers):
                return candidate
        raise ModelPackOperationError(
            f"No complete isolated {pack.runtime.runtime_id} runtime was found for {pack.pack_id}."
        )

    def _accelerator_for(self, pack_id: str) -> AcceleratorUsage:
        if pack_id == "kokoro":
            return AcceleratorUsage(backend="cpu")
        inspection_path = self.models_root
        while not inspection_path.exists() and inspection_path != inspection_path.parent:
            inspection_path = inspection_path.parent
        available = inspect_hardware(inspection_path).accelerators
        if "metal" in available:
            return AcceleratorUsage(backend="metal", device_name="Apple Metal")
        if "cuda" in available:
            return AcceleratorUsage(backend="cuda", device_name="NVIDIA CUDA")
        return AcceleratorUsage(backend="cpu")

    @staticmethod
    def _accelerator_from_device(device: str) -> AcceleratorUsage:
        normalized = device.lower()
        if normalized.startswith("mps") or "metal" in normalized:
            return AcceleratorUsage(backend="metal", device_name=device)
        if normalized.startswith("cuda"):
            return AcceleratorUsage(backend="cuda", device_name=device)
        if normalized.startswith("cpu"):
            return AcceleratorUsage(backend="cpu", device_name=device)
        return AcceleratorUsage(backend="other", device_name=device)
