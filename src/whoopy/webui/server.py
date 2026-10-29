"""Dependency-free local HTTP server for Whoopy's browser tester.

The web layer deliberately starts the existing CLI in a child process. That
keeps the CLI as the single generation entry point and prevents the tester from
growing a second orchestration path with different validation or recovery rules.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext, suppress
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from importlib import import_module
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

import psutil

from whoopy.adapters.tts import (
    MOSS_LANGUAGES,
    QWEN3_TTS_LANGUAGES,
    QWEN3_TTS_SPEAKERS,
    Qwen3TTSVariant,
    resolve_qwen3_tts_installation,
)
from whoopy.artifacts import (
    ArtifactError,
    ArtifactState,
    ArtifactStore,
    TargetPlatform,
    load_artifact_lock,
)
from whoopy.audio import ExportMetadata, export_finished_audio, load_export_manifest
from whoopy.evaluation.pacing_profiles import PacingProfileRegistry
from whoopy.hardware import diagnose, inspect_hardware, load_runtime_profiles
from whoopy.meditation import MeditationPlan
from whoopy.model_packs import (
    ModelPackState,
    ReadinessCheck,
    classify_model_eligibility,
    load_model_pack_registry,
)
from whoopy.model_packs.resolution import (
    OptionalTTSBackend,
    models_root_from_artifact_store,
    resolve_tts_model_pack,
    resolve_voice_reference,
)
from whoopy.pipeline import RunExecution, RunRecord, RunStage, RunStatus, RunStore
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.revisions import (
    RevisionRequest,
    RevisionStore,
    TimelineEdit,
    validate_semantic_plan_edit,
)
from whoopy.pipeline.runs import RunNotFoundError, RunStoreError
from whoopy.pipeline.takes import SegmentTakeStore
from whoopy.timeline import SpeechSegment
from whoopy.voice_references import VoiceConsent, VoiceReferenceManager
from whoopy.voices import KOKORO_ENGLISH_VOICES
from whoopy.webui.audio_inspection import (
    AudioInspectionError,
    enrich_waveform_context,
    section_descriptors,
    section_wave_bytes,
    segment_wave_bytes,
    waveform_payload,
)

if TYPE_CHECKING:
    from whoopy.evaluation import ProductionExperimentExecutor

MAX_REQUEST_BYTES = 64_000
PROCESS_STOP_TIMEOUT_SECONDS = 2.0
SEGMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MODEL_PACK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}$")
RUN_TRASH_RECEIPT_PATTERN = re.compile(r"^[0-9]{8}T[0-9]{6}-[0-9a-f]{10}$")
WEB_LAUNCH_REQUEST_FILENAME = "web-launch-request.json"
ALLOWED_ARTIFACTS = {
    "run": "run.json",
    "script": "script.md",
    "plan": "plan.json",
    "timeline": "timeline.json",
    "quality": "quality.json",
    "manifest": "audio-manifest.json",
    "models": "model-metadata.json",
}
QWEN_BACKEND_VARIANTS: dict[str, Qwen3TTSVariant] = {
    "qwen3-custom-voice-0.6b": "custom-voice-0.6b",
    "qwen3-custom-voice-1.7b": "custom-voice-1.7b",
    "qwen3-voice-design-1.7b": "voice-design-1.7b",
    "qwen3-base-clone-0.6b": "base-clone-0.6b",
    "qwen3-base-clone-1.7b": "base-clone-1.7b",
}
STUDIO_TTS_BACKENDS = frozenset({"kokoro", "fish-1.4", "moss-local-v1.5", "moss-v1.5"})
QUALITY_PROFILES = ("basic", "lite", "standard", "high")
ENVIRONMENT_STATUS_CACHE_SECONDS = 60.0
MOSS_LOCAL_NAMING_NOTE = (
    "The upstream Local Transformer checkpoint uses a Qwen3-4B language backbone. "
    "Whoopy preserves the established moss-local-5b product and pack IDs for compatibility; "
    "5B is not presented as an exact language-backbone parameter count."
)
QWEN_CUSTOM_06_QUALITY_BLOCKER = (
    "Qwen3 CustomVoice 0.6B remains unavailable for product selection: its sampled MPS "
    "path can produce invalid probabilities, while the bounded greedy recovery reached "
    "the codec-token limit instead of a natural ending. Review EOS quality before enabling it."
)
QWEN_BASE_CLONE_06_QUALITY_BLOCKER = (
    "Qwen3 Base Clone 0.6B is product-blocked on this machine: a current two-segment "
    "proof rendered five words as 15.28 seconds and nine words as 19.12 seconds instead "
    "of reaching a natural ending. Use Base Clone 1.7B or another playable voice while "
    "the 0.6B EOS strategy is reviewed."
)


def _speech_model_capabilities(model: str) -> dict[str, Any]:
    """Return the typed browser contract for one production adapter.

    These declarations describe controls the adapter really consumes. They do
    not imply installation or smoke-test readiness; callers must inspect the
    adjacent readiness fields separately.
    """

    deliveries = [
        "neutral",
        "warm",
        "calm",
        "soothing",
        "grounded",
        "compassionate",
        "sleepy",
        "encouraging",
    ]
    if model == "kokoro":
        return {
            "voice_modes": ["preset"],
            "languages": ["English"],
            # Kokoro consumes voice and speed, but has no instruction channel.
            # Keep the browser contract aligned with the adapter instead of
            # offering expressive controls which would be normalized away.
            "delivery": {"values": ["neutral"], "fallback": "neutral"},
            "intensity": {"values": ["subtle"], "fallback": "subtle"},
            "pace": {
                "speed_range": [0.5, 1.2],
                "values": ["natural", "unhurried", "slow", "very_slow"],
            },
            "preset_voices": sorted(KOKORO_ENGLISH_VOICES),
            "request_fields": ["voice", "speed", "delivery", "pace"],
        }
    if model == "fish-1.4":
        return {
            "voice_modes": ["reference"],
            "languages": ["English"],
            "delivery": {"values": ["neutral"], "fallback": "neutral"},
            "intensity": {"values": ["subtle"], "fallback": "subtle"},
            "pace": {"speed_range": [1.0, 1.0], "values": ["natural"]},
            "reference_source": "installed_or_consented_managed_reference",
            "request_fields": ["voice_reference", "delivery", "pace"],
        }
    if model.startswith("moss-"):
        return {
            "voice_modes": ["reference", "designed"],
            "languages": list(MOSS_LANGUAGES),
            "delivery": {"values": deliveries, "fallback": None},
            "intensity": {"values": ["subtle", "moderate"], "fallback": None},
            "pace": {
                "speed_range": [1.0, 1.0],
                "values": ["natural", "unhurried", "slow", "very_slow"],
            },
            "supports_freeform_instruction": True,
            "supports_pronunciation_overrides": True,
            "supports_duration_control": True,
            "reference_source": "installed_or_consented_managed_reference",
            "request_fields": [
                "moss_language",
                "moss_instruction",
                "moss_use_reference",
                "voice_reference",
                "delivery",
                "intensity",
                "pace",
            ],
        }
    variant = QWEN_BACKEND_VARIANTS[model]
    if variant.startswith("custom-voice"):
        voice_modes = ["preset"]
        fields = ["qwen_language", "qwen_speaker", "delivery", "intensity", "pace"]
        presets: list[str] = list(QWEN3_TTS_SPEAKERS)
    elif variant == "voice-design-1.7b":
        voice_modes = ["designed"]
        fields = [
            "qwen_language",
            "qwen_voice_design_preset",
            "delivery",
            "intensity",
            "pace",
        ]
        presets = ["warm_meditation", "grounded_meditation", "sleep_meditation"]
    else:
        voice_modes = ["reference"]
        fields = ["qwen_language", "qwen_reference", "delivery", "intensity", "pace"]
        presets = []
    return {
        "voice_modes": voice_modes,
        "languages": list(QWEN3_TTS_LANGUAGES),
        "delivery": {
            "values": (
                ["neutral"]
                if variant.startswith("base-clone") or variant == "custom-voice-0.6b"
                else deliveries
            ),
            "fallback": (
                "neutral"
                if variant.startswith("base-clone") or variant == "custom-voice-0.6b"
                else None
            ),
        },
        "intensity": {
            "values": (
                ["subtle"]
                if variant.startswith("base-clone") or variant == "custom-voice-0.6b"
                else ["subtle", "moderate"]
            ),
            "fallback": (
                "subtle"
                if variant.startswith("base-clone") or variant == "custom-voice-0.6b"
                else None
            ),
        },
        "pace": {
            "speed_range": [1.0, 1.0],
            "values": (
                ["natural"]
                if variant.startswith("base-clone") or variant == "custom-voice-0.6b"
                else ["natural", "unhurried", "slow", "very_slow"]
            ),
        },
        "supports_freeform_instruction": (
            not variant.startswith("base-clone") and variant != "custom-voice-0.6b"
        ),
        "preset_values": presets,
        "requires_consented_reference": variant.startswith("base-clone"),
        "request_fields": fields,
    }


def _model_pack_service(registry_path: Path, models_root: Path) -> Any:
    """Build the optional-pack facade only when a pack endpoint is used."""

    manager_type = import_module("whoopy.model_packs").ManagedModelPacks
    return manager_type.from_paths(
        registry_path=registry_path,
        models_root=models_root,
    )


def _runtime_pack_service(registry_path: Path, models_root: Path) -> Any:
    """Build the immutable isolated-runtime installer at the web boundary."""

    manager_type = import_module("whoopy.model_packs").ManagedRuntimePacks
    return manager_type.from_path(registry_path, models_root=models_root)


def _model_pack_payload(value: Any) -> dict[str, Any]:
    """Normalize a typed pack report for the dependency-free HTTP server."""

    model_dump = getattr(value, "model_dump", None)
    payload = model_dump(mode="json") if callable(model_dump) else value
    if not isinstance(payload, dict):
        raise ValueError("Model-pack operations must return a JSON object.")
    return cast(dict[str, Any], payload)


@dataclass
class GenerationTask:
    """Ephemeral process handle for a durable browser-initiated run.

    This deliberately does *not* own status or progress.  Those values live in
    ``run.json`` so a restarted web server can still show the real run state.
    """

    task_id: str
    run_id: str
    mode: str | None = None
    process: Any | None = None
    launching: bool = True
    cancel_requested: bool = False
    error: str | None = None
    prior_process_attempts: int = 0
    prior_resume_count: int = 0
    resumed_web_launch: bool = False

    @property
    def active(self) -> bool:
        """Include the pre-Popen window in duplicate/cancel decisions."""

        return self.launching or (self.process is not None and self.process.poll() is None)


@dataclass
class ModelPackTask:
    """Ephemeral supervision for one durable/resumable model-pack operation."""

    task_id: str
    pack_id: str
    action: str
    payload: dict[str, Any]
    created_at: datetime
    resource_kind: Literal["model", "runtime"] = "model"
    status: str = "queued"
    stage: str = "queued"
    message: str = "Waiting for the local model operation slot."
    started_at: datetime | None = None
    finished_at: datetime | None = None
    result: dict[str, Any] | None = None
    error: str | None = None


class LocalWebApplication:
    """Own local paths, task processes, and inspectable run history."""

    def __init__(
        self,
        *,
        project_root: Path,
        config_directory: Path,
        models_directory: Path,
        runs_directory: Path,
        command_launcher: Callable[..., Any] | None = None,
        model_pack_service_factory: Callable[[Path, Path], Any] | None = None,
        runtime_pack_service_factory: Callable[[Path, Path], Any] | None = None,
        evaluation_executor: ProductionExperimentExecutor | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config_directory = self._resolve(config_directory)
        self.models_directory = self._resolve(models_directory)
        self.model_pack_models_root = models_root_from_artifact_store(self.models_directory)
        self.runs_directory = self._resolve(runs_directory)
        self.voice_reference_manager = VoiceReferenceManager(
            self.model_pack_models_root / "voice-references"
        )
        self._tasks: dict[str, GenerationTask] = {}
        self._task_lock = threading.Lock()
        self._model_pack_tasks: dict[str, ModelPackTask] = {}
        self._model_pack_task_lock = threading.Lock()
        self._operation_admission_lock = threading.RLock()
        self._environment_status_lock = threading.Lock()
        self._environment_status_cache: tuple[float, dict[str, Any]] | None = None
        # Injection keeps web tests independent from a shell child process.
        self._command_launcher: Callable[..., Any] = command_launcher or subprocess.Popen
        # The web layer deliberately receives an already-composed service. It
        # never imports a TTS runtime or reconstructs pack file paths itself.
        self._model_pack_service_factory = model_pack_service_factory or _model_pack_service
        self._runtime_pack_service_factory = runtime_pack_service_factory or _runtime_pack_service
        # Lab comparisons must enter through an explicitly composed production
        # executor. None keeps definitions queued and visible without inventing
        # fixture artifacts in the shipped application.
        self.evaluation_executor = evaluation_executor

    @contextmanager
    def _admit_generation(self) -> Iterator[None]:
        """Atomically exclude model/runtime mutation while registering a worker."""

        with self._operation_admission_lock:
            with self._model_pack_task_lock:
                if any(
                    task.status in {"queued", "running"} for task in self._model_pack_tasks.values()
                ):
                    raise ValueError(
                        "Wait for the active model or runtime operation before generating."
                    )
            if self._has_active_generation():
                raise ValueError(
                    "One local generation worker is already active. "
                    "Wait, cancel it, or resume later."
                )
            yield

    @contextmanager
    def _admit_model_operation(self) -> Iterator[None]:
        """Atomically exclude a generation worker while touching model state."""

        with self._operation_admission_lock:
            if self._has_active_generation():
                raise ValueError("Finish the active meditation before changing or loading models.")
            yield

    def _has_active_generation(self) -> bool:
        with self._task_lock:
            if any(task.active for task in self._tasks.values()):
                return True
        if not self.runs_directory.is_dir():
            return False
        store = RunStore(self.runs_directory)
        for directory in self.runs_directory.iterdir():
            if not directory.is_dir():
                continue
            try:
                run_id = store.parse_run_id(directory.name)
                store.load(run_id)
            except (ValueError, RunStoreError):
                continue
            try:
                with RunLock(directory):
                    record = store.reconcile_stale_run_locked(run_id)
                    if record.status is RunStatus.RUNNING:
                        return True
            except RunLockUnavailable:
                return True
        return False

    def list_voice_references(self) -> dict[str, Any]:
        """Return active consented references without exposing absolute paths."""

        return {
            "voice_references": [
                item.model_dump(mode="json") for item in self.voice_reference_manager.list()
            ]
        }

    def demo_voice_reference(self) -> dict[str, Any]:
        """Return the verified, read-only project demo reference.

        This source is intentionally separate from the managed user-reference
        store: it is project-generated and must never imply that the user owns
        or consented a personal voice.
        """

        resolved = resolve_voice_reference(
            self.config_directory / "voice_references.yaml",
            models_root=self.model_pack_models_root,
        )
        with wave.open(str(resolved.audio_path), "rb") as source:
            frame_rate = source.getframerate()
            duration_ms = round(source.getnframes() * 1000 / frame_rate)
        return {
            "reference_id": resolved.reference_id,
            "display_name": "Whoopy local demo reference",
            "duration_ms": duration_ms,
            "source_kind": "project_generated",
            "consent_scope": resolved.consent_scope,
            "read_only": True,
        }

    def demo_voice_reference_preview_path(self) -> tuple[Path, int]:
        """Return the verified project demo WAV without exposing its path."""

        resolved = resolve_voice_reference(
            self.config_directory / "voice_references.yaml",
            models_root=self.model_pack_models_root,
        )
        metadata = self.demo_voice_reference()
        return resolved.audio_path, cast(int, metadata["duration_ms"])

    def voice_reference(self, reference_id: str) -> dict[str, Any]:
        """Return verified active revision metadata."""

        return self.voice_reference_manager.load(reference_id).model_dump(mode="json")

    def voice_reference_preview_path(self, reference_id: str) -> tuple[Path, int]:
        """Return a verified local WAV and its advertised preview duration."""

        record = self.voice_reference_manager.load(reference_id)
        return (
            self.voice_reference_manager.root / record.preview.audio_path,
            record.preview.duration_ms,
        )

    def import_voice_reference(
        self,
        *,
        reference_id: str,
        display_name: str,
        source_wav: Path,
        transcript: str,
        consent: VoiceConsent,
        trim_start_ms: int,
        trim_end_ms: int | None,
        replace: bool,
    ) -> dict[str, Any]:
        """Create or replace a reference through the authoritative manager."""

        operation = (
            self.voice_reference_manager.replace
            if replace
            else self.voice_reference_manager.import_wav
        )
        return operation(
            reference_id=reference_id,
            display_name=display_name,
            source_wav=source_wav,
            transcript=transcript,
            consent=consent,
            trim_start_ms=trim_start_ms,
            trim_end_ms=trim_end_ms,
        ).model_dump(mode="json")

    def delete_voice_reference(self, reference_id: str, *, confirmation: str) -> dict[str, Any]:
        """Move all revisions to recoverable managed trash."""

        return self.voice_reference_manager.delete(
            reference_id, confirmation=confirmation
        ).model_dump(mode="json")

    def restore_voice_reference(self, trash_id: str, *, confirmation: str) -> dict[str, Any]:
        """Restore a recoverably deleted reference by receipt ID."""

        return self.voice_reference_manager.restore(trash_id, confirmation=confirmation).model_dump(
            mode="json"
        )

    def _resolve(self, path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (self.project_root / path).resolve()

    def list_model_packs(self) -> dict[str, Any]:
        """Return declarative pack readiness without loading a voice model."""

        return _model_pack_payload(
            self._model_pack_service_factory(
                self.config_directory / "model_packs.yaml", self.model_pack_models_root
            ).list()
        )

    def list_runtime_packs(self) -> dict[str, Any]:
        """Expose truthful isolated-runtime state without requiring the CLI."""

        service = self._runtime_pack_service_factory(
            self.config_directory / "runtime_packs.yaml", self.model_pack_models_root
        )
        runtime_packs: list[dict[str, Any]] = []
        for item in service.list():
            payload = item.model_dump(mode="json")
            spec = service.get(item.runtime_id)
            bundle = next(
                (candidate for candidate in spec.bundles if candidate.target == item.target),
                None,
            )
            payload["browser_installable"] = bool(bundle is not None and bundle.url)
            payload["install_source"] = (
                "immutable_https"
                if bundle is not None and bundle.url
                else "offline_bundle_only"
                if bundle is not None
                else "unpublished"
            )
            runtime_packs.append(payload)
        return {"runtime_packs": runtime_packs}

    def runtime_pack_action(
        self, action: str, runtime_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Run one allow-listed runtime operation from the local product."""

        with self._admit_model_operation():
            with self._model_pack_task_lock:
                if any(
                    task.status in {"queued", "running"} for task in self._model_pack_tasks.values()
                ):
                    raise ValueError("Wait for the active model or runtime operation.")
            return self._runtime_pack_action_unlocked(action, runtime_id, payload)

    def _runtime_pack_action_unlocked(
        self, action: str, runtime_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:

        if not MODEL_PACK_ID_PATTERN.fullmatch(runtime_id):
            raise ValueError("Choose a declared runtime pack.")
        service = self._runtime_pack_service_factory(
            self.config_directory / "runtime_packs.yaml", self.model_pack_models_root
        )
        if action == "verify":
            if payload:
                raise ValueError("Runtime verification does not accept request fields.")
            result = service.inspect(runtime_id)
        elif action == "install":
            allow_network = payload.get("allow_network", False)
            if set(payload) != {"allow_network"} or not isinstance(allow_network, bool):
                raise ValueError("Runtime install accepts only boolean allow_network.")
            result = service.install(
                runtime_id, offline_directory=None, allow_network=allow_network
            )
        else:
            raise ValueError("Unknown runtime-pack action.")
        self._invalidate_environment_status()
        return _model_pack_payload(result)

    def start_runtime_pack_task(
        self, action: str, runtime_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """Install an isolated runtime in the shared heavyweight-operation slot."""

        if action != "install":
            raise ValueError("Only runtime install can start a background task.")
        if not MODEL_PACK_ID_PATTERN.fullmatch(runtime_id):
            raise ValueError("Choose a declared runtime pack.")
        allow_network = payload.get("allow_network", False)
        if set(payload) != {"allow_network"} or not isinstance(allow_network, bool):
            raise ValueError("Runtime install accepts only boolean allow_network.")
        with self._admit_model_operation(), self._model_pack_task_lock:
            if any(
                task.status in {"queued", "running"} for task in self._model_pack_tasks.values()
            ):
                raise ValueError("One model or runtime operation is already active.")
            task = ModelPackTask(
                task_id=str(uuid4()),
                pack_id=runtime_id,
                action=action,
                payload=dict(payload),
                created_at=datetime.now(UTC),
                resource_kind="runtime",
                message="Waiting for the local runtime installation slot.",
            )
            self._model_pack_tasks[task.task_id] = task
        threading.Thread(
            target=self._run_model_pack_task,
            args=(task,),
            name=f"whoopy-runtime-{action}-{runtime_id}",
            daemon=True,
        ).start()
        return self._model_pack_task_public(task)

    def runtime_pack_task(self, task_id: str) -> dict[str, Any] | None:
        with self._model_pack_task_lock:
            task = self._model_pack_tasks.get(task_id)
        if task is None or task.resource_kind != "runtime":
            return None
        return self._model_pack_task_public(task)

    def model_pack_sample_path(self, pack_id: str) -> Path:
        """Resolve a saved smoke sample through the same current-evidence guard."""

        if not MODEL_PACK_ID_PATTERN.fullmatch(pack_id):
            raise ValueError("Choose a declared model pack.")
        service = self._model_pack_service_factory(
            self.config_directory / "model_packs.yaml", self.model_pack_models_root
        )
        return cast(Path, service.smoke_sample_path(pack_id))

    def start_model_pack_task(
        self,
        action: str,
        pack_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run install or real synthesis off the request thread with pollable truth."""

        if action not in {"install", "smoke-test"}:
            raise ValueError("Only install and smoke-test can start background model tasks.")
        if not MODEL_PACK_ID_PATTERN.fullmatch(pack_id):
            raise ValueError("Choose a declared model pack.")
        if action == "install":
            allow_network = payload.get("allow_network", False)
            if set(payload) - {"allow_network"} or not isinstance(allow_network, bool):
                raise ValueError("Install accepts only boolean allow_network.")
        elif payload:
            raise ValueError("Smoke test does not accept request fields.")
        with self._admit_model_operation(), self._model_pack_task_lock:
            if any(
                task.status in {"queued", "running"} for task in self._model_pack_tasks.values()
            ):
                raise ValueError("One model installation or live test is already active.")
            task = ModelPackTask(
                task_id=str(uuid4()),
                pack_id=pack_id,
                action=action,
                payload=dict(payload),
                created_at=datetime.now(UTC),
            )
            self._model_pack_tasks[task.task_id] = task
        threading.Thread(
            target=self._run_model_pack_task,
            args=(task,),
            name=f"whoopy-model-{action}-{pack_id}",
            daemon=True,
        ).start()
        return self._model_pack_task_public(task)

    def model_pack_task(self, task_id: str) -> dict[str, Any] | None:
        with self._model_pack_task_lock:
            task = self._model_pack_tasks.get(task_id)
        if task is None or task.resource_kind != "model":
            return None
        return self._model_pack_task_public(task)

    def _run_model_pack_task(self, task: ModelPackTask) -> None:
        with self._model_pack_task_lock:
            task.status = "running"
            task.started_at = datetime.now(UTC)
            if task.resource_kind == "runtime":
                task.stage = "installing_runtime"
                task.message = "Installing and checksum-verifying the isolated runtime."
            elif task.action == "install":
                task.stage = "installing"
                task.message = "Installing and checksum-verifying pinned model files."
            else:
                task.stage = "runtime_probe_and_synthesis"
                task.message = (
                    "Loading the isolated runtime and rendering a real offline voice sample."
                )
        try:
            result = (
                self._runtime_pack_action_unlocked(task.action, task.pack_id, task.payload)
                if task.resource_kind == "runtime"
                else self._model_pack_action_unlocked(task.action, task.pack_id, task.payload)
            )
        except (OSError, ValueError) as error:
            with self._model_pack_task_lock:
                task.status = "failed"
                task.stage = "failed"
                task.message = "The local model operation failed."
                task.error = str(error)
                task.finished_at = datetime.now(UTC)
        else:
            with self._model_pack_task_lock:
                task.status = "completed"
                task.stage = "completed"
                if task.resource_kind == "runtime":
                    task.message = "Pinned isolated runtime finished installing and verifying."
                else:
                    task.message = (
                        "Real playable synthesis passed and was saved."
                        if task.action == "smoke-test"
                        else "Pinned model files finished installing and verifying."
                    )
                task.result = result
                task.finished_at = datetime.now(UTC)

    @staticmethod
    def _model_pack_task_public(task: ModelPackTask) -> dict[str, Any]:
        now = task.finished_at or datetime.now(UTC)
        start = task.started_at or task.created_at
        resident_bytes = 0
        measured = False
        try:
            process = psutil.Process(os.getpid())
            try:
                processes = (process, *process.children(recursive=True))
            except (psutil.Error, OSError):
                processes = (process,)
            for current in processes:
                try:
                    if current.is_running():
                        resident_bytes += current.memory_info().rss
                        measured = True
                except (psutil.Error, OSError):
                    continue
        except (psutil.Error, OSError):
            pass
        return {
            "task_id": task.task_id,
            "pack_id": task.pack_id,
            "resource_kind": task.resource_kind,
            "action": task.action,
            "status": task.status,
            "stage": task.stage,
            "message": task.message,
            "elapsed_seconds": max(0.0, (now - start).total_seconds()),
            "resident_memory_bytes": resident_bytes if measured else None,
            "cancellable": False,
            "result": task.result,
            "error": task.error,
        }

    def _require_speech_model_ready(self, tts_model: str, *, surface: str) -> None:
        """Reject selections that have not earned current production readiness."""

        if tts_model in QWEN_BACKEND_VARIANTS:
            if surface != "lab" and not self._qwen_has_current_studio_promotion(tts_model):
                raise ValueError(
                    f"{tts_model} is Lab-only until a saved evaluation decision promotes it."
                )
            quality_blocker = {
                "qwen3-custom-voice-0.6b": QWEN_CUSTOM_06_QUALITY_BLOCKER,
                "qwen3-base-clone-0.6b": QWEN_BASE_CLONE_06_QUALITY_BLOCKER,
            }.get(tts_model)
            if quality_blocker is not None:
                raise ValueError(quality_blocker)
            pack_id = f"qwen3-tts-{QWEN_BACKEND_VARIANTS[tts_model]}"
        else:
            pack_id = {
                "kokoro": "kokoro",
                "fish-1.4": "fish-speech-1.4",
                "moss-local-v1.5": "moss-local-5b",
                "moss-v1.5": "moss-8b",
            }[tts_model]
        statuses = {
            item.get("pack_id"): item
            for item in self.list_model_packs().get("packs", [])
            if isinstance(item, dict)
        }
        report = statuses.get(pack_id)
        state = report.get("state") if isinstance(report, dict) else "missing"
        if state != "ready":
            checks = report.get("checks", []) if isinstance(report, dict) else []
            remedy = next(
                (
                    check.get("message")
                    for check in checks
                    if isinstance(check, dict) and check.get("passed") is False
                ),
                f"install, verify, and run Test voice for {pack_id}",
            )
            raise ValueError(f"{tts_model} is not ready on this machine: {remedy}.")

    def _qwen_current_studio_promotion(self, model_id: str) -> dict[str, Any] | None:
        """Return the exact promoted voice contract after immutable-evidence validation."""

        if model_id not in QWEN_BACKEND_VARIANTS or model_id in {
            "qwen3-custom-voice-0.6b",
            "qwen3-base-clone-0.6b",
        }:
            return None
        try:
            from whoopy.evaluation import EvaluationRepository
            from whoopy.webui.catalog import LocalCatalog

            setting = (
                LocalCatalog(self.runs_directory / ".whoopy-catalog.sqlite3")
                .export_settings()
                .get("studio.default.voice")
            )
            if not isinstance(setting, dict) or setting.get("schema_version") != 2:
                return None
            condition = setting.get("selected_condition")
            voice = condition.get("voice") if isinstance(condition, dict) else None
            if not isinstance(voice, dict) or voice.get("model_id") != model_id:
                return None
            decision_id = str(setting.get("decision_id", ""))
            matching = next(
                (
                    decision
                    for decision in EvaluationRepository(
                        self.runs_directory / ".whoopy-lab"
                    ).decisions()
                    if str(decision.decision_id) == decision_id
                ),
                None,
            )
            selected_parts = str(setting.get("value", "")).split("|")
            condition_matches_value = len(selected_parts) == 6 and selected_parts == [
                str(voice.get("model_id", "")),
                str(voice.get("voice_id", "")),
                str(voice.get("voice_mode", "")),
                str(voice.get("delivery", "")),
                str(voice.get("intensity", "")),
                str(voice.get("language", "")),
            ]
            valid = bool(
                matching is not None
                and matching.outcome == "promote"
                and matching.studio_target == "voice"
                and matching.experiment_id == setting.get("experiment_id")
                and matching.experiment_version == setting.get("experiment_version")
                and matching.candidate_id == setting.get("candidate_id")
                and matching.report_sha256 == setting.get("report_sha256")
                and matching.selected_value == setting.get("value")
                and matching.selected_value.split("|", 1)[0] == model_id
                and condition_matches_value
            )
            return voice if valid else None
        except (OSError, RuntimeError, ValueError):
            return None

    def _qwen_has_current_studio_promotion(self, model_id: str) -> bool:
        return self._qwen_current_studio_promotion(model_id) is not None

    def model_pack_action(
        self,
        action: str,
        pack_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one allow-listed pack operation from the loopback Studio.

        Pack IDs are declarations, not paths. This is the same safety boundary
        used by the terminal: the browser cannot point removal or installation
        at an arbitrary location on the laptop.
        """

        with self._admit_model_operation():
            with self._model_pack_task_lock:
                if any(
                    task.status in {"queued", "running"} for task in self._model_pack_tasks.values()
                ):
                    raise ValueError("Wait for the active model or runtime operation.")
            return self._model_pack_action_unlocked(action, pack_id, payload)

    def _model_pack_action_unlocked(
        self,
        action: str,
        pack_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:

        service = self._model_pack_service_factory(
            self.config_directory / "model_packs.yaml", self.model_pack_models_root
        )
        if action in {"install", "verify", "smoke-test", "remove"} and (
            pack_id is None or not MODEL_PACK_ID_PATTERN.fullmatch(pack_id)
        ):
            raise ValueError("Choose a declared model pack.")
        if action == "install":
            allow_network = payload.get("allow_network", False)
            if not isinstance(allow_network, bool):
                raise ValueError("allow_network must be a boolean.")
            if set(payload) - {"allow_network"}:
                raise ValueError("Install accepts only allow_network.")
            result = service.install(pack_id, offline_directory=None, allow_network=allow_network)
        elif action == "verify":
            if payload:
                raise ValueError("Verify does not accept request fields.")
            result = service.verify(pack_id)
        elif action == "smoke-test":
            if payload:
                raise ValueError("Smoke test does not accept request fields.")
            result = service.smoke_test(pack_id)
        elif action == "unload":
            if payload:
                raise ValueError("Unload does not accept request fields.")
            result = service.unload()
        elif action == "remove":
            if set(payload) != {"confirm"} or payload["confirm"] is not True:
                raise ValueError('Remove requires exactly {"confirm": true}.')
            result = service.remove(pack_id, confirmed=True)
        elif action == "restore":
            receipt_id = payload.get("receipt_id")
            if (
                not isinstance(receipt_id, str)
                or not receipt_id.strip()
                or set(payload) != {"receipt_id"}
            ):
                raise ValueError("Restore requires exactly one non-empty receipt_id.")
            result = service.restore(receipt_id)
        else:
            raise ValueError("Unknown model-pack action.")
        self._invalidate_environment_status()
        return _model_pack_payload(result)

    def environment_status(self) -> dict[str, Any]:
        """Report compatibility and downloads without loading any model.

        Verifying multi-gigabyte model trees is intentionally strict but can
        take several seconds. Keep one short-lived, process-local snapshot so
        moving between Studio, Lab, and Voices does not repeat the same digest
        work. Every model/runtime mutation invalidates this snapshot.
        """

        with self._environment_status_lock:
            cached = self._environment_status_cache
            now = time.monotonic()
            if cached is not None and now - cached[0] <= ENVIRONMENT_STATUS_CACHE_SECONDS:
                return deepcopy(cached[1])
            status = self._compute_environment_status()
            self._environment_status_cache = (time.monotonic(), status)
            return deepcopy(status)

    def _invalidate_environment_status(self) -> None:
        with self._environment_status_lock:
            self._environment_status_cache = None

    def _compute_environment_status(self) -> dict[str, Any]:
        """Build a fresh current-machine readiness snapshot."""

        try:
            inspection_path = self.models_directory
            while not inspection_path.exists() and inspection_path != inspection_path.parent:
                inspection_path = inspection_path.parent
            snapshot = inspect_hardware(inspection_path)
            target = TargetPlatform(
                operating_system=snapshot.operating_system,
                architecture=snapshot.architecture,
            )
            profiles = load_runtime_profiles(self.config_directory / "runtime_profiles.yaml")
            lock = load_artifact_lock(self.config_directory / "artifacts.yaml")
            store = ArtifactStore(self.models_directory)
            profile_reports: dict[str, Any] = {}
            for profile_name in QUALITY_PROFILES:
                diagnosis = diagnose(snapshot, profiles, profile_name)
                artifacts = lock.resolve(profile_name, target)
                states = [store.inspect(artifact).state for artifact in artifacts]
                profile_reports[profile_name] = {
                    "compatible": diagnosis.supported,
                    "installed": bool(states)
                    and all(state is ArtifactState.INSTALLED for state in states),
                    "messages": diagnosis.messages,
                    "artifact_count": len(states),
                }
            pack_report = self.list_model_packs()
            pack_statuses = {
                item["pack_id"]: item
                for item in pack_report.get("packs", [])
                if isinstance(item, dict) and isinstance(item.get("pack_id"), str)
            }
            registry = load_model_pack_registry(
                self.config_directory / "model_packs.yaml",
                models_root=self.model_pack_models_root,
            )

            def optional_status(pack_id: str) -> dict[str, Any]:
                status = pack_statuses.get(pack_id, {})
                state = status.get("state", "missing")
                raw_checks = [
                    check for check in status.get("checks", []) if isinstance(check, dict)
                ]

                def evidence(
                    check_names: set[str], missing_message: str, *, require_all: bool = False
                ) -> dict[str, Any]:
                    matching = [check for check in raw_checks if check.get("check") in check_names]
                    observed = {str(check.get("check")) for check in matching}
                    return {
                        "passed": bool(matching)
                        and (not require_all or check_names <= observed)
                        and all(check.get("passed") is True for check in matching),
                        "message": (
                            "; ".join(
                                str(check.get("message"))
                                for check in matching
                                if check.get("message")
                            )
                            or missing_message
                        ),
                    }

                typed_checks = tuple(ReadinessCheck.model_validate(check) for check in raw_checks)
                spec = registry.get(pack_id)
                eligibility = classify_model_eligibility(
                    pack_id,
                    ModelPackState(state),
                    typed_checks,
                    supported_platforms=spec.supported_platforms,
                )
                return {
                    "ready": state == "ready",
                    "state": state,
                    "error": eligibility.remedy,
                    "studio_candidate": eligibility.studio_candidate,
                    "studio_eligible": eligibility.studio_selectable,
                    "lab_only": False,
                    "lab_visible": eligibility.lab_visible,
                    "machine_resolution": eligibility.machine_resolution,
                    "readiness": {
                        "installed_files": evidence(
                            {"pinned_files", "installed_tree", "shard_indexes"},
                            "Pinned model files have not been verified.",
                        ),
                        "runtime_probe": evidence(
                            {"runtime_probe"},
                            "No current-machine runtime probe has passed.",
                        ),
                        "playable_synthesis": evidence(
                            {"offline_smoke_test", "playable_smoke_sample"},
                            "No playable synthesis matches this model and runtime revision.",
                            require_all=True,
                        ),
                    },
                }

            def qwen_status(backend: str, variant: Qwen3TTSVariant) -> dict[str, Any]:
                status = optional_status(f"qwen3-tts-{variant}")
                quality_blocker = {
                    "custom-voice-0.6b": QWEN_CUSTOM_06_QUALITY_BLOCKER,
                    "base-clone-0.6b": QWEN_BASE_CLONE_06_QUALITY_BLOCKER,
                }.get(variant)
                if quality_blocker is not None:
                    if status.get("state") in {
                        "missing",
                        "partial",
                        "corrupt",
                        "resource_blocked",
                        "incompatible",
                    }:
                        return {
                            **status,
                            "studio_candidate": False,
                            "studio_eligible": False,
                            "lab_only": True,
                            "lab_visible": True,
                        }
                    readiness = dict(status.get("readiness", {}))
                    readiness["playable_synthesis"] = {
                        "passed": False,
                        "message": quality_blocker,
                    }
                    return {
                        **status,
                        "ready": False,
                        "state": "experimental",
                        "error": quality_blocker,
                        "studio_candidate": False,
                        "studio_eligible": False,
                        "lab_only": True,
                        "lab_visible": True,
                        "machine_resolution": "unresolved",
                        "readiness": readiness,
                    }
                promoted = bool(status.get("ready")) and self._qwen_has_current_studio_promotion(
                    backend
                )
                return {
                    **status,
                    "license": "Apache-2.0",
                    "studio_candidate": bool(status.get("ready")),
                    "studio_eligible": promoted,
                    "lab_only": not promoted,
                    "lab_visible": True,
                    "promotion_status": "accepted" if promoted else "lab_evidence_required",
                }

            return {
                "ok": True,
                "system": f"{target.operating_system} {target.architecture}",
                "hardware": snapshot.model_dump(mode="json"),
                "profiles": profile_reports,
                "privacy": "Models and generation stay on this laptop.",
                "speech_models": {
                    "kokoro": {
                        **optional_status("kokoro"),
                        "license": "Apache-2.0",
                        "expression": "voice preset",
                        "capabilities": _speech_model_capabilities("kokoro"),
                    },
                    "fish-1.4": {
                        **optional_status("fish-speech-1.4"),
                        "license": "CC-BY-NC-SA-4.0",
                        "expression": "reference voice; no bracket tags",
                        "capabilities": _speech_model_capabilities("fish-1.4"),
                    },
                    "fish-s2": {
                        "ready": False,
                        "state": "incompatible",
                        "license": "Fish Audio Research License",
                        "expression": "[emotion] tags",
                        "error": "requires a much larger Linux/WSL GPU setup",
                        "studio_eligible": False,
                        "studio_candidate": False,
                        "lab_only": True,
                        "lab_visible": True,
                        "machine_resolution": "proven_incompatibility",
                    },
                    **{
                        model: {
                            **optional_status(pack_id),
                            "license": "Apache-2.0",
                            "expression": "language + free-form delivery instruction",
                            **(
                                {"architecture_note": MOSS_LOCAL_NAMING_NOTE}
                                if model == "moss-local-v1.5"
                                else {}
                            ),
                            "capabilities": _speech_model_capabilities(model),
                        }
                        for model, pack_id in {
                            "moss-local-v1.5": "moss-local-5b",
                            "moss-v1.5": "moss-8b",
                        }.items()
                    },
                    **{
                        backend: {
                            **qwen_status(backend, variant),
                            "expression": (
                                "consented reference clone"
                                if variant.startswith("base-clone")
                                else "reviewed voice design + typed delivery"
                                if variant == "voice-design-1.7b"
                                else "preset speaker + typed delivery"
                            ),
                            "capabilities": _speech_model_capabilities(backend),
                        }
                        for backend, variant in QWEN_BACKEND_VARIANTS.items()
                    },
                },
            }
        except (ArtifactError, OSError, ValueError) as error:
            return {"ok": False, "error": str(error), "profiles": {}}

    def list_runs(self) -> list[dict[str, Any]]:
        """Load the newest valid records while ignoring caches and partial files."""

        if not self.runs_directory.is_dir():
            return []
        records: list[tuple[RunRecord, Path]] = []
        for directory in self.runs_directory.iterdir():
            if not directory.is_dir():
                continue
            try:
                UUID(directory.name)
                record = RunRecord.model_validate_json(
                    (directory / "run.json").read_text(encoding="utf-8")
                )
            except (FileNotFoundError, OSError, ValueError):
                continue
            records.append((record, directory))
        records.sort(key=lambda item: item[0].updated_at, reverse=True)
        return [self._run_summary(record, directory) for record, directory in records]

    def reconcile_runs(self) -> list[dict[str, Any]]:
        """Make abandoned work resumable before the application accepts requests."""

        store = RunStore(self.runs_directory)
        store.reconcile_stale_runs()
        # A process may stop after the browser request was committed but before
        # the CLI child claimed its UUID. Reclassify those abandoned QUEUED
        # records at startup so the library never presents permanently active
        # work. Current web requests retain their exact launch envelope and are
        # resumable; legacy placeholders fail visibly and remain deletable.
        for summary in self.list_runs():
            if summary.get("status") != RunStatus.QUEUED.value:
                continue
            run_id = str(summary["run_id"])
            if self._web_launch_request_path(run_id).is_file():
                self._persist_cancel_intent(run_id)
            else:
                self._persist_process_failure(
                    run_id,
                    "The local app restarted before this legacy request launched. "
                    "No durable launch settings exist; create it again or move it to trash.",
                )
        records = [store.load(str(item["run_id"])) for item in self.list_runs()]
        return [
            self._run_summary(record, self.runs_directory / str(record.run_id))
            for record in records
        ]

    def delete_run(self, run_id: str) -> dict[str, Any] | None:
        """Move a stopped run to recoverable local trash."""

        record = self._record(run_id)
        if record is None:
            return None
        if record.status in (RunStatus.QUEUED, RunStatus.RUNNING):
            raise ValueError("Cancel active generation before deleting this meditation.")
        source = self.runs_directory / str(record.run_id)
        trash = self.runs_directory / ".trash"
        trash.mkdir(parents=True, exist_ok=True)
        if trash.is_symlink() or source.is_symlink():
            raise ValueError("Meditation trash and run directories must not be symbolic links.")
        receipt_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:10]}"
        destination = trash / f"{receipt_id}-{record.run_id}"
        try:
            source.replace(destination)
            receipt = {
                "schema_version": 2,
                "receipt_id": receipt_id,
                "run_id": str(record.run_id),
                "title": record.prompt,
                "audience": record.audience,
                "deleted_at": datetime.now(UTC).isoformat(),
            }
            temporary = destination / f".trash-receipt.{uuid4().hex}.tmp"
            temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
            temporary.replace(destination / "trash-receipt.json")
        except OSError as error:
            if destination.is_dir() and not source.exists():
                with suppress(OSError):
                    destination.replace(source)
            raise ValueError(f"Could not move the meditation to local trash: {error}") from error
        return {
            "run_id": str(record.run_id),
            "receipt_id": receipt_id,
            "recoverable": True,
            "restore_confirmation": self.run_restore_confirmation(receipt_id),
        }

    @staticmethod
    def run_restore_confirmation(receipt_id: str) -> str:
        """Return the exact phrase required for a recoverable run restore."""

        return f"restore meditation {receipt_id}"

    def list_deleted_runs(self) -> list[dict[str, Any]]:
        """List intact recoverable run deletions without following untrusted links."""

        trash = self.runs_directory / ".trash"
        if not trash.is_dir() or trash.is_symlink():
            return []
        entries: list[dict[str, Any]] = []
        for directory in sorted(trash.iterdir(), reverse=True):
            parsed = self._deleted_run_entry(directory)
            if parsed is not None:
                entries.append(parsed)
        return entries

    def restore_deleted_run(self, receipt_id: str, *, confirmation: str) -> dict[str, Any] | None:
        """Restore an intact deleted run after an explicit receipt-bound confirmation."""

        if not RUN_TRASH_RECEIPT_PATTERN.fullmatch(receipt_id):
            raise ValueError("Invalid meditation trash receipt ID.")
        if confirmation != self.run_restore_confirmation(receipt_id):
            raise ValueError(
                f"Restoring requires exact confirmation: "
                f"{self.run_restore_confirmation(receipt_id)!r}."
            )
        candidates = [
            item for item in self.list_deleted_runs() if item.get("receipt_id") == receipt_id
        ]
        if not candidates:
            return None
        if len(candidates) != 1:
            raise ValueError("Meditation trash contains duplicate receipt IDs; restore refused.")
        item = candidates[0]
        run_id = str(item["run_id"])
        source = Path(str(item["path"]))
        destination = self.runs_directory / run_id
        if destination.exists():
            raise ValueError(f"Run {run_id} already exists; restore refused.")
        try:
            source.replace(destination)
            (destination / "trash-receipt.json").unlink(missing_ok=True)
            for temporary in destination.glob(".trash-receipt.*.tmp"):
                temporary.unlink(missing_ok=True)
        except OSError as error:
            if destination.is_dir() and not source.exists():
                with suppress(OSError):
                    destination.replace(source)
            raise ValueError(
                f"Could not restore the meditation from local trash: {error}"
            ) from error
        return {"run_id": run_id, "receipt_id": receipt_id, "restored": True}

    def _deleted_run_entry(self, directory: Path) -> dict[str, Any] | None:
        if not directory.is_dir() or directory.is_symlink():
            return None
        match = re.fullmatch(
            r"(?P<receipt>[0-9]{8}T[0-9]{6}-[0-9a-f]{10})-(?P<run>[0-9a-f-]{36})",
            directory.name,
        )
        if match is None:
            return None
        receipt_id = match.group("receipt")
        try:
            run_id = str(UUID(match.group("run")))
            record_path = directory / "run.json"
            if record_path.is_symlink():
                return None
            record = RunRecord.model_validate_json(record_path.read_text(encoding="utf-8"))
            if str(record.run_id) != run_id:
                return None
            receipt_path = directory / "trash-receipt.json"
            if receipt_path.exists():
                if receipt_path.is_symlink():
                    return None
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("schema_version") not in {1, 2}
                    or receipt.get("receipt_id") != receipt_id
                    or receipt.get("run_id") != run_id
                    or (
                        receipt.get("schema_version") == 2
                        and receipt.get("audience") != record.audience
                    )
                ):
                    return None
                deleted_at = str(receipt.get("deleted_at", ""))
            else:
                # Deletions created before receipt manifests remain recoverable because
                # their directory name and authoritative run record bind both IDs.
                deleted_at = datetime.fromtimestamp(directory.stat().st_mtime, UTC).isoformat()
            return {
                "receipt_id": receipt_id,
                "run_id": run_id,
                "title": record.prompt,
                "audience": record.audience,
                "deleted_at": deleted_at,
                "restore_confirmation": self.run_restore_confirmation(receipt_id),
                "path": str(directory.resolve()),
            }
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def _run_summary(self, record: RunRecord, directory: Path) -> dict[str, Any]:
        duration_seconds: float | None = None
        quality_passed: bool | None = None
        tts_model: str | None = None
        voice: str | None = None
        execution = record.execution
        live_metrics: dict[str, float | int] | None = None
        if (
            execution is not None
            and execution.pid is not None
            and execution.started_at is not None
            and record.status is RunStatus.RUNNING
        ):
            elapsed_seconds = max(
                0.0,
                (datetime.now(UTC) - execution.started_at).total_seconds(),
            )
            try:
                process = psutil.Process(execution.pid)
                try:
                    processes = (process, *process.children(recursive=True))
                except (psutil.Error, OSError):
                    processes = (process,)
                resident_bytes = 0
                measured_process = False
                for current in processes:
                    try:
                        if current.is_running():
                            resident_bytes += current.memory_info().rss
                            measured_process = True
                    except (psutil.Error, OSError):
                        continue
                live_metrics = {
                    "elapsed_seconds": elapsed_seconds,
                    **({"resident_memory_bytes": resident_bytes} if measured_process else {}),
                }
            except (psutil.Error, OSError):
                live_metrics = {"elapsed_seconds": elapsed_seconds}
        try:
            manifest = json.loads((directory / "audio-manifest.json").read_text(encoding="utf-8"))
            duration_seconds = round(float(manifest["duration_ms"]) / 1_000, 2)
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            quality = json.loads((directory / "quality.json").read_text(encoding="utf-8"))
            quality_passed = bool(quality["passed"])
        except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError):
            pass
        try:
            resolved = json.loads((directory / "resolved-config.json").read_text(encoding="utf-8"))
            tts_model = str(resolved["tts"].get("backend", "kokoro"))
            voice = str(resolved["tts"]["voice_name"])
        except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError):
            pass
        audience = record.audience
        if audience == "studio" and record.prompt.startswith("Lab "):
            try:
                raw_record = json.loads((directory / "run.json").read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                raw_record = {}
            if isinstance(raw_record, dict) and "audience" not in raw_record:
                # Historical Lab runs predate durable audience ownership. Limit
                # the title convention to records that genuinely lack the field;
                # a current Studio meditation may legitimately begin with “Lab”.
                audience = "lab"
        return {
            "run_id": str(record.run_id),
            "status": record.status.value,
            "source_kind": record.source_kind,
            "audience": audience,
            "title": record.prompt,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "duration_seconds": duration_seconds,
            "quality_passed": quality_passed,
            "tts_model": tts_model,
            "voice": voice,
            "has_audio": record.audio_artifact is not None
            and (directory / "narration.wav").is_file(),
            "error": record.error,
            "resumable": record.status in (RunStatus.FAILED, RunStatus.INTERRUPTED)
            and (
                record.source_kind != "fixture_prompt"
                or self._web_launch_request_path(str(record.run_id)).is_file()
                or not self._fixture_requires_web_launch(record)
            ),
            "recovery": (
                record.recovery.model_dump(mode="json") if record.recovery is not None else None
            ),
            # Lifecycle stage, heartbeat, lease, and segment position are all
            # persisted by the worker. Returning them verbatim avoids a second,
            # lossy web-only progress model.
            "execution": (execution.model_dump(mode="json") if execution is not None else None),
            "live_metrics": live_metrics,
        }

    def run(self, run_id: str) -> dict[str, Any] | None:
        """Read one durable record; this works after a web-server restart."""

        try:
            record = RunStore(self.runs_directory).load(run_id)
        except (ValueError, RunNotFoundError, RunStoreError):
            return None
        return self._run_summary(record, self.runs_directory / str(record.run_id))

    def generation_attempts(self, run_id: str) -> list[dict[str, Any]] | None:
        """Return bounded failed-generation diagnostics from the managed workspace."""

        try:
            parsed = UUID(run_id)
        except ValueError:
            return None
        if self._record(str(parsed)) is None:
            return None
        managed_root = (self.runs_directory / ".generation-workspaces").resolve()
        attempt_root = managed_root / str(parsed) / "raw-model-output"
        try:
            attempt_root.resolve().relative_to(managed_root)
        except ValueError:
            return []
        if attempt_root.is_symlink() or not attempt_root.is_dir():
            return []
        values: list[dict[str, Any]] = []
        for path in sorted(attempt_root.glob("*.json"))[:100]:
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(attempt_root.resolve())
                if path.is_symlink() or not path.is_file() or path.stat().st_size > 512_000:
                    continue
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(raw, dict):
                continue
            values.append(
                {
                    "filename": path.name,
                    "stage": str(raw.get("stage", "unknown"))[:100],
                    "attempt": raw.get("attempt") if isinstance(raw.get("attempt"), int) else None,
                    "validation_error": str(raw.get("validation_error", ""))[:12_000],
                    "text": str(raw.get("text", ""))[:24_000],
                    "elapsed_seconds": raw.get("elapsed_seconds"),
                    "request_seed": raw.get("request_seed"),
                }
            )
        return values

    def _record(self, run_id: str) -> RunRecord | None:
        try:
            return RunStore(self.runs_directory).load(run_id)
        except (ValueError, RunNotFoundError, RunStoreError):
            return None

    def record_failed_generation(self, prompt: str, error: str) -> RunRecord:
        """Create an honest durable failed run for an unsupported Lab candidate."""

        record = RunStore(self.runs_directory).create(prompt, audience="lab")
        self._persist_process_failure(str(record.run_id), error)
        failed = self._record(str(record.run_id))
        if failed is None:
            raise RunStoreError("Could not persist the failed Lab production run.")
        return failed

    def start_generation(
        self,
        payload: Any,
        *,
        _resume_run_id: str | None = None,
        _run_created: Callable[[UUID], None] | None = None,
    ) -> dict[str, Any]:
        """Validate a browser request and start it without blocking HTTP."""

        if not isinstance(payload, dict):
            raise ValueError("The request body must be a JSON object.")
        mode = payload.get("mode")
        if mode not in ("prompt", "script"):
            raise ValueError("mode must be either 'prompt' or 'script'.")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Please enter a meditation prompt or script.")
        text = text.strip()
        if len(text) > 20_000:
            raise ValueError("Meditation input must contain at most 20,000 characters.")
        voice = payload.get("voice", "af_heart")
        tts_model = payload.get("tts_model", "kokoro")
        if tts_model not in (
            "kokoro",
            "fish-1.4",
            "moss-local-v1.5",
            "moss-v1.5",
            *QWEN_BACKEND_VARIANTS,
        ):
            raise ValueError("Choose an available local speech model.")
        surface = payload.get("surface", "studio")
        if surface not in {"studio", "lab"}:
            raise ValueError("surface must be either 'studio' or 'lab'.")
        if (
            tts_model in QWEN_BACKEND_VARIANTS
            and surface != "lab"
            and not self._qwen_has_current_studio_promotion(tts_model)
        ):
            raise ValueError(
                f"{tts_model} is Lab-only until a saved evaluation decision promotes it."
            )
        if tts_model == "kokoro" and voice not in KOKORO_ENGLISH_VOICES:
            raise ValueError("Choose one of Whoopy's reviewed voices.")
        if tts_model == "fish-1.4":
            resolve_tts_model_pack(
                "fish-1.4",
                registry_path=self.config_directory / "model_packs.yaml",
                references_path=self.config_directory / "voice_references.yaml",
                models_root=self.model_pack_models_root,
                resolve_reference=payload.get("voice_reference") is None,
            )
        voice_reference = payload.get("voice_reference")
        if voice_reference is not None and not isinstance(voice_reference, str):
            raise ValueError("Voice-reference ID must be text.")
        moss_language = payload.get("moss_language") or "English"
        if moss_language not in MOSS_LANGUAGES:
            raise ValueError("Choose one of MOSS-TTS v1.5's 31 supported languages.")
        moss_instruction = payload.get(
            "moss_instruction",
            "Speak slowly, softly, and warmly, with a meditative delivery.",
        )
        if not isinstance(moss_instruction, str) or len(moss_instruction) > 300:
            raise ValueError("MOSS delivery instructions must contain at most 300 characters.")
        moss_use_reference = payload.get("moss_use_reference", True)
        if not isinstance(moss_use_reference, bool):
            raise ValueError("MOSS voice source must be a boolean.")
        if tts_model.startswith("moss-"):
            resolve_tts_model_pack(
                cast(OptionalTTSBackend, tts_model),
                registry_path=self.config_directory / "model_packs.yaml",
                references_path=self.config_directory / "voice_references.yaml",
                models_root=self.model_pack_models_root,
                resolve_reference=moss_use_reference and voice_reference is None,
            )
        if voice_reference is not None:
            if tts_model not in {"fish-1.4", "moss-local-v1.5", "moss-v1.5"}:
                raise ValueError("This speech model does not accept a managed voice reference.")
            if tts_model.startswith("moss-") and not moss_use_reference:
                raise ValueError("MOSS direct voice mode cannot use a voice reference.")
            self.voice_reference_manager.load(voice_reference)
        qwen_language = payload.get("qwen_language", "English")
        if qwen_language not in QWEN3_TTS_LANGUAGES:
            raise ValueError("Choose one of Qwen3-TTS's supported languages.")
        qwen_speaker = payload.get("qwen_speaker", "Serena")
        if qwen_speaker not in QWEN3_TTS_SPEAKERS:
            raise ValueError("Choose one of Qwen3-TTS's reviewed preset speakers.")
        qwen_voice_design_preset = payload.get("qwen_voice_design_preset", "warm_meditation")
        if qwen_voice_design_preset not in {
            "warm_meditation",
            "grounded_meditation",
            "sleep_meditation",
        }:
            raise ValueError("Choose a reviewed Qwen3-TTS voice-design preset.")
        qwen_reference = payload.get("qwen_reference")
        if qwen_reference is not None and not isinstance(qwen_reference, str):
            raise ValueError("Qwen voice-reference ID must be text.")
        if tts_model in QWEN_BACKEND_VARIANTS:
            variant = QWEN_BACKEND_VARIANTS[tts_model]
            resolve_qwen3_tts_installation(
                self.model_pack_models_root,
                variant,
                registry_path=self.config_directory / "model_packs.yaml",
            )
            if variant.startswith("base-clone"):
                if not qwen_reference:
                    raise ValueError("Qwen Base cloning requires a consented voice reference.")
                self.voice_reference_manager.load(qwen_reference)
            elif qwen_reference is not None:
                raise ValueError("Only Qwen Base cloning accepts a voice reference.")
        try:
            speed = float(payload.get("speed", 0.6))
        except (TypeError, ValueError) as error:
            raise ValueError("Speech speed must be a number.") from error
        if not 0.5 <= speed <= 1.2:
            raise ValueError("Speech speed must be between 0.5 and 1.2.")
        try:
            minutes = float(payload.get("minutes", 3))
        except (TypeError, ValueError) as error:
            raise ValueError("Duration must be a number of minutes.") from error
        if mode == "prompt" and not 1 <= minutes <= 30:
            raise ValueError("Prompt duration must be between 1 and 30 minutes.")
        requested_duration_seconds = payload.get("requested_duration_seconds")
        if requested_duration_seconds is not None:
            if mode != "script" or surface != "lab":
                raise ValueError(
                    "A controlled requested duration is valid only for Lab script renders."
                )
            if (
                not isinstance(requested_duration_seconds, int)
                or isinstance(requested_duration_seconds, bool)
                or not 60 <= requested_duration_seconds <= 1_800
            ):
                raise ValueError("Requested duration must be an integer from 60 to 1800 seconds.")
        meditation_kind = payload.get("meditation_kind")
        allowed_kinds = {
            "grounding",
            "focused_attention",
            "breath_awareness",
            "body_scan",
            "noting",
            "reflection",
            "loving_kindness",
            "self_compassion",
            "visualization",
            "open_awareness",
            "sleep",
        }
        if meditation_kind is not None and meditation_kind not in allowed_kinds:
            raise ValueError("Choose a reviewed meditation technique.")
        delivery = payload.get("delivery", "calm")
        allowed_delivery = {
            "neutral",
            "warm",
            "calm",
            "soothing",
            "grounded",
            "compassionate",
            "sleepy",
            "encouraging",
        }
        if delivery not in allowed_delivery:
            raise ValueError("Choose a reviewed delivery preset.")
        intensity = payload.get("intensity", "subtle")
        if intensity not in {"subtle", "moderate"}:
            raise ValueError("Choose subtle or moderate delivery intensity.")
        pace = payload.get("pace", "slow")
        if pace not in {"natural", "unhurried", "slow", "very_slow"}:
            raise ValueError("Choose a reviewed narration pace.")
        if tts_model in {
            "qwen3-custom-voice-0.6b",
            "qwen3-base-clone-0.6b",
            "qwen3-base-clone-1.7b",
        } and (delivery, intensity, pace) != ("neutral", "subtle", "natural"):
            raise ValueError(
                f"{tts_model} supports only neutral delivery, subtle intensity, and natural pace."
            )
        if tts_model in QWEN_BACKEND_VARIANTS and surface == "studio":
            promoted = self._qwen_current_studio_promotion(tts_model)
            if promoted is None:
                raise ValueError(
                    f"{tts_model} is Lab-only until a saved evaluation decision promotes it."
                )
            expected_voice_id = str(promoted.get("voice_id", ""))
            expected_language = str(promoted.get("language", ""))
            expected_delivery = str(promoted.get("delivery", ""))
            expected_intensity = str(promoted.get("intensity", ""))
            variant = QWEN_BACKEND_VARIANTS[tts_model]
            selected_voice_id: str | None
            if variant.startswith("base-clone"):
                selected_voice_id = qwen_reference
            elif variant.startswith("custom-voice"):
                selected_voice_id = qwen_speaker
            else:
                selected_voice_id = qwen_voice_design_preset
            if (
                selected_voice_id != expected_voice_id
                or qwen_language != expected_language
                or delivery != expected_delivery
                or intensity != expected_intensity
            ):
                raise ValueError(
                    "Studio Qwen controls must match the exact accepted Lab voice decision."
                )
        seed = payload.get("seed", 42)
        if not isinstance(seed, int) or isinstance(seed, bool) or seed < 0:
            raise ValueError("Seed must be a non-negative integer.")
        profile = payload.get("profile", "standard" if mode == "prompt" else "basic")
        allowed_profiles = {"lite", "standard", "high"} if mode == "prompt" else {"basic"}
        if profile not in allowed_profiles:
            raise ValueError(f"{mode} mode does not support profile {profile!r}.")
        writing_method = payload.get("writing_method", "plan_first")
        if writing_method not in {
            "plan_first",
            "blueprint_first_sparse",
            "three_candidate_structural_selection",
            "recipe_whole_session",
        }:
            raise ValueError("Choose a production-backed writing method.")
        if mode != "prompt" and writing_method != "plan_first":
            raise ValueError("Writing methods apply only to prompt generation.")
        pacing_profile_id = payload.get("pacing_profile_id")
        if pacing_profile_id is None and "speed" not in payload:
            pacing_profile_id = "meditative-v2"
        pacing_path = self.config_directory / "pacing_profiles.yaml"
        pacing_registry = (
            PacingProfileRegistry.load(pacing_path)
            if pacing_path.is_file()
            else PacingProfileRegistry.load_bundled()
        )
        pacing_profile = (
            pacing_registry.get(pacing_profile_id) if pacing_profile_id is not None else None
        )
        if mode == "prompt" and pacing_profile is not None:
            speed = pacing_profile.speech_speed
        self._require_speech_model_ready(tts_model, surface=surface)
        durable_audience = cast(Literal["studio", "lab"], surface)
        normalized_launch_request = {
            "mode": mode,
            "text": text,
            "voice": voice,
            "tts_model": tts_model,
            "surface": surface,
            "speed": speed,
            "minutes": minutes,
            "requested_duration_seconds": requested_duration_seconds,
            "moss_language": moss_language,
            "moss_instruction": moss_instruction,
            "moss_use_reference": moss_use_reference,
            "meditation_kind": meditation_kind,
            "delivery": delivery,
            "intensity": intensity,
            "pace": pace,
            "seed": seed,
            "qwen_language": qwen_language,
            "qwen_speaker": qwen_speaker,
            "qwen_voice_design_preset": qwen_voice_design_preset,
            "qwen_reference": qwen_reference,
            "voice_reference": voice_reference,
            "profile": profile,
            "writing_method": writing_method,
            "pacing_profile_id": pacing_profile.profile_id if pacing_profile is not None else None,
            "pacing_profile_version": pacing_profile.version
            if pacing_profile is not None
            else None,
            "pacing_profile_sha256": pacing_profile.digest if pacing_profile is not None else None,
        }

        # Allocating the UUID and record before the child begins means the UI
        # can follow this run through planning and even after a web restart.
        with self._admit_generation(), self._task_lock:
            store = RunStore(self.runs_directory)
            if _resume_run_id is None:
                record = store.create(text, audience=durable_audience)
                run_id = str(record.run_id)
                try:
                    self._write_web_launch_request(run_id, normalized_launch_request)
                except (OSError, RunStoreError, ValueError) as error:
                    self._persist_process_failure(
                        run_id,
                        f"Could not save the durable browser launch request: {error}",
                    )
                    raise ValueError(
                        "Could not save the durable browser launch request."
                    ) from error
                task = GenerationTask(task_id=run_id, run_id=run_id, mode=mode)
            else:
                record = store.load(_resume_run_id)
                if (
                    record.source_kind != "fixture_prompt"
                    or record.status not in (RunStatus.FAILED, RunStatus.INTERRUPTED)
                    or record.recovery is None
                ):
                    raise ValueError(
                        "Only an unlaunched durable browser request can use this path."
                    )
                saved = self._load_web_launch_request(_resume_run_id)
                # Requests saved before global typed pace existed reconstruct
                # the former product default instead of becoming unresumable.
                saved.setdefault("pace", "slow")
                saved.setdefault("requested_duration_seconds", None)
                if saved != normalized_launch_request:
                    raise ValueError("Saved browser launch settings changed and cannot be resumed.")
                prior_process_attempts = record.recovery.process_attempts
                prior_resume_count = record.recovery.resume_count
                queued = RunRecord.model_validate(
                    {
                        **record.model_dump(mode="python"),
                        "status": RunStatus.QUEUED,
                        "updated_at": datetime.now(UTC),
                        "recovery": record.recovery.model_copy(
                            update={"process_attempts": 0, "resume_count": 0}
                        ),
                        "execution": RunExecution(stage=RunStage.QUEUED),
                        "error": None,
                    }
                )
                store.save(queued)
                run_id = _resume_run_id
                task = GenerationTask(
                    task_id=run_id,
                    run_id=run_id,
                    mode=mode,
                    prior_process_attempts=prior_process_attempts,
                    prior_resume_count=prior_resume_count,
                    resumed_web_launch=True,
                )
            self._tasks[run_id] = task
        if _run_created is not None:
            try:
                _run_created(record.run_id)
            except Exception:
                with self._task_lock:
                    self._tasks.pop(run_id, None)
                self._persist_process_failure(run_id, "Could not persist generation ownership.")
                raise
        thread = threading.Thread(
            target=self._run_generation,
            args=(
                task,
                text,
                tts_model,
                voice,
                speed,
                minutes,
                requested_duration_seconds,
                moss_language,
                moss_instruction,
                moss_use_reference,
                meditation_kind,
                delivery,
                intensity,
                pace,
                seed,
                qwen_language,
                qwen_speaker,
                qwen_voice_design_preset,
                qwen_reference,
                voice_reference,
                profile,
                writing_method,
                pacing_profile.profile_id if pacing_profile is not None else None,
                pacing_profile.version if pacing_profile is not None else None,
                pacing_profile.digest if pacing_profile is not None else None,
                durable_audience,
            ),
            name=f"whoopy-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self._task_public(task)

    def task(self, task_id: str) -> dict[str, Any] | None:
        try:
            UUID(task_id)
        except ValueError:
            return None
        with self._task_lock:
            task = self._tasks.get(task_id)
        if task is not None:
            return self._task_public(task)
        # The historical /api/tasks endpoint remains a compatibility alias,
        # but it now recovers its answer from the durable record.
        run = self.run(task_id)
        return None if run is None else {"task_id": task_id, **run}

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        """Request cancellation without losing the pre-launch race window."""

        with self._operation_admission_lock:
            try:
                UUID(task_id)
            except ValueError:
                return None
            with self._task_lock:
                task = self._tasks.get(task_id)
                if task is not None and task.active:
                    task.cancel_requested = True
                    process = task.process
                else:
                    process = None
            if task is None or not task.active:
                return self.cancel_run(task_id)
            if process is not None:
                self._stop_process(process)
            return self._task_public(task)

    def cancel_run(self, run_id: str) -> dict[str, Any] | None:
        """Cancel through the CLI so it also works after a web restart."""

        record = self._record(run_id)
        if record is None:
            return None
        if record.status is RunStatus.QUEUED:
            self._persist_cancel_intent(run_id)
            run = self.run(run_id)
            return None if run is None else {"task_id": run_id, **run}
        if record.status is not RunStatus.RUNNING:
            raise ValueError(
                f"Run {run_id} is {record.status.value}; only queued or running runs can cancel."
            )
        return self._launch_recovery_command(run_id, "cancel", prevalidated=True)

    def resume_run(self, run_id: str) -> dict[str, Any] | None:
        """Launch the CLI's durable resume path without inventing web state."""

        with self._admit_generation():
            return self._resume_run_admitted(run_id)

    def _resume_run_admitted(self, run_id: str) -> dict[str, Any] | None:
        try:
            record = RunStore(self.runs_directory).reconcile_stale_run(run_id)
        except (ValueError, RunNotFoundError, RunStoreError):
            return None
        if record.status not in (RunStatus.FAILED, RunStatus.INTERRUPTED):
            raise ValueError(
                f"Run {run_id} is {record.status.value}; only failed or interrupted runs resume."
            )
        launch_request_exists = self._web_launch_request_path(run_id).is_file()
        if record.source_kind == "fixture_prompt" and launch_request_exists:
            try:
                request = self._load_web_launch_request(run_id)
            except (OSError, RunStoreError, ValueError) as error:
                raise ValueError(
                    "This legacy placeholder has no valid durable browser request; "
                    "create it again or move it to trash."
                ) from error
            return self.start_generation(request, _resume_run_id=run_id)
        if record.source_kind == "fixture_prompt" and self._fixture_requires_web_launch(record):
            raise ValueError(
                "This legacy placeholder has no valid durable browser request; "
                "create it again or move it to trash."
            )
        return self._launch_recovery_command_admitted(run_id, "resume")

    @staticmethod
    def _fixture_requires_web_launch(record: RunRecord) -> bool:
        execution = record.execution
        return record.source_kind == "fixture_prompt" and (
            (execution is not None and execution.interruption_kind == "user_cancelled")
            or (record.error is not None and "No durable launch settings exist" in record.error)
        )

    def _web_launch_request_path(self, run_id: str) -> Path:
        return self.runs_directory / str(UUID(run_id)) / WEB_LAUNCH_REQUEST_FILENAME

    def _write_web_launch_request(self, run_id: str, payload: dict[str, Any]) -> None:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        envelope = {
            "schema_version": 1,
            "payload_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            "payload": payload,
        }
        RunStore._write_json(
            self._web_launch_request_path(run_id),
            json.dumps(envelope, indent=2, sort_keys=True) + "\n",
        )

    def _load_web_launch_request(self, run_id: str) -> dict[str, Any]:
        path = self._web_launch_request_path(run_id)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_REQUEST_BYTES:
            raise ValueError("Durable browser launch request is missing or unsafe.")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "payload_sha256",
            "payload",
        }:
            raise ValueError("Durable browser launch request has an invalid envelope.")
        payload = value.get("payload")
        if value.get("schema_version") != 1 or not isinstance(payload, dict):
            raise ValueError("Durable browser launch request has an unsupported schema.")
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        actual = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if value.get("payload_sha256") != actual:
            raise ValueError("Durable browser launch request checksum does not match.")
        return cast(dict[str, Any], payload)

    def regenerate_segment(self, run_id: str, segment_id: str) -> dict[str, Any] | None:
        """Ask the CLI to invalidate and rebuild one named speech segment."""

        record = self._record(run_id)
        if record is None:
            return None
        if record.status not in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
        ):
            raise ValueError(
                f"Run {run_id} is {record.status.value}; only stopped runs regenerate."
            )
        try:
            timeline = RunStore(self.runs_directory).load_timeline(run_id)
        except RunStoreError as error:
            raise ValueError(f"Run {run_id} has no valid timeline to regenerate.") from error
        matching = [segment for segment in timeline.segments if segment.id == segment_id]
        if not matching:
            raise ValueError(f"Segment {segment_id} is not present in run {run_id}.")
        if not isinstance(matching[0], SpeechSegment):
            raise ValueError(f"Segment {segment_id} is not a speech segment.")
        return self._launch_recovery_command(
            run_id,
            "regenerate-segment",
            segment_id,
            prevalidated=True,
        )

    def list_segment_takes(self, run_id: str, segment_id: str) -> dict[str, Any] | None:
        if self._record(run_id) is None:
            return None
        takes = SegmentTakeStore(RunStore(self.runs_directory))
        return {
            "run_id": run_id,
            "segment_id": segment_id,
            "selected_take_id": takes.selected(run_id, segment_id),
            "takes": [value.model_dump(mode="json") for value in takes.list(run_id, segment_id)],
        }

    def generate_alternative_take(self, run_id: str, segment_id: str) -> dict[str, Any] | None:
        """Run real cache-bypassing synthesis, retaining the selected take."""

        with self._operation_admission_lock:
            if self._record(run_id) is None:
                return None
            takes = SegmentTakeStore(RunStore(self.runs_directory))
            original = takes.capture_active(
                run_id, segment_id, source="original", select_if_unset=False
            )
            selected = takes.choose(run_id, segment_id, original.take_id).selected_take_id
            task = self.regenerate_segment(run_id, segment_id)
            if task is None:
                return None
            with self._task_lock:
                generation_task = self._tasks[run_id]
            thread = threading.Thread(
                target=self._finalize_alternative_take,
                args=(generation_task, segment_id, selected),
                daemon=True,
                name=f"whoopy-take-{run_id[:8]}-{segment_id}",
            )
            thread.start()
            return {
                "original_take": original.model_dump(mode="json"),
                "selected_take_id": selected,
                "task": task,
                "status": "generating",
            }

    def _finalize_alternative_take(
        self,
        generation_task: GenerationTask,
        segment_id: str,
        selected_take_id: str,
    ) -> None:
        run_id = generation_task.run_id
        deadline = time.monotonic() + 14_400
        while time.monotonic() < deadline:
            with self._task_lock:
                if (
                    self._tasks.get(run_id) is not generation_task
                    or generation_task.cancel_requested
                ):
                    return
                active = generation_task.active
            state = self._task_public(generation_task)
            if not active and state["status"] not in {"queued", "running"}:
                break
            time.sleep(0.2)
        else:
            return
        with self._operation_admission_lock:
            with self._task_lock:
                if (
                    self._tasks.get(run_id) is not generation_task
                    or generation_task.cancel_requested
                    or generation_task.active
                ):
                    return
            if self._task_public(generation_task)["status"] != "completed":
                return
            takes = SegmentTakeStore(RunStore(self.runs_directory))
            regeneration_attempt_id: UUID | None = None
            history = (self.runs_directory / run_id / "history" / "manual-regeneration").glob(
                "*/request.json"
            )
            try:
                latest = max(history, key=lambda path: path.stat().st_mtime_ns)
                payload = json.loads(latest.read_text(encoding="utf-8"))
                regeneration_attempt_id = UUID(payload["regeneration_attempt_id"])
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                pass
            takes.capture_active(
                run_id,
                segment_id,
                source="production_regeneration",
                regeneration_attempt_id=regeneration_attempt_id,
                select_if_unset=False,
            )
            takes.choose(run_id, segment_id, selected_take_id, action="restore")
            RevisionStore(RunStore(self.runs_directory)).create_assembly_revision(
                run_id,
                note=f"Retained selected take {selected_take_id} after generating an alternative.",
            )
            self.resume_run(run_id)

    def choose_segment_take(
        self,
        run_id: str,
        segment_id: str,
        take_id: str,
        *,
        undo: bool = False,
    ) -> dict[str, Any] | None:
        with self._admit_generation():
            if self._record(run_id) is None:
                return None
            takes = SegmentTakeStore(RunStore(self.runs_directory))
            selection = (
                takes.undo(run_id, segment_id)
                if undo
                else takes.choose(run_id, segment_id, take_id)
            )
            revision = RevisionStore(RunStore(self.runs_directory)).create_assembly_revision(
                run_id,
                note=(
                    f"Restored take {selection.selected_take_id}."
                    if undo
                    else f"Selected take {selection.selected_take_id}."
                ),
            )
            task = self._resume_run_admitted(run_id)
            return {
                "selection": selection.model_dump(mode="json"),
                "revision": revision.model_dump(mode="json"),
                "task": task,
            }

    def segment_take_audio_path(
        self,
        run_id: str,
        segment_id: str,
        take_id: str,
    ) -> Path | None:
        if self._record(run_id) is None:
            return None
        return SegmentTakeStore(RunStore(self.runs_directory)).audio_path(
            run_id, segment_id, take_id
        )

    def waveform(self, run_id: str, *, points: int) -> dict[str, Any] | None:
        """Return PCM positions plus canonical editor and take context."""

        if self._record(run_id) is None:
            return None
        store = RunStore(self.runs_directory)
        payload = waveform_payload(
            store.audio_path(run_id),
            store.audio_manifest_path(run_id),
            points=points,
        )
        timeline = store.load_timeline(run_id)
        takes = SegmentTakeStore(store)
        regenerations: dict[str, dict[str, Any]] = {}
        for segment in timeline.segments:
            if not isinstance(segment, SpeechSegment):
                continue
            available = takes.list(run_id, segment.id)
            selected_take_id = takes.selected(run_id, segment.id)
            selected = next(
                (take for take in available if take.take_id == selected_take_id),
                None,
            )
            has_alternative = any(take.source == "production_regeneration" for take in available)
            status = (
                "regenerated_selected"
                if selected is not None and selected.source == "production_regeneration"
                else "alternatives_available"
                if has_alternative
                else "original"
            )
            regenerations[segment.id] = {
                "status": status,
                "take_count": len(available),
                "selected_take_id": selected_take_id,
            }
        try:
            sections = section_descriptors(
                timeline_path=store.timeline_path(run_id),
                manifest_path=store.audio_manifest_path(run_id),
                plan_path=store.plan_path(run_id),
                draft_sections_path=store.draft_sections_path(run_id),
            )
        except AudioInspectionError:
            # Authored and materially script-edited runs can truthfully have no
            # reviewed section alignment while retaining exact segment spans.
            sections = []
        return enrich_waveform_context(
            payload,
            timeline=timeline,
            sections=sections,
            regenerations=regenerations,
        )

    def segment_audition(self, run_id: str, segment_id: str) -> bytes | None:
        """Return an exact crop from the finished master; never resynthesize."""

        if self._record(run_id) is None:
            return None
        store = RunStore(self.runs_directory)
        return segment_wave_bytes(
            store.audio_path(run_id),
            store.audio_manifest_path(run_id),
            segment_id,
        )

    def sections(self, run_id: str) -> list[dict[str, Any]] | None:
        """Return reviewed section boundaries backed by canonical artifacts."""

        if self._record(run_id) is None:
            return None
        store = RunStore(self.runs_directory)
        return section_descriptors(
            timeline_path=store.timeline_path(run_id),
            manifest_path=store.audio_manifest_path(run_id),
            plan_path=store.plan_path(run_id),
            draft_sections_path=store.draft_sections_path(run_id),
        )

    def section_audition(self, run_id: str, section_id: str) -> bytes | None:
        """Return an exact crop for one reviewed plan section."""

        sections = self.sections(run_id)
        if sections is None:
            return None
        descriptor = next(
            (value for value in sections if value["section_id"] == section_id),
            None,
        )
        if descriptor is None:
            raise ValueError(f"Section {section_id!r} is not in the active plan.")
        store = RunStore(self.runs_directory)
        return section_wave_bytes(
            store.audio_path(run_id),
            store.audio_manifest_path(run_id),
            descriptor,
        )

    def list_revisions(self, run_id: str) -> list[dict[str, Any]] | None:
        if self._record(run_id) is None:
            return None
        return [
            revision.model_dump(mode="json")
            for revision in RevisionStore(RunStore(self.runs_directory)).list(run_id)
        ]

    def edit_run(self, run_id: str, payload: Any) -> dict[str, Any] | None:
        if self._record(run_id) is None:
            return None
        request = RevisionRequest.model_validate(payload)
        if request.timeline.pronunciation_overrides:
            try:
                backend = RunStore(self.runs_directory).load_resolved_config(run_id).tts.backend
            except RunStoreError as error:
                raise ValueError(
                    f"Cannot verify pronunciation support for this run: {error}"
                ) from error
            if not backend.startswith("moss-"):
                raise ValueError(
                    f"{backend} does not support typed pronunciation overrides. "
                    "Create or open a MOSS meditation to edit pronunciation without "
                    "changing the spoken text."
                )
        if request.plan is not None:
            store = RunStore(self.runs_directory)
            try:
                active_plan = MeditationPlan.model_validate_json(
                    store.plan_path(run_id).read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as error:
                raise ValueError(f"The active plan cannot be edited: {error}") from error
            if request.plan != active_plan:
                if request.script is not None or request.timeline != TimelineEdit():
                    raise ValueError(
                        "Save semantic plan edits separately from script or timeline edits."
                    )
                normalized = validate_semantic_plan_edit(
                    active_plan,
                    request.plan,
                    blueprint_path=self.config_directory / "meditation_blueprints.yaml",
                )
                semantic_task = self._launch_plan_regeneration(
                    run_id,
                    normalized,
                    note=request.note,
                )
                return {"semantic_regeneration": True, "task": semantic_task}
        with self._admit_generation():
            revision = RevisionStore(RunStore(self.runs_directory)).create(run_id, request)
            resume_task = self._resume_run_admitted(run_id)
        return {
            "revision": revision.model_dump(mode="json"),
            "task": resume_task,
        }

    def _launch_plan_regeneration(
        self,
        run_id: str,
        plan: MeditationPlan,
        *,
        note: str,
    ) -> dict[str, Any]:
        task = GenerationTask(task_id=run_id, run_id=run_id, mode="plan_regeneration")
        with self._admit_generation(), self._task_lock:
            self._tasks[run_id] = task
        input_root = self.runs_directory / run_id / "history" / "editor-inputs"
        input_root.mkdir(parents=True, exist_ok=True)
        input_path = input_root / f"{uuid4()}.plan.json"
        RunStore(self.runs_directory)._write_bytes(
            input_path,
            (plan.model_dump_json(indent=2) + "\n").encode("utf-8"),
        )
        thread = threading.Thread(
            target=self._run_plan_regeneration,
            args=(task, input_path, note),
            name=f"whoopy-plan-edit-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self._task_public(task)

    def _run_plan_regeneration(
        self,
        task: GenerationTask,
        input_path: Path,
        note: str,
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "whoopy",
            "run",
            "regenerate-plan",
            task.run_id,
            "--plan-file",
            str(input_path),
            "--note",
            note,
            "--json",
            "--config-dir",
            str(self.config_directory),
            "--models-dir",
            str(self.models_directory),
            "--runs-dir",
            str(self.runs_directory),
        ]
        try:
            process = self._command_launcher(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self._process_group_options(),
            )
            with self._task_lock:
                task.process = process
                task.launching = False
                cancel_after_launch = task.cancel_requested
            if cancel_after_launch:
                self._stop_process(process)
            _stdout, stderr = process.communicate()
            with self._task_lock:
                cancelled = task.cancel_requested
            if not cancelled and process.returncode not in (0, 130, 143):
                task.error = self._friendly_process_error(stderr)
        except OSError as error:
            task.error = f"Could not launch plan regeneration: {error}"
        finally:
            with self._task_lock:
                task.launching = False
                task.process = None
            with suppress(OSError):
                input_path.unlink(missing_ok=True)

    def restore_revision(self, run_id: str, revision_id: str) -> dict[str, Any] | None:
        if self._record(run_id) is None:
            return None
        with self._admit_generation():
            revision = RevisionStore(RunStore(self.runs_directory)).restore(run_id, revision_id)
            task = self._resume_run_admitted(run_id)
        return {
            "revision": revision.model_dump(mode="json"),
            "task": task,
        }

    def export_run(self, run_id: str) -> dict[str, Any] | None:
        record = self._record(run_id)
        if record is None:
            return None
        if record.status is not RunStatus.COMPLETED:
            raise ValueError("Only a completed meditation can be exported.")
        audio = self.audio_path(run_id)
        if audio is None:
            raise ValueError("The completed meditation has no narration master.")
        model = "unknown-local-model"
        try:
            config = RunStore(self.runs_directory).load_resolved_config(run_id)
            model = config.tts.backend
        except RunStoreError:
            pass
        manifest = export_finished_audio(
            audio,
            audio.parent / "exports",
            ExportMetadata(
                title=record.prompt.removeprefix("Render local script: "),
                model=model,
                created_at=record.created_at,
            ),
        )
        return manifest.model_dump(mode="json")

    def exports(self, run_id: str) -> dict[str, Any] | None:
        if self._record(run_id) is None:
            return None
        path = self.runs_directory / run_id / "exports" / "export-manifest.json"
        if not path.is_file():
            return {"available": False, "artifacts": []}
        manifest = load_export_manifest(path)
        audio = self.audio_path(run_id)
        if audio is None:
            return {"available": False, "artifacts": []}
        with audio.open("rb") as source:
            source_sha256 = hashlib.file_digest(source, "sha256").hexdigest()
        if source_sha256 != manifest.source_sha256:
            return {"available": False, "artifacts": []}
        return {
            "available": True,
            **manifest.model_dump(mode="json"),
        }

    def export_path(self, run_id: str, kind: str) -> Path | None:
        manifest = self.exports(run_id)
        if manifest is None or not manifest.get("available"):
            return None
        artifacts = manifest.get("artifacts", [])
        matching = [item for item in artifacts if item.get("kind") == kind]
        if len(matching) != 1:
            return None
        filename = matching[0].get("filename")
        if not isinstance(filename, str) or Path(filename).name != filename:
            return None
        path = self.runs_directory / run_id / "exports" / filename
        return path if path.is_file() else None

    def _task_public(self, task: GenerationTask) -> dict[str, Any]:
        run = self.run(task.run_id)
        if run is None:
            # The record was successfully created before the task was stored;
            # this branch is only defensive against external manual deletion.
            return {"task_id": task.task_id, "run_id": task.run_id, "status": "missing"}
        result = {"task_id": task.task_id, **run}
        if task.active and result.get("status") in {"completed", "interrupted", "failed"}:
            # The durable worker writes its stopped record before the
            # supervising child has necessarily returned and released the run
            # lock. Keep the browser task non-terminal during that short
            # finalization window so immediate edits or Resume cannot race it.
            result.update(
                {
                    "status": "running",
                    "execution": {
                        "stage": "finalizing",
                        "message": (
                            "Finalizing the local worker and releasing its run lock. "
                            "Stopped work will become resumable when this finishes."
                        ),
                    },
                }
            )
        if task.mode == "plan_regeneration":
            if task.active:
                result.update(
                    {
                        "status": "running",
                        "execution": {
                            "stage": "drafting",
                            "message": (
                                "Redrafting edited plan sections through the saved "
                                "local LLM context."
                            ),
                        },
                    }
                )
            elif task.error is not None:
                result.update({"status": "failed", "error": task.error})
        return result

    def _launch_recovery_command(
        self,
        run_id: str,
        command_name: str,
        segment_id: str | None = None,
        *,
        prevalidated: bool = False,
    ) -> dict[str, Any] | None:
        if not prevalidated and self.run(run_id) is None:
            return None
        admission = nullcontext() if command_name == "cancel" else self._admit_generation()
        with admission:
            return self._launch_recovery_command_admitted(run_id, command_name, segment_id)

    def _launch_recovery_command_admitted(
        self,
        run_id: str,
        command_name: str,
        segment_id: str | None = None,
    ) -> dict[str, Any]:
        task = GenerationTask(task_id=run_id, run_id=run_id)
        with self._task_lock:
            existing = self._tasks.get(run_id)
            if existing is not None and existing.active:
                raise ValueError("This run already has an active local worker.")
            self._tasks[run_id] = task
        thread = threading.Thread(
            target=self._run_recovery_command,
            args=(task, command_name, segment_id),
            name=f"whoopy-{command_name}-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self._task_public(task)

    def _run_generation(
        self,
        task: GenerationTask,
        text: str,
        tts_model: str,
        voice: str,
        speed: float,
        minutes: float,
        requested_duration_seconds: int | None,
        moss_language: str,
        moss_instruction: str,
        moss_use_reference: bool,
        meditation_kind: str | None,
        delivery: str,
        intensity: str,
        pace: str,
        seed: int,
        qwen_language: str,
        qwen_speaker: str,
        qwen_voice_design_preset: str,
        qwen_reference: str | None,
        voice_reference: str | None,
        profile: str,
        writing_method: str,
        pacing_profile_id: str | None,
        pacing_profile_version: int | None,
        pacing_profile_sha256: str | None,
        audience: Literal["studio", "lab"],
    ) -> None:
        input_path: Path | None = None
        try:
            with self._task_lock:
                cancelled_before_launch = task.cancel_requested
            if cancelled_before_launch:
                self._persist_cancel_intent(task.run_id)
                return
            command = [
                sys.executable,
                "-m",
                "whoopy",
                "generate",
                "--json",
                "--run-id",
                task.run_id,
                "--tts-model",
                tts_model,
                "--speed",
                str(speed),
                "--seed",
                str(seed),
                "--delivery",
                delivery,
                "--intensity",
                intensity,
                "--pace",
                pace,
                "--moss-language",
                moss_language,
                "--moss-instruction",
                moss_instruction,
                "--config-dir",
                str(self.config_directory),
                "--models-dir",
                str(self.models_directory),
                "--runs-dir",
                str(self.runs_directory),
                "--audience",
                audience,
            ]
            if tts_model == "kokoro":
                command.extend(["--voice", voice])
            if not moss_use_reference:
                command.append("--moss-direct-voice")
            if voice_reference is not None:
                command.extend(["--voice-reference", voice_reference])
            if tts_model in QWEN_BACKEND_VARIANTS:
                command.extend(["--qwen-language", qwen_language])
                variant = QWEN_BACKEND_VARIANTS[tts_model]
                if variant.startswith("custom-voice"):
                    command.extend(["--qwen-speaker", qwen_speaker])
                elif variant == "voice-design-1.7b":
                    command.extend(["--qwen-voice-design-preset", qwen_voice_design_preset])
                else:
                    assert qwen_reference is not None
                    command.extend(["--qwen-reference", qwen_reference])
            if task.mode == "prompt":
                if meditation_kind is not None:
                    command.extend(["--meditation-kind", meditation_kind])
                command.extend(["--writing-method", writing_method])
                if pacing_profile_id is not None:
                    assert pacing_profile_sha256 is not None
                    command.extend(
                        [
                            "--pacing-profile",
                            pacing_profile_id,
                            "--pacing-profile-version",
                            str(pacing_profile_version),
                            "--pacing-profile-sha256",
                            pacing_profile_sha256,
                        ]
                    )
                command.extend(
                    [
                        text,
                        "--minutes",
                        str(minutes),
                        "--profile",
                        profile,
                    ]
                )
            else:
                input_directory = self.runs_directory / ".web-inputs"
                input_directory.mkdir(parents=True, exist_ok=True)
                input_path = input_directory / f"{task.task_id}.md"
                input_path.write_text(
                    f"[delivery: {delivery}]\n[intensity: {intensity}]\n[pace: {pace}]\n\n{text}\n",
                    encoding="utf-8",
                )
                command.extend(["--script-file", str(input_path), "--profile", "basic"])
                if requested_duration_seconds is not None:
                    command.extend(
                        ["--requested-duration-seconds", str(requested_duration_seconds)]
                    )
            process = self._command_launcher(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self._process_group_options(),
            )
            with self._task_lock:
                task.process = process
                task.launching = False
                cancel_after_launch = task.cancel_requested
            if cancel_after_launch:
                self._stop_process(process)
            stdout, stderr = process.communicate()
            # The CLI owns all durable transitions and errors.  Do not write a
            # competing status from the web process merely because its child
            # exited; the record remains the source of truth.
            with self._task_lock:
                cancelled = task.cancel_requested
            if cancelled:
                self._persist_cancel_intent(task.run_id)
            elif process.returncode not in (0, 130, 143):
                self._persist_process_failure(task.run_id, self._friendly_process_error(stderr))
            _ = stdout
        except OSError as error:
            self._persist_process_failure(task.run_id, f"Could not launch generation: {error}")
        finally:
            if task.resumed_web_launch:
                self._record_resumed_web_launch(task)
            with self._task_lock:
                task.launching = False
                task.process = None
            if input_path is not None:
                with suppress(OSError):
                    input_path.unlink(missing_ok=True)

    def _run_recovery_command(
        self,
        task: GenerationTask,
        command_name: str,
        segment_id: str | None,
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "whoopy",
            "run",
            command_name,
            task.run_id,
        ]
        if segment_id is not None:
            command.append(segment_id)
        command.extend(
            [
                "--json",
                "--config-dir",
                str(self.config_directory),
                "--models-dir",
                str(self.models_directory),
                "--runs-dir",
                str(self.runs_directory),
            ]
        )
        try:
            process = self._command_launcher(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self._process_group_options(),
            )
            with self._task_lock:
                task.process = process
                task.launching = False
                cancel_after_launch = task.cancel_requested
            if cancel_after_launch and command_name != "cancel":
                self._stop_process(process)
            _stdout, stderr = process.communicate()
            with self._task_lock:
                cancelled = task.cancel_requested
            if cancelled:
                self._persist_cancel_intent(task.run_id)
            elif process.returncode not in (0, 130, 143):
                self._persist_process_failure(
                    task.run_id,
                    self._friendly_process_error(stderr),
                )
        except OSError as error:
            self._persist_process_failure(
                task.run_id,
                f"Could not launch {command_name}: {error}",
            )
        finally:
            with self._task_lock:
                task.launching = False
                task.process = None

    @staticmethod
    def _process_group_options() -> dict[str, object]:
        if os.name == "posix":
            return {"start_new_session": True}
        creation_flag = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return {"creationflags": creation_flag} if creation_flag else {}

    @staticmethod
    def _stop_process(process: Any) -> None:
        """Bound cancellation so the HTTP request never waits indefinitely."""

        if process.poll() is not None:
            return
        try:
            if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
                cast(Any, os).killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            return
        except (OSError, ProcessLookupError):
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
                cast(Any, os).killpg(
                    process.pid,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
            else:
                process.kill()
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            return

    def _persist_process_failure(self, run_id: str, message: str) -> None:
        """Keep launcher/CLI failures from becoming permanently queued runs."""

        store = RunStore(self.runs_directory)
        try:
            with RunLock(store.run_directory(run_id)):
                record = store.load(run_id)
                if record.recovery is None or record.status not in (
                    RunStatus.QUEUED,
                    RunStatus.FAILED,
                    RunStatus.INTERRUPTED,
                ):
                    return
                now = datetime.now(UTC)
                attempt_id = uuid4()
                resumed = record.status in (RunStatus.FAILED, RunStatus.INTERRUPTED)
                stage = (
                    RunStage.PLANNING
                    if record.status is RunStatus.QUEUED and record.script_artifact is None
                    else RunStage.COMPILING
                )
                stopped_execution = RunExecution(
                    stage=stage,
                    attempt_id=attempt_id,
                    owner_id=f"web:{socket.gethostname()}:{os.getpid()}",
                    pid=os.getpid(),
                    started_at=now,
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=1),
                    message="The local command could not start.",
                ).finish(message=message or "Local command failed without an error message.")
                failed = record.transition(
                    RunStatus.FAILED,
                    updated_at=now,
                    recovery=record.recovery.model_copy(
                        update={
                            "process_attempts": record.recovery.process_attempts + 1,
                            "resume_count": record.recovery.resume_count + int(resumed),
                            "failed_segment_id": None,
                        }
                    ),
                    execution=stopped_execution,
                    error=message or "Local command failed without an error message.",
                )
                # One atomic record replacement prevents observers from seeing
                # an artificial RUNNING state for a command that never started.
                store.save(failed)
        except (OSError, RunLockUnavailable, RunStoreError, ValueError):
            return

    def _record_resumed_web_launch(self, task: GenerationTask) -> None:
        """Merge pre-launch retry provenance after the CLI owns the final state."""

        store = RunStore(self.runs_directory)
        try:
            with RunLock(store.run_directory(task.run_id)):
                record = store.load(task.run_id)
                if record.recovery is None or record.status in (
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                ):
                    return
                attempts = record.recovery.process_attempts + task.prior_process_attempts
                resumes = record.recovery.resume_count + task.prior_resume_count + 1
                updated_recovery = record.recovery.model_copy(
                    update={
                        "process_attempts": attempts,
                        "resume_count": min(attempts, resumes),
                    }
                )
                updated = RunRecord.model_validate(
                    {
                        **record.model_dump(mode="python"),
                        "updated_at": datetime.now(UTC),
                        "recovery": updated_recovery,
                    }
                )
                store.save(updated)
        except (OSError, RunLockUnavailable, RunStoreError, ValueError):
            return

    def _persist_cancel_intent(self, run_id: str) -> None:
        """Make a completed browser cancellation durably resumable.

        The native child normally records its own SIGTERM interruption.  A
        cancellation can, however, race either the initial claim or an adapter
        failure.  Once ``communicate`` has returned and the run lock is free,
        the browser's explicit cancellation intent wins over those transient
        terminal states.  The previous failure remains visible in the
        execution message and will surface again if it is reproducible on
        resume.
        """

        store = RunStore(self.runs_directory)
        try:
            with RunLock(store.run_directory(run_id)):
                record = store.load(run_id)
                if record.recovery is None or record.status not in (
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                    RunStatus.FAILED,
                ):
                    return
                now = datetime.now(UTC)
                if record.status is RunStatus.QUEUED:
                    recovery = record.recovery.model_copy(
                        update={"process_attempts": record.recovery.process_attempts + 1}
                    )
                    cancel_execution = RunExecution(
                        stage=(
                            RunStage.PLANNING
                            if record.script_artifact is None
                            else RunStage.COMPILING
                        ),
                        interruption_kind="user_cancelled",
                        message=(
                            "Generation was cancelled before the local command started; "
                            "the durable request can be resumed."
                        ),
                    )
                else:
                    active_execution = record.execution
                    if active_execution is None:
                        return
                    prior_failure = (
                        f" The worker had also reported: {record.error}"
                        if record.status is RunStatus.FAILED and record.error
                        else ""
                    )
                    recovery = record.recovery.model_copy(update={"failed_segment_id": None})
                    cancel_execution = active_execution.finish(
                        current_segment_id=active_execution.current_segment_id,
                        interruption_kind="user_cancelled",
                        message=(
                            "Generation was cancelled from Studio; completed checkpoints "
                            f"were kept and the run can be resumed.{prior_failure}"
                        )[:2_000],
                    )
                interrupted = record.transition(
                    RunStatus.INTERRUPTED,
                    updated_at=now,
                    recovery=recovery,
                    execution=cancel_execution,
                )
                store.save(interrupted)
        except (OSError, RunLockUnavailable, RunStoreError, ValueError):
            return

    @staticmethod
    def _friendly_process_error(stderr: str) -> str:
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        if not lines:
            return "Whoopy stopped without an error message."
        message = lines[-1]
        prefix = "whoopy: error: "
        return message[len(prefix) :] if message.startswith(prefix) else message

    def artifact_path(self, run_id: str, artifact: str) -> Path | None:
        """Resolve an allow-listed run artifact without accepting path input."""

        try:
            parsed = UUID(run_id)
        except ValueError:
            return None
        filename = ALLOWED_ARTIFACTS.get(artifact)
        if filename is None:
            return None
        path = self.runs_directory / str(parsed) / filename
        return path if path.is_file() else None

    def audio_path(self, run_id: str) -> Path | None:
        try:
            parsed = UUID(run_id)
        except ValueError:
            return None
        path = self.runs_directory / str(parsed) / "narration.wav"
        return path if path.is_file() else None


def _handler_factory(application: LocalWebApplication) -> type[BaseHTTPRequestHandler]:
    asset_root = files("whoopy.webui").joinpath("static")

    class WhoopyRequestHandler(BaseHTTPRequestHandler):
        server_version = "WhoopyLocal/1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/api/health":
                self._send_json(
                    {
                        "ok": True,
                        "service": "whoopy-local",
                        "api_version": 1,
                        "surfaces": ["studio", "lab"],
                    }
                )
                return
            if path == "/api/status":
                self._send_json(application.environment_status())
                return
            if path == "/api/model-packs":
                try:
                    self._send_json(application.list_model_packs())
                except (OSError, ValueError) as error:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                return
            if path == "/api/runs":
                self._send_json({"runs": application.list_runs()})
                return
            if path.startswith("/api/tasks/"):
                task = application.task(path.removeprefix("/api/tasks/"))
                if task is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Task not found.")
                else:
                    self._send_json(task)
                return
            if path.startswith("/api/runs/"):
                self._serve_run_path(path)
                return
            if path == "/favicon.ico":
                # Browsers request this automatically. An empty success keeps
                # the local log focused on actual application problems.
                self._send_bytes(b"", "image/x-icon", status=HTTPStatus.NO_CONTENT)
                return
            static_name = {"/": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}.get(
                path
            )
            if static_name is None:
                self._send_error(HTTPStatus.NOT_FOUND, "Page not found.")
                return
            asset = asset_root.joinpath(static_name)
            payload = asset.read_bytes()
            content_type = mimetypes.guess_type(static_name)[0] or "application/octet-stream"
            self._send_bytes(payload, content_type)

        def do_POST(self) -> None:
            if not self._origin_is_local():
                self._send_error(HTTPStatus.FORBIDDEN, "Only the local Whoopy page may post.")
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/generate":
                try:
                    payload = self._read_json()
                    task = application.start_generation(payload)
                except ValueError as error:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                    return
                self._send_json(task, status=HTTPStatus.ACCEPTED)
                return
            if parsed.path.startswith("/api/model-packs"):
                self._post_model_pack_action(parsed.path)
                return
            if parsed.path.startswith("/api/runs/"):
                self._post_run_action(parsed.path)
                return
            if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/cancel"):
                # Compatibility with the temporary Phase 3 tester. New code
                # uses the durable run endpoint below.
                task_id = parsed.path.removeprefix("/api/tasks/").removesuffix("/cancel")
                cancelled_task = application.cancel_task(task_id)
                if cancelled_task is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Task not found.")
                else:
                    self._send_json(cancelled_task)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Endpoint not found.")

        def _post_model_pack_action(self, path: str) -> None:
            """Dispatch only the explicit, local model-pack action routes."""

            parts = path.strip("/").split("/")
            action: str
            pack_id: str | None
            if parts == ["api", "model-packs", "unload"]:
                action, pack_id = "unload", None
            elif parts == ["api", "model-packs", "restore"]:
                action, pack_id = "restore", None
            elif len(parts) == 4 and parts[:2] == ["api", "model-packs"]:
                pack_id, action = parts[2], parts[3]
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "Model-pack action not found.")
                return
            if action not in {"install", "verify", "smoke-test", "unload", "remove", "restore"}:
                self._send_error(HTTPStatus.NOT_FOUND, "Model-pack action not found.")
                return
            try:
                payload = self._read_json()
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object.")
                report = application.model_pack_action(action, pack_id, payload)
            except (OSError, ValueError) as error:
                self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                return
            self._send_json(report)

        def _post_run_action(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) < 4 or parts[:2] != ["api", "runs"]:
                self._send_error(HTTPStatus.NOT_FOUND, "Run action not found.")
                return
            run_id = parts[2]
            try:
                UUID(run_id)
            except ValueError:
                self._send_error(HTTPStatus.NOT_FOUND, "Run not found.")
                return
            try:
                payload = self._read_json()
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object.")
                if len(parts) == 4 and parts[3] == "cancel":
                    if payload:
                        raise ValueError("Cancel does not accept request fields.")
                    task = application.cancel_task(run_id)
                elif len(parts) == 4 and parts[3] == "resume":
                    if payload:
                        raise ValueError("Resume does not accept request fields.")
                    task = application.resume_run(run_id)
                elif len(parts) == 6 and parts[3] == "segments" and parts[5] == "regenerate":
                    segment_id = parts[4]
                    if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
                        raise ValueError(
                            "Segment ID must contain only lowercase letters, digits, _ or -."
                        )
                    if payload:
                        raise ValueError("Regenerate does not accept request fields.")
                    task = application.regenerate_segment(run_id, segment_id)
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Run action not found.")
                    return
            except ValueError as error:
                self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                return
            if task is None:
                self._send_error(HTTPStatus.NOT_FOUND, "Run not found.")
            else:
                self._send_json(task, status=HTTPStatus.ACCEPTED)

        def _serve_run_path(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                run = application.run(parts[2])
                if run is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Run not found.")
                else:
                    self._send_json(run)
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "audio":
                audio = application.audio_path(parts[2])
                if audio is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Audio not found.")
                else:
                    self._send_audio(audio)
                return
            if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "artifact":
                artifact = application.artifact_path(parts[2], parts[4])
                if artifact is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Artifact not found.")
                else:
                    content_type = mimetypes.guess_type(artifact.name)[0] or "application/json"
                    self._send_bytes(artifact.read_bytes(), content_type)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Run resource not found.")

        def _send_audio(self, path: Path) -> None:
            """Stream a WAV with byte-range support for browser seek/cancel behavior."""

            size = path.stat().st_size
            start = 0
            end = size - 1
            status = HTTPStatus.OK
            range_header = self.headers.get("Range")
            if range_header:
                try:
                    unit, requested = range_header.split("=", 1)
                    first, last = requested.split("-", 1)
                    if unit.strip().lower() != "bytes" or "," in requested:
                        raise ValueError
                    if first:
                        start = int(first)
                        end = int(last) if last else end
                    elif last:
                        suffix_length = int(last)
                        if suffix_length <= 0:
                            raise ValueError
                        start = max(0, size - suffix_length)
                    if start < 0 or start >= size or end < start:
                        raise ValueError
                    end = min(end, size - 1)
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            try:
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(64 * 1_024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Seeking or closing the browser player legitimately cancels a
                # request. It is not a failed Whoopy generation.
                return

        def _read_json(self) -> Any:
            content_type = self.headers.get_content_type()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("Invalid Content-Length.") from error
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError(f"Request must contain at most {MAX_REQUEST_BYTES} bytes.")
            try:
                return json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Request body must contain valid UTF-8 JSON.") from error

        def _origin_is_local(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                # Native clients do not send Origin. The server is loopback-only,
                # so they retain access without pretending to be a browser page.
                return True
            parsed = urlparse(origin)
            host = self.headers.get("Host", "").lower()
            return (
                parsed.scheme == "http"
                and parsed.username is None
                and parsed.password is None
                and parsed.hostname in ("127.0.0.1", "localhost")
                and parsed.netloc.lower() == host
            )

        def _send_json(
            self,
            payload: Any,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self._send_bytes(
                (json.dumps(payload, separators=(",", ":")) + "\n").encode(),
                "application/json; charset=utf-8",
                status=status,
            )

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"error": message}, status=status)

        def _send_bytes(
            self,
            payload: bytes,
            content_type: str,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self'")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            print(f"[whoopy web] {self.address_string()} {format % args}", file=sys.stderr)

    return WhoopyRequestHandler


def serve(
    *,
    host: str,
    port: int,
    project_root: Path,
    config_directory: Path,
    models_directory: Path,
    runs_directory: Path,
    open_browser: bool = False,
) -> None:
    """Serve the permanent FastAPI application on the local loopback interface."""

    application = LocalWebApplication(
        project_root=project_root,
        config_directory=config_directory,
        models_directory=models_directory,
        runs_directory=runs_directory,
    )
    from whoopy.webui.api import create_api

    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - packaging contract
        raise RuntimeError("Whoopy's local app requires the bundled uvicorn runtime.") from error

    url = f"http://{host}:{port}"
    print("Whoopy local studio")
    print(f"  Open: {url}")
    print("  Privacy: bound to this laptop only; press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        uvicorn.run(
            create_api(application),
            host=host,
            port=port,
            access_log=False,
            log_level="warning",
        )
    except KeyboardInterrupt:
        print("\nStopping Whoopy local studio.")
