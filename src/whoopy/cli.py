"""Command-line entry point for Whoopy's foundation commands."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import yaml

from whoopy.adapters.llm import LlamaCppScriptGenerator, LlamaCppSettings
from whoopy.adapters.tts import (
    MOSS_LANGUAGES,
    QWEN3_TTS_LANGUAGES,
    QWEN3_TTS_SPEAKERS,
    FishSpeech14Adapter,
    FishSpeechSettings,
    MossTTSAdapter,
    MossTTSSettings,
    MossVariant,
    Qwen3TTSAdapter,
    Qwen3TTSSettings,
    Qwen3TTSVariant,
    SherpaOnnxKokoroAdapter,
    SherpaOnnxSettings,
    resolve_qwen3_tts_installation,
)
from whoopy.adapters.tts.mlx_installation import resolve_mlx_qwen_installation
from whoopy.adapters.tts.mlx_qwen3_tts import (
    MLX_QWEN_BACKENDS,
    MlxEvidenceCache,
    MlxEvidenceCheckpoints,
    MlxProcessedSpeech,
    MlxQwenAdapter,
    MlxQwenSettings,
    adapter_source_sha256,
    source_sha256,
)
from whoopy.artifacts import (
    ArtifactError,
    ArtifactInstaller,
    ArtifactSpec,
    ArtifactState,
    ArtifactStore,
    TargetPlatform,
    load_artifact_lock,
)
from whoopy.audio.processing import ProcessedSpeechSynthesizer, SpeechProcessingSettings
from whoopy.config import ConfigError, load_settings
from whoopy.control import LocalControlPlane
from whoopy.evaluation import BakeoffRunner, EvaluationSetError, load_evaluation_set
from whoopy.evaluation.pacing_profiles import (
    PacingProfileRegistry,
    apply_pacing_profile,
)
from whoopy.hardware import DoctorResult, diagnose, inspect_hardware, load_runtime_profiles
from whoopy.locations import SOURCE_PROJECT_ROOT, default_config_directory, runtime_worker_script
from whoopy.meditation import (
    GenerationError,
    GenerationWorkspace,
    LocalMeditationGenerator,
    MeditationPlan,
    load_blueprint_library,
    load_prompt_bundle,
)
from whoopy.meditation.models import DraftedSection, MeditationKind
from whoopy.meditation.prompts import PromptLoadError
from whoopy.meditation.recipes import load_recipe_library
from whoopy.meditation.whole_session import recipe_quiet_limits
from whoopy.meditation.workspace import WorkspaceError
from whoopy.model_packs.operations import ModelPackOperationError
from whoopy.model_packs.resolution import (
    OptionalTTSBackend,
    models_root_from_artifact_store,
    resolve_tts_model_pack,
)
from whoopy.pipeline import (
    GenerationRunSettings,
    LocalWorker,
    PacingRunSettings,
    PendingGenerationConfig,
    RunModelMetadata,
    RunRecord,
    RunStage,
    RunStatus,
    RunStore,
    ScriptRunConfig,
    SegmentCache,
    TTSRunSettings,
    prepare_segment_regeneration,
)
from whoopy.pipeline.acceptance_stage import observe_acceptance_stage
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.revisions import (
    RevisionError,
    RevisionStore,
    validate_semantic_plan_edit,
)
from whoopy.pipeline.runs import RunNotFoundError, RunStoreError
from whoopy.pipeline.worker import WorkerError
from whoopy.ports import AdapterError, SpeechCapabilities, SpeechSynthesizer
from whoopy.timeline import (
    ScriptCompileError,
    SpeechSegment,
    Timeline,
    TimelineSegment,
    build_script_timeline,
)
from whoopy.voice_references import VoiceReferenceManager
from whoopy.voices import KOKORO_ENGLISH_VOICES, kokoro_speaker_id

PROJECT_ROOT = SOURCE_PROJECT_ROOT
GENERATION_HEARTBEAT_SECONDS = 2.0
GENERATION_LEASE_SECONDS = 15.0
QWEN_BACKEND_VARIANTS: dict[str, Qwen3TTSVariant] = {
    "qwen3-custom-voice-0.6b": "custom-voice-0.6b",
    "qwen3-custom-voice-1.7b": "custom-voice-1.7b",
    "qwen3-voice-design-1.7b": "voice-design-1.7b",
    "qwen3-base-clone-0.6b": "base-clone-0.6b",
    "qwen3-base-clone-1.7b": "base-clone-1.7b",
}


class _GenerationHeartbeat:
    """Keep a durable planning/drafting lease alive around blocking LLM calls."""

    def __init__(self, store: RunStore, run_id: UUID, attempt_id: UUID) -> None:
        self.store = store
        self.run_id = run_id
        self.attempt_id = attempt_id
        self._stage: Literal[RunStage.PLANNING, RunStage.DRAFTING] = RunStage.PLANNING
        self._message = "Planning the meditation structure."
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="whoopy-generation-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def set_stage(self, stage: str) -> None:
        """Persist the generator's semantic progress immediately."""

        resolved: Literal[RunStage.PLANNING, RunStage.DRAFTING]
        if stage == "planning":
            resolved = RunStage.PLANNING
            message = "Planning the meditation structure."
        elif stage == "drafting":
            resolved = RunStage.DRAFTING
            message = "Drafting and validating meditation sections."
        else:
            raise ValueError(f"Unsupported generation stage: {stage}")
        with self._lock:
            self._stage = resolved
            self._message = message
            self._renew()
            record = self.store.load(self.run_id)
        observe_acceptance_stage(
            run_id=self.run_id,
            stage=stage,
            durable_run_status=record.status.value,
            durable_run_stage=resolved.value,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=GENERATION_HEARTBEAT_SECONDS * 2)

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RunStoreError(f"Generation heartbeat failed: {self._error}")

    def _run(self) -> None:
        while not self._stop.wait(GENERATION_HEARTBEAT_SECONDS):
            try:
                with self._lock:
                    self._renew()
            except Exception as error:
                self._error = error
                return

    def _renew(self) -> None:
        self.store.update_generation_stage(
            self.run_id,
            attempt_id=self.attempt_id,
            stage=self._stage,
            now=datetime.now(UTC),
            lease_seconds=GENERATION_LEASE_SECONDS,
            message=self._message,
        )


class _AttemptHeartbeat:
    """Keep an existing attempt alive while the next local component is prepared."""

    def __init__(self, store: RunStore, run_id: UUID, attempt_id: UUID) -> None:
        self.store = store
        self.run_id = run_id
        self.attempt_id = attempt_id
        self._stop = threading.Event()
        self._error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run,
            name="whoopy-handoff-heartbeat",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=GENERATION_HEARTBEAT_SECONDS * 2)

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise RunStoreError(f"Generation handoff heartbeat failed: {self._error}")

    def _run(self) -> None:
        while not self._stop.wait(GENERATION_HEARTBEAT_SECONDS):
            try:
                self.store.heartbeat(
                    self.run_id,
                    attempt_id=self.attempt_id,
                    now=datetime.now(UTC),
                    lease_seconds=GENERATION_LEASE_SECONDS,
                )
            except Exception as error:
                self._error = error
                return


@contextmanager
def _interruptible_sigterm() -> Iterator[None]:
    """Turn SIGTERM into a catchable interruption while a durable worker runs."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(_signal_number: int, _frame: object) -> None:
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _persist_unstarted_generation_failure(
    store: RunStore,
    run_id: UUID,
    error: Exception,
) -> None:
    """Make preflight failures visible when the browser already owns a run UUID."""

    try:
        record = store.load(run_id)
        if record.status is not RunStatus.QUEUED:
            return
        started = store.start_generation(
            run_id,
            owner_id=f"{socket.gethostname()}:{os.getpid()}",
            pid=os.getpid(),
            started_at=datetime.now(UTC),
            lease_seconds=GENERATION_LEASE_SECONDS,
        )
        execution = started.execution
        if execution is None or execution.attempt_id is None:
            return
        store.fail_active_run(
            run_id,
            attempt_id=execution.attempt_id,
            error=f"{type(error).__name__}: {error}",
        )
    except (OSError, RunStoreError, ValueError):
        # Preserve the original exception; the lease reconciler remains the
        # fallback if the local disk cannot accept this diagnostic update.
        return


def _persist_unstarted_generation_interruption(store: RunStore, run_id: UUID) -> None:
    """Turn an early Ctrl-C into a resumable durable interruption."""

    try:
        record = store.load(run_id)
        if record.status is not RunStatus.QUEUED:
            return
        started = store.start_generation(
            run_id,
            owner_id=f"{socket.gethostname()}:{os.getpid()}",
            pid=os.getpid(),
            started_at=datetime.now(UTC),
            lease_seconds=GENERATION_LEASE_SECONDS,
        )
        execution = started.execution
        if execution is None or execution.attempt_id is None:
            return
        store.interrupt_active_run(
            run_id,
            attempt_id=execution.attempt_id,
            kind="keyboard_interrupt",
            message="Generation was cancelled before model work completed; it can be resumed.",
        )
    except (OSError, RunStoreError, ValueError):
        return


def _persist_generation_handoff_stop(
    store: RunStore,
    run_id: UUID,
    attempt_id: UUID,
    interruption: BaseException,
) -> None:
    """Finish the generation lease if audio-worker construction stops early."""

    try:
        record = store.load(run_id)
        execution = record.execution
        if (
            record.status is not RunStatus.RUNNING
            or execution is None
            or execution.attempt_id != attempt_id
        ):
            return
        if isinstance(interruption, Exception):
            store.fail_active_run(
                run_id,
                attempt_id=attempt_id,
                error=f"{type(interruption).__name__}: {interruption}",
            )
        else:
            store.interrupt_active_run(
                run_id,
                attempt_id=attempt_id,
                kind=(
                    "keyboard_interrupt"
                    if isinstance(interruption, KeyboardInterrupt)
                    else "signal"
                ),
                message=(
                    "Generation stopped during the audio handoff. The validated script "
                    "was kept and audio processing can be resumed."
                ),
            )
    except (OSError, RunStoreError, ValueError):
        return


def _cancel_active_local_run(store: RunStore, run_id: UUID | str) -> RunRecord:
    """Signal only the live local process proven to own this run.

    The worker lock is intentionally probed rather than acquired: a healthy
    worker holds it for the whole attempt.  If the lock is free, the recorded
    PID may be stale or reused, so cancellation refuses to send any signal.
    """

    parsed_run_id = store.parse_run_id(run_id)
    probe = RunLock(store.run_directory(parsed_run_id))
    try:
        probe.acquire()
    except RunLockUnavailable:
        pass
    else:
        probe.release()
        record = store.reconcile_stale_run(parsed_run_id)
        raise RunStoreError(
            f"Run {record.run_id} has no active worker lock; refusing to signal a PID"
        )

    record = store.load(parsed_run_id)
    execution = record.execution
    now = datetime.now(UTC)
    if (
        record.status is not RunStatus.RUNNING
        or execution is None
        or not execution.is_active
        or execution.pid is None
        or execution.owner_id is None
        or execution.lease_expires_at is None
    ):
        raise RunStoreError(f"Run {record.run_id} has no active local process to cancel")
    if execution.lease_expires_at <= now:
        store.reconcile_stale_run(record.run_id, now=now)
        raise RunStoreError(f"Run {record.run_id} has an expired lease; reconcile and resume it")
    expected_owner = f"{socket.gethostname()}:{execution.pid}"
    if execution.owner_id != expected_owner:
        raise RunStoreError(
            f"Run {record.run_id} is not owned by a verifiable process on this laptop"
        )
    if execution.pid == os.getpid():
        raise RunStoreError("Refusing to send SIGTERM to the cancellation command itself")
    try:
        os.kill(execution.pid, 0)
        os.kill(execution.pid, signal.SIGTERM)
    except ProcessLookupError as error:
        raise RunStoreError(
            f"Run {record.run_id} records PID {execution.pid}, but that process no longer exists"
        ) from error
    except PermissionError as error:
        raise RunStoreError(
            f"Whoopy does not have permission to stop PID {execution.pid}"
        ) from error
    return record


def _add_run_location_arguments(parser: argparse.ArgumentParser) -> None:
    """Give run commands the same configurable storage contract."""

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_directory(),
        help="Directory containing default.yaml and optional local.yaml.",
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        help="Override pipeline.checkpoint_dir for this command.",
    )


def _run_store(args: argparse.Namespace) -> RunStore:
    """Resolve the run root without duplicating config logic in each command."""

    if args.runs_dir is not None:
        return RunStore(args.runs_dir)
    settings = load_settings(args.config_dir)
    return RunStore(settings.pipeline.checkpoint_dir)


def _segment_cache(store: RunStore) -> SegmentCache:
    """Share cached speech across every run stored beneath this root."""

    return SegmentCache(store.root / ".cache" / "segments")


def _add_model_location_arguments(parser: argparse.ArgumentParser) -> None:
    """Give model commands explicit, machine-local storage inputs."""

    parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_directory(),
        help="Directory containing runtime_profiles.yaml.",
    )
    _add_runtime_model_arguments(parser)


def _add_model_pack_location_arguments(parser: argparse.ArgumentParser) -> None:
    """Add explicit locations for optional packs without hiding local state.

    The pack registry is intentionally a checked-in declaration, while the
    downloaded pack bytes belong to the current laptop. These flags make that
    boundary visible to a beginner and keep tests independent from real model
    directories.
    """

    parser.add_argument(
        "--pack-registry",
        type=Path,
        default=default_config_directory() / "model_packs.yaml",
        help="Versioned declaration of the optional voice packs.",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("models"),
        help="Root directory containing managed local model packs.",
    )


def _add_runtime_model_arguments(parser: argparse.ArgumentParser) -> None:
    """Add model paths to commands that already define --config-dir."""

    parser.add_argument(
        "--artifact-lock",
        type=Path,
        help="Override CONFIG_DIR/artifacts.yaml.",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("models/managed"),
        help="Ignored directory for verified downloads and extracted runtimes.",
    )


def _llm_settings_for_writing_method(writing_method: str) -> LlamaCppSettings:
    """Reserve context for the complete draft and a complete holistic revision."""
    return LlamaCppSettings(
        context_tokens=32_768 if writing_method == "recipe_whole_session" else 8_192
    )


def _planning_words_per_minute(backend: str, speed: float, pace: str, writing_method: str) -> int:
    if backend != "kokoro":
        return 133
    if writing_method != "recipe_whole_session":
        return max(80, round(205 * speed))
    # The restored portable Michael control measured 94.8 WPM at 0.6/slow.
    # Treat this as an estimate; the worker still measures real speech duration.
    multiplier = {"natural": 1.0, "unhurried": 0.92, "slow": 0.82, "very_slow": 0.72}[pace]
    return max(80, min(220, round(193 * speed * multiplier)))


def _model_plan(
    args: argparse.Namespace,
    *,
    requested_profile: str | None = None,
) -> tuple[DoctorResult, TargetPlatform, ArtifactStore, list[ArtifactSpec]]:
    """Resolve a safe profile and its exact artifacts without loading a model."""

    lock_path = args.artifact_lock or args.config_dir / "artifacts.yaml"
    artifact_lock = load_artifact_lock(lock_path)
    profiles = [
        profile
        for profile in load_runtime_profiles(args.config_dir / "runtime_profiles.yaml")
        if profile.name in artifact_lock.profiles
    ]
    profile_name = requested_profile or args.profile
    if profile_name != "auto" and profile_name not in artifact_lock.profiles:
        raise ArtifactError(
            f"Profile {profile_name!r} has no pinned artifact plan yet. "
            f"Available plans: {', '.join(sorted(artifact_lock.profiles))}."
        )
    inspection_path = args.models_dir
    while not inspection_path.exists() and inspection_path != inspection_path.parent:
        inspection_path = inspection_path.parent
    snapshot = inspect_hardware(inspection_path)
    result = diagnose(snapshot, profiles, profile_name)
    target = TargetPlatform(
        operating_system=snapshot.operating_system,
        architecture=snapshot.architecture,
    )
    if not result.supported or result.selected_profile is None:
        return result, target, ArtifactStore(args.models_dir), []
    artifacts = artifact_lock.resolve(result.selected_profile.name, target)
    return result, target, ArtifactStore(args.models_dir), artifacts


def _artifact_lock_path(args: argparse.Namespace) -> Path:
    artifact_lock: Path | None = args.artifact_lock
    return artifact_lock or args.config_dir / "artifacts.yaml"


def _model_pack_service(args: argparse.Namespace) -> Any:
    """Create the PR 14 pack facade at the command boundary.

    The CLI intentionally knows no pack-specific download URLs, file layouts,
    or model-runtime imports. ``ManagedModelPacks`` is the small integration
    seam between this user surface and the registry/operation layers. Keeping
    the import here also preserves fast ``whoopy --help`` and legacy profile
    commands on laptops without any optional TTS dependency installed.
    """

    manager_type = import_module("whoopy.model_packs").ManagedModelPacks
    return manager_type.from_paths(
        registry_path=args.pack_registry,
        models_root=args.models_dir,
    )


def _runtime_pack_service(args: argparse.Namespace) -> Any:
    """Create the isolated-runtime installer only at the CLI boundary."""

    manager_type = import_module("whoopy.model_packs").ManagedRuntimePacks
    return manager_type.from_path(args.runtime_registry, models_root=args.models_dir)


def _legacy_cleanup_service(args: argparse.Namespace) -> Any:
    """Create the manifest-constrained legacy-runtime cleanup service."""

    cleanup_type = import_module("whoopy.model_packs").LegacyRuntimeCleanup
    return cleanup_type.from_path(
        args.cleanup_manifest,
        models_root=args.models_dir,
        runs_root=args.runs_dir,
    )


def _model_pack_json(value: Any) -> Any:
    """Turn an operation result into safe JSON without prescribing its model type."""

    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise TypeError("Model-pack operations must return a JSON-compatible report.")


def _print_model_pack_result(value: Any, *, json_output: bool) -> None:
    """Keep automation stable while retaining a readable terminal fallback."""

    payload = _model_pack_json(value)
    if json_output:
        print(json.dumps(payload, indent=2))
        return
    print(yaml.safe_dump(payload, sort_keys=False), end="")


def _qwen_reference_manager(models_dir: Path) -> VoiceReferenceManager:
    models_root = models_root_from_artifact_store(models_dir)
    return VoiceReferenceManager(models_root / "voice-references")


def _qwen_adapter_from_durable(
    durable: TTSRunSettings,
    *,
    models_dir: Path,
    config_dir: Path | None = None,
) -> Qwen3TTSAdapter:
    """Reconstruct exactly the Qwen adapter selected before the run started."""

    variant = QWEN_BACKEND_VARIANTS.get(durable.backend)
    if variant is None:
        raise GenerationError(f"Unsupported durable Qwen backend: {durable.backend}")
    models_root = models_root_from_artifact_store(models_dir)
    installation = resolve_qwen3_tts_installation(
        models_root,
        variant,
        registry_path=None if config_dir is None else config_dir / "model_packs.yaml",
    )
    if (
        installation.model_revision != durable.qwen_model_revision
        or installation.runtime_revision != durable.qwen_runtime_revision
    ):
        raise GenerationError(
            "The local Qwen3-TTS model/runtime revision changed after this run was created. "
            "Restore the recorded revision before resuming."
        )
    reference_audio: Path | None = None
    reference_text: Path | None = None
    consent_scope: Literal["local_voice_cloning_experiment_only"] | None = None
    if variant.startswith("base-clone"):
        assert durable.reference_id is not None
        assert durable.reference_revision is not None
        reference_manager = _qwen_reference_manager(models_dir)
        reference = reference_manager.load_revision(
            durable.reference_id, durable.reference_revision
        )
        if (
            reference.revision_id != durable.reference_revision_id
            or reference.audio_sha256 != durable.reference_audio_sha256
            or reference.transcript_sha256 != durable.reference_transcript_sha256
            or reference.consent.scope != durable.reference_consent_scope
        ):
            raise GenerationError(
                "The consented Qwen voice-reference revision no longer matches this run."
            )
        reference_audio = reference_manager.root / reference.audio_path
        reference_text = reference_manager.root / reference.transcript_path
        consent_scope = reference.consent.scope
    settings = Qwen3TTSSettings(
        runtime_directory=installation.runtime_directory,
        worker_script=runtime_worker_script("qwen3_tts_worker.py"),
        model_directory=installation.model_directory,
        variant=variant,
        model_revision=installation.model_revision,
        runtime_revision=installation.runtime_revision,
        speaker=durable.voice_name,
        language=durable.language,
        voice_design_preset=durable.qwen_voice_design_preset,
        reference_audio=reference_audio,
        reference_text=reference_text,
        consent_scope=consent_scope,
        seed=durable.seed,
        models_root=models_root,
        hardware_requirement=installation.hardware_requirement,
    )
    error = Qwen3TTSAdapter.availability_error(settings)
    if error is not None:
        raise GenerationError(f"{durable.backend} is not ready: {error}.")
    return Qwen3TTSAdapter(settings)


def _mlx_adapter_from_durable(
    durable: TTSRunSettings,
    *,
    models_dir: Path,
    config_dir: Path,
    raw_output_directory: Path,
) -> MlxQwenAdapter:
    """Fail closed if an explicitly experimental run's installed bytes changed."""
    models_root = models_root_from_artifact_store(models_dir)
    installation = resolve_mlx_qwen_installation(
        models_root, durable.backend, manifest_path=config_dir / "experimental_tts.yaml"
    )
    worker = runtime_worker_script("mlx_qwen3_tts_worker.py")
    actual = (
        installation.installation_sha256,
        installation.model_revision,
        installation.runtime_revision,
        source_sha256(worker),
        adapter_source_sha256(),
    )
    expected = (
        durable.experimental_installation_sha256,
        durable.experimental_model_revision,
        durable.experimental_runtime_revision,
        durable.experimental_worker_sha256,
        durable.experimental_adapter_sha256,
    )
    if actual != expected:
        raise GenerationError(
            "Experimental MLX installation or worker changed since this run was saved; "
            "restore the recorded bytes before resuming."
        )
    return MlxQwenAdapter(
        MlxQwenSettings(
            installation=installation,
            worker_script=worker,
            raw_output_directory=raw_output_directory,
            models_root=models_root,
            voice=durable.voice_name,
            language=durable.language,
            seed=durable.seed,
        )
    )


def _managed_reference_paths(
    durable: TTSRunSettings, *, models_dir: Path
) -> tuple[Path, Path] | None:
    """Resolve and checksum-verify a recorded managed reference revision."""

    if durable.reference_id is None:
        return None
    assert durable.reference_revision is not None
    manager = _qwen_reference_manager(models_dir)
    reference = manager.load_revision(durable.reference_id, durable.reference_revision)
    if (
        reference.revision_id != durable.reference_revision_id
        or reference.audio_sha256 != durable.reference_audio_sha256
        or reference.transcript_sha256 != durable.reference_transcript_sha256
        or reference.consent.scope != durable.reference_consent_scope
    ):
        raise GenerationError("The consented voice-reference revision no longer matches this run.")
    return manager.root / reference.audio_path, manager.root / reference.transcript_path


def _real_script_worker(
    store: RunStore,
    record: RunRecord,
    *,
    artifact_lock_path: Path,
    models_dir: Path,
    config_dir: Path | None = None,
    bypass_cache_segment_ids: frozenset[str] | None = None,
) -> LocalWorker:
    """Reconstruct a schema-v4 worker only from durable run configuration."""

    resolved = store.load_resolved_config(record.run_id)
    pack_config_dir = config_dir or artifact_lock_path.parent
    raw_synthesizer: SpeechSynthesizer
    if resolved.tts.backend == "fish-1.4":
        pack = resolve_tts_model_pack(
            "fish-1.4",
            registry_path=pack_config_dir / "model_packs.yaml",
            references_path=pack_config_dir / "voice_references.yaml",
            models_root=models_root_from_artifact_store(models_dir),
            resolve_reference=resolved.tts.reference_id is None,
        )
        reference_paths = _managed_reference_paths(resolved.tts, models_dir=models_dir)
        if reference_paths is None:
            assert pack.reference is not None
            reference_paths = (pack.reference.audio_path, pack.reference.transcript_path)
        raw_synthesizer = FishSpeech14Adapter(
            FishSpeechSettings(
                runtime_directory=pack.runtime_directory,
                worker_script=runtime_worker_script("fish_speech_1_4_worker.py"),
                checkpoint_directory=pack.checkpoint_directory,
                reference_audio=reference_paths[0],
                reference_text=reference_paths[1],
                seed=resolved.tts.seed,
                models_root=models_root_from_artifact_store(models_dir),
                hardware_requirement=pack.hardware_requirement,
            )
        )
    elif resolved.tts.backend in {"moss-local-v1.5", "moss-v1.5"}:
        backend = cast(OptionalTTSBackend, resolved.tts.backend)
        pack = resolve_tts_model_pack(
            backend,
            registry_path=pack_config_dir / "model_packs.yaml",
            references_path=pack_config_dir / "voice_references.yaml",
            models_root=models_root_from_artifact_store(models_dir),
            resolve_reference=resolved.tts.use_reference and resolved.tts.reference_id is None,
        )
        assert pack.codec_directory is not None
        reference_paths = _managed_reference_paths(resolved.tts, models_dir=models_dir)
        raw_synthesizer = MossTTSAdapter(
            MossTTSSettings(
                runtime_directory=pack.runtime_directory,
                worker_script=runtime_worker_script("moss_tts_worker.py"),
                model_directory=pack.checkpoint_directory,
                codec_directory=pack.codec_directory,
                variant=cast(MossVariant, resolved.tts.backend),
                reference_audio=(
                    reference_paths[0]
                    if reference_paths is not None
                    else pack.reference.audio_path
                    if pack.reference is not None
                    else None
                ),
                language=resolved.tts.language,
                instruction=resolved.tts.instruction,
                use_reference=resolved.tts.use_reference,
                seed=resolved.tts.seed,
                models_root=models_root_from_artifact_store(models_dir),
                hardware_requirement=pack.hardware_requirement,
            )
        )
    elif resolved.tts.backend in MLX_QWEN_BACKENDS:
        raw_synthesizer = _mlx_adapter_from_durable(
            resolved.tts,
            models_dir=models_dir,
            config_dir=pack_config_dir,
            raw_output_directory=store.run_directory(record.run_id) / "native-speech",
        )
    elif resolved.tts.backend in QWEN_BACKEND_VARIANTS:
        raw_synthesizer = _qwen_adapter_from_durable(
            resolved.tts, models_dir=models_dir, config_dir=pack_config_dir
        )
    else:
        tts_settings = SherpaOnnxSettings(
            voice_name=resolved.tts.voice_name,
            speaker_id=resolved.tts.speaker_id,
            speed=resolved.tts.speed,
            num_threads=resolved.tts.num_threads,
            provider=resolved.tts.provider,
            language=resolved.tts.language,
        )
        raw_synthesizer = SherpaOnnxKokoroAdapter.from_artifact_store(
            artifact_lock=load_artifact_lock(artifact_lock_path),
            store=ArtifactStore(models_dir),
            profile_name=resolved.profile,
            target=resolved.target,
            settings=tts_settings,
        )
    synthesizer = (
        MlxProcessedSpeech(raw_synthesizer, settings=resolved.processing)
        if isinstance(raw_synthesizer, MlxQwenAdapter)
        else ProcessedSpeechSynthesizer(raw_synthesizer, settings=resolved.processing)
    )
    if resolved.tts.backend == "fish-1.4":
        effective_voice_mode = "reference"
    elif resolved.tts.backend in {"moss-local-v1.5", "moss-v1.5"}:
        effective_voice_mode = "reference" if resolved.tts.use_reference else "designed"
    elif resolved.tts.backend in QWEN_BACKEND_VARIANTS or resolved.tts.backend in MLX_QWEN_BACKENDS:
        effective_voice_mode = resolved.tts.voice_mode
    else:
        effective_voice_mode = "preset"

    def timeline_builder(active_record: RunRecord, created_at: datetime) -> Timeline:
        timeline = build_script_timeline(
            run_id=active_record.run_id,
            script=store.load_script(active_record.run_id),
            created_at=created_at,
            source=(
                "generated_prompt"
                if active_record.source_kind == "generated_prompt"
                else "script_file"
            ),
        )
        timeline = _apply_global_speech_overrides(timeline, resolved.tts)
        if resolved.generation is not None and resolved.generation.pacing is not None:
            registry_path = pack_config_dir / "pacing_profiles.yaml"
            registry = (
                PacingProfileRegistry.load(registry_path)
                if registry_path.is_file()
                else PacingProfileRegistry.load_bundled()
            )
            saved = resolved.generation.pacing
            profile = registry.get(saved.profile_id)
            if profile.version != saved.version or profile.digest != saved.sha256:
                raise GenerationError(
                    "The reviewed pacing profile changed since this run was created."
                )
            quiet_limits = None
            if resolved.generation.writing_method == "recipe_whole_session":
                quiet_limits = recipe_quiet_limits(
                    timeline,
                    MeditationPlan.model_validate_json(
                        store.plan_path(active_record.run_id).read_text(encoding="utf-8")
                    ),
                    [
                        DraftedSection.model_validate_json(path.read_text(encoding="utf-8"))
                        for path in store.draft_sections_path(active_record.run_id).glob("*.json")
                    ],
                )
            timeline = apply_pacing_profile(timeline, profile, quiet_limits=quiet_limits)
        return _apply_speech_capability_fallbacks(
            timeline,
            raw_synthesizer.capabilities,
            voice_mode=effective_voice_mode,
        )

    worker = LocalWorker(
        store,
        cache=(
            MlxEvidenceCache(
                store.run_directory(record.run_id) / "speech-cache",
                store.run_directory(record.run_id) / "native-speech",
            )
            if resolved.tts.backend in MLX_QWEN_BACKENDS
            else _segment_cache(store)
        ),
        synthesizer=synthesizer,
        timeline_builder=timeline_builder,
        bypass_cache_segment_ids=bypass_cache_segment_ids,
    )
    if resolved.tts.backend in MLX_QWEN_BACKENDS:
        worker.checkpoints = MlxEvidenceCheckpoints(store)
    return worker


def _apply_global_speech_overrides(timeline: Timeline, settings: TTSRunSettings) -> Timeline:
    """Apply durable adapter-neutral controls to every spoken timeline segment."""

    changes = {
        key: value
        for key, value in {
            "delivery": settings.delivery_override,
            "intensity": settings.intensity_override,
            "pace": settings.pace_override,
        }.items()
        if value is not None
    }
    if not changes:
        return timeline
    return timeline.model_copy(
        update={
            "segments": [
                segment.model_copy(update=changes)
                if isinstance(segment, SpeechSegment)
                else segment
                for segment in timeline.segments
            ]
        }
    )


def _apply_speech_capability_fallbacks(
    timeline: Timeline,
    capabilities: SpeechCapabilities,
    *,
    voice_mode: str,
) -> Timeline:
    """Persist the controls an adapter will actually apply, never ignored intent."""

    segments: list[TimelineSegment] = []
    selected_voice_mode = (
        voice_mode if voice_mode in capabilities.voice_modes else capabilities.voice_modes[0]
    )
    for segment in timeline.segments:
        if not isinstance(segment, SpeechSegment):
            segments.append(segment)
            continue
        changes: dict[str, object] = {
            "voice_mode": selected_voice_mode,
            "delivery": (
                segment.delivery
                if segment.delivery in capabilities.delivery_styles
                else capabilities.delivery_styles[0]
            ),
            "intensity": (
                segment.intensity
                if segment.intensity in capabilities.intensity_levels
                else capabilities.intensity_levels[0]
            ),
            "pace": (
                segment.pace
                if segment.pace in capabilities.pace_intents
                else capabilities.pace_intents[0]
            ),
            "language": (
                segment.language
                if segment.language in capabilities.languages
                else capabilities.languages[0]
            ),
        }
        if not capabilities.supports_pronunciation_overrides:
            changes["pronunciation_overrides"] = ()
        if not capabilities.supports_duration_control:
            changes["target_duration_ms"] = None
        segments.append(segment.model_copy(update=changes))
    return timeline.model_copy(update={"segments": segments})


def _worker_for_record(
    store: RunStore,
    record: RunRecord,
    *,
    artifact_lock_path: Path,
    models_dir: Path,
    config_dir: Path | None = None,
    bypass_cache_segment_ids: frozenset[str] | None = None,
) -> LocalWorker:
    if record.source_kind in ("script_file", "generated_prompt"):
        return _real_script_worker(
            store,
            record,
            artifact_lock_path=artifact_lock_path,
            config_dir=config_dir,
            models_dir=models_dir,
            bypass_cache_segment_ids=bypass_cache_segment_ids,
        )
    return LocalWorker(
        store,
        cache=_segment_cache(store),
        bypass_cache_segment_ids=bypass_cache_segment_ids,
    )


def _print_run(record: RunRecord, store: RunStore, *, as_json: bool) -> None:
    if as_json:
        print(record.model_dump_json(indent=2))
        return
    print(f"Run: {record.run_id}")
    print(f"Status: {record.status.value}")
    print(f"Prompt: {record.prompt}")
    print(f"Record: {store.record_path(record.run_id)}")
    if record.script_artifact is not None:
        print(f"Script: {store.script_path(record.run_id)}")
    if record.resolved_config_artifact is not None:
        print(f"Resolved config: {store.resolved_config_path(record.run_id)}")
    if record.model_metadata_artifact is not None:
        print(f"Model metadata: {store.model_metadata_path(record.run_id)}")
    if record.plan_artifact is not None:
        print(f"Plan: {store.plan_path(record.run_id)}")
    if record.raw_model_output_artifact is not None:
        print(f"Raw model output: {store.raw_model_output_path(record.run_id)}")
    if record.draft_sections_artifact is not None:
        print(f"Draft sections: {store.draft_sections_path(record.run_id)}")
    if record.timeline_artifact is not None:
        print(f"Timeline: {store.timeline_path(record.run_id)}")
    if record.audio_artifact is not None:
        print(f"Audio: {store.audio_path(record.run_id)}")
    if record.audio_manifest_artifact is not None:
        print(f"Audio manifest: {store.audio_manifest_path(record.run_id)}")
    if record.quality_artifact is not None:
        print(f"Quality report: {store.quality_path(record.run_id)}")
    if record.error is not None:
        print(f"Error: {record.error}")
    if record.recovery is not None:
        recovery = record.recovery
        print(
            "Recovery: "
            f"{recovery.speech_segments_completed}/{recovery.speech_segments_total} speech, "
            f"{recovery.cache_hits} cache hits, "
            f"{recovery.checkpoint_reuses} checkpoint reuses, "
            f"{recovery.resume_count} resumes"
        )


def _pending_generation_resume_arguments(
    args: argparse.Namespace,
    record: RunRecord,
    store: RunStore,
) -> list[str]:
    """Rebuild the exact generate command from immutable pre-LLM settings."""

    pending = store.load_generation_request(record.run_id)
    command = [
        "generate",
        record.prompt,
        "--run-id",
        str(record.run_id),
        "--minutes",
        str(pending.duration_seconds / 60),
        "--profile",
        pending.profile,
        "--seed",
        str(pending.seed),
        "--parallel-sections",
        str(pending.max_parallel_sections),
        "--writing-method",
        pending.writing_method,
        "--tts-model",
        pending.tts.backend,
        "--config-dir",
        str(args.config_dir),
        "--models-dir",
        str(args.models_dir),
        "--runs-dir",
        str(store.root),
        "--audience",
        record.audience,
    ]
    if pending.pacing is not None:
        command.extend(
            [
                "--pacing-profile",
                pending.pacing.profile_id,
                "--pacing-profile-version",
                str(pending.pacing.version),
                "--pacing-profile-sha256",
                pending.pacing.sha256,
            ]
        )
    if pending.meditation_kind is not None:
        command.extend(["--meditation-kind", pending.meditation_kind])
    if pending.tts.backend == "kokoro":
        command.extend(
            [
                "--voice",
                pending.tts.voice_name,
                "--speed",
                str(pending.tts.speed),
            ]
        )
    elif pending.tts.backend.startswith("moss-"):
        command.extend(
            [
                "--moss-language",
                pending.tts.language,
                "--moss-instruction",
                pending.tts.instruction,
            ]
        )
        if not pending.tts.use_reference:
            command.append("--moss-direct-voice")
    elif pending.tts.backend in MLX_QWEN_BACKENDS:
        command.extend(["--qwen-language", pending.tts.language])
        if MLX_QWEN_BACKENDS[pending.tts.backend] == "custom_voice":
            command.extend(["--qwen-speaker", pending.tts.voice_name])
        else:
            command.extend(["--qwen-voice-design-preset", pending.tts.voice_name])
    elif pending.tts.backend in QWEN_BACKEND_VARIANTS:
        command.extend(["--qwen-language", pending.tts.language])
        if pending.tts.backend.startswith("qwen3-custom-voice-"):
            command.extend(["--qwen-speaker", pending.tts.voice_name])
        elif pending.tts.backend == "qwen3-voice-design-1.7b":
            command.extend(["--qwen-voice-design-preset", pending.tts.qwen_voice_design_preset])
        else:
            assert pending.tts.reference_id is not None
            assert pending.tts.reference_revision is not None
            command.extend(
                [
                    "--qwen-reference",
                    pending.tts.reference_id,
                    "--qwen-reference-revision",
                    str(pending.tts.reference_revision),
                ]
            )
    if (
        pending.tts.backend
        in {
            "fish-1.4",
            "moss-local-v1.5",
            "moss-v1.5",
        }
        and pending.tts.reference_id is not None
    ):
        assert pending.tts.reference_revision is not None
        command.extend(
            [
                "--voice-reference",
                pending.tts.reference_id,
                "--voice-reference-revision",
                str(pending.tts.reference_revision),
            ]
        )
    if pending.tts.delivery_override is not None:
        command.extend(["--delivery", pending.tts.delivery_override])
    if pending.tts.intensity_override is not None:
        command.extend(["--intensity", pending.tts.intensity_override])
    if pending.tts.pace_override is not None:
        command.extend(["--pace", pending.tts.pace_override])
    if args.artifact_lock is not None:
        command.extend(["--artifact-lock", str(args.artifact_lock)])
    if args.json:
        command.append("--json")
    return command


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="whoopy",
        description="Local-first, timeline-driven meditation generation.",
    )
    subcommands = parser.add_subparsers(dest="command")

    config_parser = subcommands.add_parser("config", help="Inspect Whoopy configuration.")
    config_commands = config_parser.add_subparsers(dest="config_command")
    show_parser = config_commands.add_parser(
        "show",
        help="Print the resolved configuration after applying all overrides.",
    )
    show_parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_directory(),
        help="Directory containing default.yaml and optional local.yaml.",
    )
    # These flags establish CLI as the highest-priority config layer without
    # pretending that the generation commands from later phases already exist.
    show_parser.add_argument("--llm-backend")
    show_parser.add_argument("--tts-backend")
    show_parser.add_argument("--tts-voice")

    doctor_parser = subcommands.add_parser(
        "doctor",
        help="Inspect this laptop and recommend the highest safe local profile.",
    )
    doctor_parser.add_argument(
        "--config-dir",
        type=Path,
        default=default_config_directory(),
        help="Directory containing runtime_profiles.yaml.",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output for installers and the future UI.",
    )
    doctor_parser.add_argument(
        "--profile",
        choices=("auto", "basic", "lite", "standard", "high", "high", "studio"),
        help="Test a named profile instead of the configured automatic selection.",
    )

    models_parser = subcommands.add_parser(
        "models",
        help="Inspect or install verified local model artifacts.",
    )
    model_commands = models_parser.add_subparsers(dest="models_command")
    models_list_parser = model_commands.add_parser(
        "list",
        help="List locked artifacts for this operating system without loading them.",
    )
    models_list_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable artifact metadata and state.",
    )
    _add_model_location_arguments(models_list_parser)

    models_doctor_parser = model_commands.add_parser(
        "doctor",
        help="Resolve a safe profile and report everything it needs.",
    )
    models_doctor_parser.add_argument(
        "--profile",
        choices=("auto", "basic", "lite", "standard", "high", "high", "studio"),
        default="auto",
        help="Resolve a named profile or choose automatically.",
    )
    models_doctor_parser.add_argument(
        "--verify",
        action="store_true",
        help="Rehash complete downloads, including multi-gigabyte model files.",
    )
    models_doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable hardware, plan, and artifact state.",
    )
    _add_model_location_arguments(models_doctor_parser)

    models_install_parser = model_commands.add_parser(
        "install",
        help="Install one safe profile from verified offline files or HTTPS.",
    )
    models_install_parser.add_argument(
        "--profile",
        choices=("auto", "basic", "lite", "standard", "high", "high", "studio"),
        default="auto",
        help="Install a named profile or choose automatically.",
    )
    models_install_parser.add_argument(
        "--offline-dir",
        type=Path,
        help="Search this directory recursively for already downloaded files.",
    )
    models_install_parser.add_argument(
        "--no-network",
        action="store_true",
        help="Fail instead of downloading an artifact missing from offline storage.",
    )
    models_install_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable install report.",
    )
    _add_model_location_arguments(models_install_parser)

    # ``models pack`` is deliberately separate from the original profile
    # installer above. Profiles answer "what does this laptop need to run a
    # baseline Whoopy flow?"; packs answer "which optional voice experiment
    # should I put on this laptop?" Keeping the nouns distinct makes removal
    # and licensing decisions much harder to make accidentally.
    model_packs_parser = model_commands.add_parser(
        "pack",
        help="Inspect and manage one optional local voice-model pack.",
    )
    model_pack_commands = model_packs_parser.add_subparsers(dest="model_pack_command")
    model_packs_list_parser = model_pack_commands.add_parser(
        "list",
        help="List declared voice packs and their honest local readiness.",
    )
    model_packs_list_parser.add_argument("--json", action="store_true")
    _add_model_pack_location_arguments(model_packs_list_parser)

    model_packs_install_parser = model_pack_commands.add_parser(
        "install",
        help="Install one declared pack with verification and resumable progress.",
    )
    model_packs_install_parser.add_argument(
        "pack_id", help="Declared pack ID from `models pack list`."
    )
    model_packs_install_parser.add_argument(
        "--offline-dir",
        type=Path,
        help="Search this directory for already downloaded pack files first.",
    )
    model_packs_install_parser.add_argument(
        "--no-network",
        action="store_true",
        help="Refuse network downloads; useful while travelling or offline.",
    )
    model_packs_install_parser.add_argument("--json", action="store_true")
    _add_model_pack_location_arguments(model_packs_install_parser)

    model_packs_verify_parser = model_pack_commands.add_parser(
        "verify",
        help="Recheck every file needed by one installed pack without loading it.",
    )
    model_packs_verify_parser.add_argument(
        "pack_id", help="Declared pack ID from `models pack list`."
    )
    model_packs_verify_parser.add_argument("--json", action="store_true")
    _add_model_pack_location_arguments(model_packs_verify_parser)

    model_packs_smoke_parser = model_pack_commands.add_parser(
        "smoke-test",
        help="Run a short, local synthesis readiness check for one installed pack.",
    )
    model_packs_smoke_parser.add_argument(
        "pack_id", help="Declared pack ID from `models pack list`."
    )
    model_packs_smoke_parser.add_argument("--json", action="store_true")
    _add_model_pack_location_arguments(model_packs_smoke_parser)

    model_packs_unload_parser = model_pack_commands.add_parser(
        "unload",
        help="Show the heavyweight slot owner and how its process can be released.",
    )
    model_packs_unload_parser.add_argument("--json", action="store_true")
    _add_model_pack_location_arguments(model_packs_unload_parser)

    model_packs_remove_parser = model_pack_commands.add_parser(
        "remove",
        help="Move one managed pack to Whoopy's recoverable trash.",
    )
    model_packs_remove_parser.add_argument(
        "pack_id", help="Declared pack ID from `models pack list`."
    )
    model_packs_remove_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge that the pack becomes unavailable until restored.",
    )
    model_packs_remove_parser.add_argument("--json", action="store_true")
    _add_model_pack_location_arguments(model_packs_remove_parser)

    model_packs_restore_parser = model_pack_commands.add_parser(
        "restore",
        help="Restore a pack from Whoopy's recoverable trash by removal receipt.",
    )
    model_packs_restore_parser.add_argument(
        "receipt_id", help="Receipt ID returned by `models pack remove`."
    )
    model_packs_restore_parser.add_argument("--json", action="store_true")
    _add_model_pack_location_arguments(model_packs_restore_parser)

    model_runtimes_parser = model_commands.add_parser(
        "runtime",
        help="Inspect or install checksum-pinned isolated voice-model runtimes.",
    )
    model_runtime_commands = model_runtimes_parser.add_subparsers(dest="model_runtime_command")
    for runtime_command, runtime_help in (
        ("list", "List runtime bundles and their truthful availability."),
        ("verify", "Verify one managed isolated runtime without importing it."),
        ("install", "Install one reviewed runtime bundle with resumable transfer."),
    ):
        runtime_parser = model_runtime_commands.add_parser(runtime_command, help=runtime_help)
        if runtime_command != "list":
            runtime_parser.add_argument("runtime_id")
        if runtime_command == "install":
            runtime_parser.add_argument("--offline-dir", type=Path)
            runtime_parser.add_argument("--no-network", action="store_true")
            runtime_parser.add_argument(
                "--python",
                type=Path,
                help="Exact Python interpreter required by a locked-wheelhouse runtime bundle.",
            )
        runtime_parser.add_argument("--json", action="store_true")
        runtime_parser.add_argument(
            "--runtime-registry",
            type=Path,
            default=default_config_directory() / "runtime_packs.yaml",
        )
        runtime_parser.add_argument("--models-dir", type=Path, default=Path("models"))

    model_cleanup_parser = model_commands.add_parser(
        "cleanup",
        help="Inspect or recoverably quarantine allowlisted legacy runtime files.",
    )
    model_cleanup_commands = model_cleanup_parser.add_subparsers(dest="model_cleanup_command")
    for cleanup_command, cleanup_help in (
        ("inspect", "Dry-run the reviewed allowlist and report exact reclaimable bytes."),
        ("apply", "Move the inspected candidates to recoverable local quarantine."),
        ("receipts", "List durable cleanup and restoration receipts."),
        ("restore", "Restore one cleanup receipt after verifying every quarantined byte."),
        ("purge", "Irreversibly delete one verified quarantine payload and retain its receipt."),
    ):
        cleanup_parser = model_cleanup_commands.add_parser(cleanup_command, help=cleanup_help)
        if cleanup_command in {"restore", "purge"}:
            cleanup_parser.add_argument("receipt_id")
        if cleanup_command in {"apply", "restore", "purge"}:
            cleanup_parser.add_argument(
                "--confirm",
                required=True,
                help="Exact confirmation token printed by inspect or the receipt.",
            )
        cleanup_parser.add_argument("--json", action="store_true")
        cleanup_parser.add_argument(
            "--cleanup-manifest",
            type=Path,
            default=default_config_directory() / "legacy_cleanup.yaml",
            help="Checked-in allowlist of disposable legacy runtime paths.",
        )
        cleanup_parser.add_argument(
            "--models-dir",
            type=Path,
            default=Path("models"),
            help="Models root containing experimental legacy runtimes.",
        )
        cleanup_parser.add_argument(
            "--runs-dir",
            type=Path,
            default=Path("runs"),
            help="Run root checked for active generation worker locks.",
        )

    draft_parser = subcommands.add_parser(
        "draft",
        help="Generate a validated local meditation plan, script, and timeline.",
    )
    draft_parser.add_argument("prompt", help="Meditation request sent only to the local model.")
    draft_parser.add_argument(
        "--minutes",
        type=float,
        default=3.0,
        help="Target duration from 1 to 30 minutes (default: 3).",
    )
    draft_parser.add_argument(
        "--profile",
        choices=("auto", "lite", "standard", "high"),
        default="auto",
        help="Choose a local LLM profile or select one safely.",
    )
    draft_parser.add_argument("--seed", type=int, default=42, help="Reproducible model seed.")
    draft_parser.add_argument(
        "--writing-method",
        choices=(
            "plan_first",
            "blueprint_first_sparse",
            "three_candidate_structural_selection",
            "recipe_whole_session",
        ),
        default="plan_first",
        help="Choose legacy section drafting or a coherent recipe-guided whole-session draft.",
    )
    draft_parser.add_argument(
        "--draft-id",
        help="Resume this draft UUID and reuse its validated plan and sections.",
    )
    draft_parser.add_argument(
        "--parallel-sections",
        type=int,
        choices=(1, 2),
        default=1,
        help="Draft one section at a time by default; two requires extra memory.",
    )
    draft_parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("drafts"),
        help="Ignored root for plans, scripts, raw model output, and timelines.",
    )
    draft_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only a machine-readable summary.",
    )
    _add_model_location_arguments(draft_parser)

    evaluate_parser = subcommands.add_parser(
        "evaluate",
        help="Run the versioned local script-model bake-off.",
    )
    evaluate_parser.add_argument(
        "--profiles",
        nargs="+",
        choices=("lite", "standard", "high"),
        default=("lite", "standard", "high"),
        help="Candidates to measure against the same cases.",
    )
    evaluate_parser.add_argument(
        "--evaluation-set",
        type=Path,
        default=default_config_directory() / "evaluation" / "phase-3-5.yaml",
        help="Versioned YAML evaluation fixture.",
    )
    evaluate_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New directory for report.json and per-case artifacts.",
    )
    evaluate_parser.add_argument("--seed", type=int, default=42)
    evaluate_parser.add_argument(
        "--writing-method",
        choices=(
            "plan_first",
            "blueprint_first_sparse",
            "three_candidate_structural_selection",
            "recipe_whole_session",
        ),
        default="plan_first",
        help=(
            "Production drafting strategy recorded for every corpus case; "
            "blueprint-first is the safest Meditation Engine V2 path."
        ),
    )
    _add_model_location_arguments(evaluate_parser)

    generate_parser = subcommands.add_parser(
        "generate",
        help="Create real local speech from a prompt or an authored script.",
    )
    generate_parser.add_argument(
        "prompt",
        nargs="?",
        help="Meditation request for the local LLM; omit when using --script-file.",
    )
    generate_parser.add_argument(
        "--script-file",
        type=Path,
        help="UTF-8 text or Markdown containing prose and [pause: 3s] markers.",
    )
    generate_parser.add_argument(
        "--minutes",
        type=float,
        default=3.0,
        help="Prompt-mode target duration from 1 to 30 minutes (default: 3).",
    )
    generate_parser.add_argument(
        "--requested-duration-seconds",
        type=int,
        help=argparse.SUPPRESS,
    )
    generate_parser.add_argument(
        "--meditation-kind",
        choices=(
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
        ),
        help="Use a reviewed technique blueprint instead of inferring one from the prompt.",
    )
    generate_parser.add_argument(
        "--writing-method",
        choices=(
            "plan_first",
            "blueprint_first_sparse",
            "three_candidate_structural_selection",
            "recipe_whole_session",
        ),
        default="plan_first",
        help="Production drafting strategy; Lab uses this for controlled comparisons.",
    )
    generate_parser.add_argument(
        "--pacing-profile",
        default=None,
        help="Reviewed product pacing profile; its exact revision is saved with the run.",
    )
    generate_parser.add_argument("--pacing-profile-version", type=int, help=argparse.SUPPRESS)
    generate_parser.add_argument("--pacing-profile-sha256", help=argparse.SUPPRESS)
    generate_parser.add_argument("--seed", type=int, default=42, help="Local LLM seed.")
    generate_parser.add_argument(
        "--parallel-sections",
        type=int,
        choices=(1, 2),
        default=1,
        help="Draft one section at a time unless extra memory is available.",
    )
    generate_parser.add_argument(
        "--draft-id",
        help=(
            "Deprecated prompt-mode alias for --run-id; reuse this UUID for "
            "the draft workspace and durable run."
        ),
    )
    generate_parser.add_argument(
        "--run-id",
        help=(
            "Caller-allocated durable run UUID. Used by the local web app so "
            "progress is inspectable before generation finishes."
        ),
    )
    generate_parser.add_argument(
        "--profile",
        choices=("auto", "basic", "lite", "standard", "high"),
        default="auto",
        help="Use Basic for scripts; prompt mode safely selects Lite or Standard.",
    )
    generate_parser.add_argument(
        "--tts-model",
        choices=(
            "kokoro",
            "fish-1.4",
            "moss-local-v1.5",
            "moss-v1.5",
            *QWEN_BACKEND_VARIANTS,
            *MLX_QWEN_BACKENDS,
        ),
        default="kokoro",
        help="Speech model; larger experimental models require local checkpoints.",
    )
    generate_parser.add_argument(
        "--delivery",
        choices=(
            "neutral",
            "warm",
            "calm",
            "soothing",
            "grounded",
            "compassionate",
            "sleepy",
            "encouraging",
        ),
        help="Apply one reviewed typed delivery intent to every spoken block.",
    )
    generate_parser.add_argument(
        "--intensity",
        choices=("subtle", "moderate"),
        help="Apply one typed delivery intensity to every spoken block.",
    )
    generate_parser.add_argument(
        "--moss-language",
        choices=MOSS_LANGUAGES,
        default="English",
        help="Explicit MOSS-TTS language tag (default: English).",
    )
    generate_parser.add_argument(
        "--moss-instruction",
        default="Speak slowly, softly, and warmly, with a meditative delivery.",
        help="Free-form MOSS-TTS delivery instruction.",
    )
    generate_parser.add_argument(
        "--moss-direct-voice",
        action="store_true",
        help="Let MOSS choose a voice instead of cloning Whoopy's slow reference.",
    )
    generate_parser.add_argument(
        "--voice-reference",
        help="Consented managed reference ID for Fish or MOSS reference mode.",
    )
    generate_parser.add_argument(
        "--voice-reference-revision",
        type=int,
        help="Immutable managed Fish/MOSS reference revision used during resume.",
    )
    generate_parser.add_argument(
        "--qwen-language",
        choices=QWEN3_TTS_LANGUAGES,
        default="English",
        help="Explicit Qwen3-TTS language (Lab-only backends).",
    )
    generate_parser.add_argument(
        "--qwen-speaker",
        choices=QWEN3_TTS_SPEAKERS,
        default="Serena",
        help="Preset speaker for Qwen3-TTS CustomVoice.",
    )
    generate_parser.add_argument(
        "--qwen-voice-design-preset",
        choices=("warm_meditation", "grounded_meditation", "sleep_meditation"),
        default="warm_meditation",
        help="Reviewed VoiceDesign profile; arbitrary instructions are not accepted.",
    )
    generate_parser.add_argument(
        "--qwen-reference",
        help="Consented local reference ID required by Qwen3-TTS Base cloning.",
    )
    generate_parser.add_argument(
        "--qwen-reference-revision",
        type=int,
        help="Immutable reference revision used when reconstructing a durable run.",
    )
    generate_parser.add_argument(
        "--voice",
        choices=tuple(sorted(KOKORO_ENGLISH_VOICES)),
        help="Kokoro voice name; defaults to config/default.yaml.",
    )
    generate_parser.add_argument("--speed", type=float, help="Positive Kokoro speech-speed factor.")
    generate_parser.add_argument(
        "--pace",
        choices=("natural", "unhurried", "slow", "very_slow"),
        default="slow",
        help="Adapter-neutral narration pace applied to every spoken segment.",
    )
    generate_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable completed run record.",
    )
    generate_parser.add_argument(
        "--audience",
        choices=("studio", "lab", "system"),
        default="studio",
        help=argparse.SUPPRESS,
    )
    _add_model_location_arguments(generate_parser)
    generate_parser.add_argument(
        "--runs-dir",
        type=Path,
        help="Override pipeline.checkpoint_dir for this command.",
    )

    run_parser = subcommands.add_parser("run", help="Create or inspect durable local runs.")
    run_commands = run_parser.add_subparsers(dest="run_command")
    create_parser = run_commands.add_parser(
        "create",
        help="Save a prompt as a queued run without processing it inline.",
    )
    create_parser.add_argument("prompt", help="Prompt to save in the run record.")
    create_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable run record.",
    )
    _add_run_location_arguments(create_parser)

    run_show_parser = run_commands.add_parser("show", help="Print a saved run record.")
    run_show_parser.add_argument("run_id", help="UUID printed by `whoopy run create`.")
    run_show_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable run record.",
    )
    _add_run_location_arguments(run_show_parser)

    run_resume_parser = run_commands.add_parser(
        "resume",
        help="Resume a failed or interrupted run from verified segment checkpoints.",
    )
    run_resume_parser.add_argument("run_id", help="UUID of a failed or interrupted run.")
    run_resume_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable completed run record.",
    )
    _add_run_location_arguments(run_resume_parser)
    _add_runtime_model_arguments(run_resume_parser)

    run_cancel_parser = run_commands.add_parser(
        "cancel",
        help="Safely request cancellation of this run's verified local worker.",
    )
    run_cancel_parser.add_argument("run_id", help="UUID of an actively running local run.")
    run_cancel_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the durable record that owned the signalled process.",
    )
    _add_run_location_arguments(run_cancel_parser)

    run_reconcile_parser = run_commands.add_parser(
        "reconcile",
        help="Mark expired running leases as safely resumable interruptions.",
    )
    run_reconcile_parser.add_argument(
        "run_id",
        nargs="?",
        help="Optional run UUID; omit to reconcile every saved run.",
    )
    run_reconcile_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only machine-readable reconciled run records.",
    )
    _add_run_location_arguments(run_reconcile_parser)

    run_regenerate_parser = run_commands.add_parser(
        "regenerate-segment",
        help="Rebuild one failed speech segment without discarding the run.",
    )
    run_regenerate_parser.add_argument("run_id", help="UUID of a recoverable run.")
    run_regenerate_parser.add_argument("segment_id", help="Stable speech segment ID.")
    run_regenerate_parser.add_argument("--regeneration-attempt-id", type=UUID)
    run_regenerate_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable completed run record.",
    )
    _add_run_location_arguments(run_regenerate_parser)
    _add_runtime_model_arguments(run_regenerate_parser)

    run_plan_regenerate_parser = run_commands.add_parser(
        "regenerate-plan",
        help="Redraft a reviewed semantic plan edit through the original local LLM context.",
    )
    run_plan_regenerate_parser.add_argument("run_id", help="UUID of a stopped generated run.")
    run_plan_regenerate_parser.add_argument(
        "--plan-file",
        type=Path,
        required=True,
        help="Validated edited meditation-plan JSON.",
    )
    run_plan_regenerate_parser.add_argument(
        "--note",
        default="Reviewed plan regenerated in Whoopy Studio.",
        help="Immutable revision note.",
    )
    run_plan_regenerate_parser.add_argument("--json", action="store_true")
    _add_run_location_arguments(run_plan_regenerate_parser)
    _add_runtime_model_arguments(run_plan_regenerate_parser)

    cache_parser = subcommands.add_parser(
        "cache",
        help="Inspect the local content-addressed speech cache.",
    )
    cache_commands = cache_parser.add_subparsers(dest="cache_command")
    cache_stats_parser = cache_commands.add_parser(
        "stats",
        help="Count valid and corrupt entries without changing the cache.",
    )
    cache_stats_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only machine-readable cache statistics.",
    )
    _add_run_location_arguments(cache_stats_parser)

    worker_parser = subcommands.add_parser(
        "worker",
        help="Process queued work outside the control-plane command.",
    )
    worker_commands = worker_parser.add_subparsers(dest="worker_command")
    process_parser = worker_commands.add_parser(
        "process",
        help="Process one queued run and write its timeline and WAV artifacts.",
    )
    process_parser.add_argument("run_id", help="UUID printed by `whoopy run create`.")
    process_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable completed run record.",
    )
    _add_run_location_arguments(process_parser)
    _add_runtime_model_arguments(process_parser)

    for command, help_text, opens_by_default in (
        ("app", "Start the permanent private local Studio and Lab.", True),
        ("web", "Legacy alias for the private local application.", False),
    ):
        local_app_parser = subcommands.add_parser(command, help=help_text)
        local_app_parser.add_argument(
            "--host",
            choices=("127.0.0.1", "localhost"),
            default="127.0.0.1",
            help="Loopback address to use (default: 127.0.0.1).",
        )
        local_app_parser.add_argument(
            "--port",
            type=int,
            default=8765,
            help="Local TCP port from 1 to 65535 (default: 8765).",
        )
        browser_action = "store_false" if opens_by_default else "store_true"
        local_app_parser.add_argument(
            "--no-open" if opens_by_default else "--open",
            action=browser_action,
            dest="open",
            default=opens_by_default,
            help=(
                "Do not open the Studio in a browser."
                if opens_by_default
                else "Open the interface in the default browser after starting."
            ),
        )
        local_app_parser.add_argument(
            "--config-dir",
            type=Path,
            default=default_config_directory(),
            help="Directory containing Whoopy configuration.",
        )
        local_app_parser.add_argument(
            "--models-dir",
            type=Path,
            default=Path("models/managed"),
            help="Directory containing verified model artifacts.",
        )
        local_app_parser.add_argument(
            "--runs-dir",
            type=Path,
            default=Path("runs"),
            help="Directory containing durable generation runs.",
        )

    return parser


def _cli_overrides(args: argparse.Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for section, field in (
        ("llm", "backend"),
        ("tts", "backend"),
        ("tts", "voice"),
    ):
        value = getattr(args, f"{section}_{field}")
        if value is not None:
            overrides.setdefault(section, {})[field] = value
    return overrides


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "config" and args.config_command == "show":
        try:
            settings = load_settings(args.config_dir, cli_overrides=_cli_overrides(args))
        except ConfigError as error:
            parser.error(str(error))
        print(yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False), end="")
        return 0

    if args.command == "doctor":
        try:
            settings = load_settings(args.config_dir)
            profiles = load_runtime_profiles(args.config_dir / "runtime_profiles.yaml")
            requested_profile = args.profile or settings.hardware.profile
            result = diagnose(inspect_hardware(), profiles, requested_profile)
        except ConfigError as error:
            parser.error(str(error))
        if args.json:
            print(json.dumps(result.model_dump(mode="json"), indent=2))
        else:
            snapshot = result.snapshot
            print("Whoopy native compatibility check")
            print(f"  System: {snapshot.operating_system} {snapshot.architecture}")
            print(f"  CPU threads: {snapshot.cpu_count}")
            print(
                f"  Memory: {snapshot.available_ram_gb:g} GB available / "
                f"{snapshot.total_ram_gb:g} GB total"
            )
            print(f"  Free disk: {snapshot.free_disk_gb:g} GB")
            print(f"  Detected acceleration: {', '.join(snapshot.accelerators)}")
            print(f"  Result: {'supported' if result.supported else 'not currently supported'}")
            if result.selected_profile is not None:
                print(f"  Recommended profile: {result.selected_profile.name}")
            for message in result.messages:
                print(f"  - {message}")
        return 0 if result.supported else 2

    if args.command == "models" and args.models_command == "pack":
        try:
            operation = args.model_pack_command
            # Refuse destructive intent before opening a registry or touching
            # a model directory. This keeps the guard useful even if optional
            # pack support has not yet been configured on a new laptop.
            if operation is None:
                raise ArtifactError("Choose a `whoopy models pack` subcommand.")
            if operation == "remove" and not args.confirm:
                raise ArtifactError("Refusing to remove a pack without --confirm.")
            service = _model_pack_service(args)
            if operation == "list":
                report = service.list()
            elif operation == "install":
                report = service.install(
                    args.pack_id,
                    offline_directory=args.offline_dir,
                    allow_network=not args.no_network,
                )
            elif operation == "verify":
                report = service.verify(args.pack_id)
            elif operation == "smoke-test":
                report = service.smoke_test(args.pack_id)
            elif operation == "unload":
                report = service.unload()
            elif operation == "remove":
                report = service.remove(args.pack_id, confirmed=True)
            elif operation == "restore":
                report = service.restore(args.receipt_id)
            else:
                raise ArtifactError("Choose a `whoopy models pack` subcommand.")
        except (ArtifactError, OSError, ValueError) as error:
            parser.error(str(error))
        _print_model_pack_result(report, json_output=args.json)
        return 0

    if args.command == "models" and args.models_command == "runtime":
        try:
            operation = args.model_runtime_command
            if operation is None:
                raise ArtifactError("Choose a `whoopy models runtime` subcommand.")
            service = _runtime_pack_service(args)
            if operation == "list":
                runtime_report: Any = {"runtime_packs": service.list()}
            elif operation == "verify":
                runtime_report = service.inspect(args.runtime_id)
            elif operation == "install":
                runtime_report = service.install(
                    args.runtime_id,
                    offline_directory=args.offline_dir,
                    allow_network=not args.no_network,
                    python_executable=args.python,
                )
            else:
                raise ArtifactError("Choose a `whoopy models runtime` subcommand.")
        except (ArtifactError, OSError, ValueError) as error:
            parser.error(str(error))
        if isinstance(runtime_report, dict):
            payload = {
                key: [item.model_dump(mode="json") for item in value]
                for key, value in runtime_report.items()
            }
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(yaml.safe_dump(payload, sort_keys=False), end="")
        else:
            _print_model_pack_result(runtime_report, json_output=args.json)
        return 0

    if args.command == "models" and args.models_command == "cleanup":
        try:
            operation = args.model_cleanup_command
            if operation is None:
                raise ArtifactError("Choose a `whoopy models cleanup` subcommand.")
            cleanup = _legacy_cleanup_service(args)
            if operation == "inspect":
                cleanup_report: Any = cleanup.inspect()
            elif operation == "apply":
                cleanup_report = cleanup.apply(confirmation=args.confirm)
            elif operation == "receipts":
                cleanup_report = {"receipts": cleanup.receipts()}
            elif operation == "restore":
                cleanup_report = cleanup.restore(args.receipt_id, confirmation=args.confirm)
            elif operation == "purge":
                cleanup_report = cleanup.purge(args.receipt_id, confirmation=args.confirm)
            else:
                raise ArtifactError("Choose a `whoopy models cleanup` subcommand.")
        except (ArtifactError, OSError, ValueError) as error:
            parser.error(str(error))
        if isinstance(cleanup_report, dict):
            payload = {
                key: [item.model_dump(mode="json") for item in value]
                for key, value in cleanup_report.items()
            }
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(yaml.safe_dump(payload, sort_keys=False), end="")
        else:
            _print_model_pack_result(cleanup_report, json_output=args.json)
        return 0

    if args.command == "models" and args.models_command == "list":
        try:
            lock_path = args.artifact_lock or args.config_dir / "artifacts.yaml"
            artifact_lock = load_artifact_lock(lock_path)
            target = TargetPlatform.current()
            artifact_store = ArtifactStore(args.models_dir)
            artifacts = [
                artifact for artifact in artifact_lock.artifacts if artifact.supports(target)
            ]
            statuses = [artifact_store.inspect(artifact) for artifact in artifacts]
        except ArtifactError as error:
            parser.error(str(error))
        if args.json:
            print(
                json.dumps(
                    {
                        "target": target.model_dump(mode="json"),
                        "artifacts": [status.model_dump(mode="json") for status in statuses],
                    },
                    indent=2,
                )
            )
        else:
            print(f"Whoopy locked artifacts for {target.operating_system} {target.architecture}")
            for status in statuses:
                print(
                    f"  [{status.state.value:9}] {status.artifact_id} "
                    f"({status.license_id}, {status.size_bytes} bytes)"
                )
        return 0

    if args.command == "models" and args.models_command == "doctor":
        try:
            result, target, artifact_store, artifacts = _model_plan(args)
            statuses = [
                artifact_store.inspect(artifact, verify_digest=args.verify)
                for artifact in artifacts
            ]
        except (ArtifactError, ConfigError) as error:
            parser.error(str(error))
        if args.json:
            print(
                json.dumps(
                    {
                        "hardware": result.model_dump(mode="json"),
                        "target": target.model_dump(mode="json"),
                        "artifacts": [status.model_dump(mode="json") for status in statuses],
                        "ready": bool(statuses)
                        and all(status.state is ArtifactState.INSTALLED for status in statuses),
                        "loaded_models": False,
                    },
                    indent=2,
                )
            )
        else:
            print("Whoopy model compatibility check")
            print(f"  Target: {target.operating_system} {target.architecture}")
            print(f"  Hardware: {'supported' if result.supported else 'not supported'}")
            if result.selected_profile is not None:
                print(f"  Profile: {result.selected_profile.name}")
            for message in result.messages:
                print(f"  - {message}")
            for status in statuses:
                print(f"  [{status.state.value:9}] {status.display_name}")
            print("  No model was loaded.")
        if not result.supported:
            return 2
        return (
            0
            if statuses and all(status.state is ArtifactState.INSTALLED for status in statuses)
            else 1
        )

    if args.command == "models" and args.models_command == "install":
        try:
            result, target, artifact_store, artifacts = _model_plan(args)
            if not result.supported or result.selected_profile is None:
                for message in result.messages:
                    print(message)
                return 2
            profile_name = cast(
                Literal["basic", "lite", "standard", "high"],
                result.selected_profile.name,
            )
            callback = None if args.json else lambda message: print(f"  {message}")
            if not args.json:
                print(
                    f"Installing Whoopy {result.selected_profile.name} artifacts for "
                    f"{target.operating_system} {target.architecture}"
                )
            report = ArtifactInstaller(
                artifact_store,
                status_callback=callback,
            ).install_profile(
                result.selected_profile.name,
                artifacts,
                target,
                offline_directory=args.offline_dir,
                allow_network=not args.no_network,
            )
        except (ArtifactError, ConfigError) as error:
            parser.error(str(error))
        if args.json:
            print(report.model_dump_json(indent=2))
        else:
            print(
                f"Ready: {len(report.artifacts)} artifacts "
                f"({len(report.installed)} installed, {len(report.reused)} reused)"
            )
            print("No model was loaded.")
        return 0

    if args.command == "draft":
        try:
            duration_seconds = round(args.minutes * 60)
            if not 60 <= duration_seconds <= 1_800:
                raise GenerationError("--minutes must be between 1 and 30")
            result, target, artifact_store, _artifacts = _model_plan(args)
            if not result.supported or result.selected_profile is None:
                for message in result.messages:
                    print(message)
                return 2
            profile_name = cast(
                Literal["lite", "standard", "high"],
                result.selected_profile.name,
            )
            if args.profile == "auto" and profile_name == "lite":
                raise GenerationError(
                    "The v1 bake-off did not qualify Lite for automatic drafting. "
                    "Use Standard, Basic script mode, or explicitly request "
                    "--profile lite for experimentation."
                )
            if profile_name not in ("lite", "standard", "high"):
                raise GenerationError("Drafting requires the Lite, Standard, or High LLM profile.")
            artifact_lock = load_artifact_lock(_artifact_lock_path(args))
            adapter = LlamaCppScriptGenerator.from_artifact_store(
                artifact_lock=artifact_lock,
                store=artifact_store,
                profile_name=profile_name,
                target=target,
                device=",".join(result.snapshot.accelerators),
                settings=_llm_settings_for_writing_method(args.writing_method),
            )
            run_id = UUID(args.draft_id) if args.draft_id is not None else uuid4()
            workspace = GenerationWorkspace(args.output_dir / str(run_id))
            generated = LocalMeditationGenerator(
                adapter,
                load_prompt_bundle(args.config_dir / "prompts"),
                blueprints=load_blueprint_library(args.config_dir / "meditation_blueprints.yaml"),
                recipes=(
                    load_recipe_library(args.config_dir / "session_recipes.yaml")
                    if args.writing_method == "recipe_whole_session"
                    else None
                ),
                max_parallel_sections=args.parallel_sections,
                workspace=workspace,
            ).generate(
                prompt=args.prompt,
                duration_seconds=duration_seconds,
                run_id=run_id,
                seed=args.seed,
                writing_method=args.writing_method,
            )
        except (
            AdapterError,
            ArtifactError,
            ConfigError,
            GenerationError,
            PromptLoadError,
            WorkspaceError,
            ValueError,
        ) as error:
            parser.error(str(error))
        if args.json:
            print(
                json.dumps(
                    {
                        "draft_id": str(run_id),
                        "directory": str(workspace.root),
                        "profile": profile_name,
                        "estimated_duration_seconds": generated.estimated_duration_seconds,
                        "sections": len(generated.sections),
                    },
                    indent=2,
                )
            )
        else:
            print(f"Draft: {run_id}")
            print(f"Profile: {profile_name}")
            print(f"Directory: {workspace.root}")
            print(f"Plan: {workspace.root / 'plan.json'}")
            print(f"Script: {workspace.root / 'script.md'}")
            print(f"Timeline: {workspace.root / 'timeline.json'}")
            print(f"Estimated duration: {generated.estimated_duration_seconds:.1f} seconds")
        return 0

    if args.command == "evaluate":
        try:
            report_path = args.output_dir / "report.json"
            if report_path.exists():
                raise EvaluationSetError(
                    f"Refusing to replace existing bake-off report: {report_path}"
                )
            evaluation_set = load_evaluation_set(args.evaluation_set)
            prompts = load_prompt_bundle(args.config_dir / "prompts")
            runner = BakeoffRunner(
                evaluation_set=evaluation_set,
                prompts=prompts,
                blueprints=load_blueprint_library(args.config_dir / "meditation_blueprints.yaml"),
                recipes=(
                    load_recipe_library(args.config_dir / "session_recipes.yaml")
                    if args.writing_method == "recipe_whole_session"
                    else None
                ),
                output_directory=args.output_dir,
                seed=args.seed,
                writing_method=args.writing_method,
            )
            candidates: dict[str, LlamaCppScriptGenerator] = {}
            all_results = []
            platform_name = ""
            for requested in args.profiles:
                result, target, artifact_store, artifacts = _model_plan(
                    args,
                    requested_profile=requested,
                )
                if not result.supported or result.selected_profile is None:
                    raise EvaluationSetError(
                        f"Profile {requested} is unsafe on this machine: "
                        + "; ".join(result.messages)
                    )
                profile_name = cast(Literal["lite", "standard", "high"], requested)
                component = {
                    "lite": "llm_model_lite",
                    "standard": "llm_model_standard",
                    "high": "llm_model_high",
                }[profile_name]
                model_artifacts = [
                    artifact for artifact in artifacts if artifact.component == component
                ]
                if len(model_artifacts) != 1:
                    raise EvaluationSetError(
                        f"Expected one {component} artifact, found {len(model_artifacts)}."
                    )
                adapter = LlamaCppScriptGenerator.from_artifact_store(
                    artifact_lock=load_artifact_lock(_artifact_lock_path(args)),
                    store=artifact_store,
                    profile_name=profile_name,
                    target=target,
                    device=",".join(result.snapshot.accelerators),
                    settings=_llm_settings_for_writing_method(args.writing_method),
                )
                candidates[profile_name] = adapter
                platform_name = f"{target.operating_system}-{target.architecture}"
                print(
                    f"Evaluating {profile_name}: {len(evaluation_set.cases)} cases...",
                    file=sys.stderr,
                )
                all_results.extend(
                    runner.run_candidate(
                        profile=profile_name,
                        adapter=adapter,
                        model_artifact_bytes=model_artifacts[0].size_bytes,
                    )
                )
            bakeoff_report = runner.report(
                platform=platform_name,
                candidates=candidates,
                results=all_results,
            )
            report_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = report_path.with_name(f".{report_path.name}.{uuid4().hex}.tmp")
            temporary.write_text(
                bakeoff_report.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(report_path)
            print(report_path)
        except (
            AdapterError,
            ArtifactError,
            ConfigError,
            EvaluationSetError,
            GenerationError,
            PromptLoadError,
            ValueError,
            WorkspaceError,
        ) as error:
            parser.error(str(error))
        return 0

    if args.command == "generate":
        active_run_id: UUID | None = None
        active_store: RunStore | None = None
        try:
            if (args.prompt is None) == (args.script_file is None):
                raise GenerationError("Provide either a prompt or --script-file, but not both.")
            if args.script_file is not None and args.draft_id is not None:
                raise GenerationError("--draft-id is available only in prompt mode.")
            if args.speed is not None and args.tts_model != "kokoro":
                raise GenerationError(
                    f"{args.tts_model} does not support numeric --speed; "
                    "numeric speed is available only for Kokoro."
                )
            if (
                args.run_id is not None
                and args.draft_id is not None
                and args.run_id != args.draft_id
            ):
                raise GenerationError("--run-id and the deprecated --draft-id must match.")
            settings = load_settings(args.config_dir)
            prompt_mode = args.prompt is not None
            if prompt_mode and args.requested_duration_seconds is not None:
                raise GenerationError(
                    "--requested-duration-seconds is available only with --script-file."
                )
            if args.requested_duration_seconds is not None and not (
                60 <= args.requested_duration_seconds <= 1_800
            ):
                raise GenerationError("--requested-duration-seconds must be between 60 and 1800")
            if args.prompt is not None:
                args.prompt = args.prompt.strip()
            requested_profile = args.profile if prompt_mode or args.profile != "auto" else "basic"
            result, target, artifact_store, _artifacts = _model_plan(
                args,
                requested_profile=requested_profile,
            )
            if not result.supported or result.selected_profile is None:
                for message in result.messages:
                    print(message)
                return 2
            profile_name = cast(
                Literal["basic", "lite", "standard", "high"],
                result.selected_profile.name,
            )
            if prompt_mode and profile_name == "basic":
                raise GenerationError("Prompt mode requires the Lite or Standard profile.")
            if prompt_mode and args.profile == "auto" and profile_name == "lite":
                raise GenerationError(
                    "The v1 bake-off did not qualify Lite for automatic prompt mode. "
                    "Use an authored --script-file on this laptop, or explicitly "
                    "request --profile lite for experimentation."
                )
            requested_run_id = args.run_id or args.draft_id
            run_id = UUID(requested_run_id) if requested_run_id is not None else uuid4()
            active_run_id = run_id
            created_at = datetime.now(UTC)
            store = _run_store(args)
            active_store = store
            placeholder_prompt = (
                args.prompt
                if args.prompt is not None
                else f"Render local script: {args.script_file.name}"
            )
            try:
                placeholder = store.load(run_id)
            except RunNotFoundError:
                placeholder = store.create(
                    placeholder_prompt,
                    run_id=run_id,
                    created_at=created_at,
                    audience=args.audience,
                )
            if placeholder.audience != args.audience:
                raise GenerationError(
                    f"Run {run_id} belongs to the {placeholder.audience} audience, not "
                    f"{args.audience}; resume or regenerate it through its owning surface."
                )
            created_at = placeholder.created_at
            artifact_lock = load_artifact_lock(_artifact_lock_path(args))
            voice_name = args.voice or settings.tts.voice
            pacing_path = args.config_dir / "pacing_profiles.yaml"
            pacing_registry = (
                PacingProfileRegistry.load(pacing_path)
                if pacing_path.is_file()
                else PacingProfileRegistry.load_bundled()
            )
            pacing_profile = pacing_registry.get(args.pacing_profile or "meditative-v2")
            if (
                args.pacing_profile_version is not None
                and args.pacing_profile_version != pacing_profile.version
            ):
                raise GenerationError("The requested pacing profile version is no longer active.")
            if (
                args.pacing_profile_sha256 is not None
                and args.pacing_profile_sha256 != pacing_profile.digest
            ):
                raise GenerationError("The requested pacing profile content changed before launch.")
            use_pacing_profile = args.pacing_profile is not None or args.speed is None
            pacing_run = (
                PacingRunSettings(
                    profile_id=pacing_profile.profile_id,
                    version=pacing_profile.version,
                    sha256=pacing_profile.digest,
                )
                if use_pacing_profile
                else None
            )
            speed = (
                pacing_profile.speech_speed
                if prompt_mode and use_pacing_profile
                else args.speed
                if args.speed is not None
                else settings.tts.speed
            )
            if not 0.5 <= speed <= 1.2:
                raise GenerationError("--speed must be between 0.5 and 1.2")
            managed_reference = None
            if args.voice_reference is not None:
                if args.tts_model not in {"fish-1.4", "moss-local-v1.5", "moss-v1.5"}:
                    raise GenerationError(
                        "--voice-reference is accepted only by Fish or MOSS reference mode."
                    )
                if args.moss_direct_voice:
                    raise GenerationError("MOSS direct voice mode cannot use --voice-reference.")
                manager = _qwen_reference_manager(args.models_dir)
                managed_reference = (
                    manager.load(args.voice_reference)
                    if args.voice_reference_revision is None
                    else manager.load_revision(args.voice_reference, args.voice_reference_revision)
                )
            elif args.voice_reference_revision is not None:
                raise GenerationError("--voice-reference-revision requires --voice-reference.")
            raw_synthesizer: SpeechSynthesizer
            if args.tts_model == "fish-1.4":
                pack = resolve_tts_model_pack(
                    "fish-1.4",
                    registry_path=args.config_dir / "model_packs.yaml",
                    references_path=args.config_dir / "voice_references.yaml",
                    models_root=models_root_from_artifact_store(args.models_dir),
                    resolve_reference=managed_reference is None,
                )
                if managed_reference is None:
                    assert pack.reference is not None
                    fish_reference_audio = pack.reference.audio_path
                    fish_reference_text = pack.reference.transcript_path
                else:
                    fish_reference_audio = manager.root / managed_reference.audio_path
                    fish_reference_text = manager.root / managed_reference.transcript_path
                fish_error = FishSpeech14Adapter.availability_error(
                    pack.runtime_directory,
                    pack.checkpoint_directory,
                    fish_reference_audio,
                    fish_reference_text,
                )
                if fish_error is not None:
                    raise GenerationError(f"Fish Speech 1.4 is not ready: {fish_error}.")
                raw_synthesizer = FishSpeech14Adapter(
                    FishSpeechSettings(
                        runtime_directory=pack.runtime_directory,
                        worker_script=runtime_worker_script("fish_speech_1_4_worker.py"),
                        checkpoint_directory=pack.checkpoint_directory,
                        reference_audio=fish_reference_audio,
                        reference_text=fish_reference_text,
                        seed=args.seed,
                        models_root=models_root_from_artifact_store(args.models_dir),
                        hardware_requirement=pack.hardware_requirement,
                    )
                )
                durable_tts = TTSRunSettings(
                    backend="fish-1.4",
                    voice_name="whoopy-reference",
                    speaker_id=0,
                    speed=1.0,
                    num_threads=1,
                    provider="auto",
                    language="en",
                    seed=args.seed,
                    delivery_override=args.delivery,
                    intensity_override=args.intensity,
                    pace_override=args.pace,
                    voice_mode="reference",
                    reference_id=(
                        None if managed_reference is None else managed_reference.reference_id
                    ),
                    reference_revision=(
                        None if managed_reference is None else managed_reference.revision
                    ),
                    reference_revision_id=(
                        None if managed_reference is None else managed_reference.revision_id
                    ),
                    reference_audio_sha256=(
                        None if managed_reference is None else managed_reference.audio_sha256
                    ),
                    reference_transcript_sha256=(
                        None if managed_reference is None else managed_reference.transcript_sha256
                    ),
                    reference_consent_scope=(
                        None if managed_reference is None else managed_reference.consent.scope
                    ),
                )
            elif args.tts_model in {"moss-local-v1.5", "moss-v1.5"}:
                backend = cast(OptionalTTSBackend, args.tts_model)
                pack = resolve_tts_model_pack(
                    backend,
                    registry_path=args.config_dir / "model_packs.yaml",
                    references_path=args.config_dir / "voice_references.yaml",
                    models_root=models_root_from_artifact_store(args.models_dir),
                    resolve_reference=not args.moss_direct_voice and managed_reference is None,
                )
                assert pack.codec_directory is not None
                moss_reference_audio = (
                    manager.root / managed_reference.audio_path
                    if managed_reference is not None
                    else pack.reference.audio_path
                    if pack.reference is not None
                    else None
                )
                moss_error = MossTTSAdapter.availability_error(
                    pack.runtime_directory,
                    pack.checkpoint_directory,
                    pack.codec_directory,
                    moss_reference_audio,
                    use_reference=not args.moss_direct_voice,
                )
                if moss_error is not None:
                    raise GenerationError(f"{args.tts_model} is not ready: {moss_error}.")
                raw_synthesizer = MossTTSAdapter(
                    MossTTSSettings(
                        runtime_directory=pack.runtime_directory,
                        worker_script=runtime_worker_script("moss_tts_worker.py"),
                        model_directory=pack.checkpoint_directory,
                        codec_directory=pack.codec_directory,
                        variant=cast(MossVariant, args.tts_model),
                        reference_audio=moss_reference_audio,
                        language=args.moss_language,
                        instruction=args.moss_instruction,
                        use_reference=not args.moss_direct_voice,
                        seed=args.seed,
                        models_root=models_root_from_artifact_store(args.models_dir),
                        hardware_requirement=pack.hardware_requirement,
                    )
                )
                durable_tts = TTSRunSettings(
                    backend=args.tts_model,
                    voice_name=(
                        "direct-generated" if args.moss_direct_voice else "whoopy-reference"
                    ),
                    speaker_id=0,
                    speed=1.0,
                    num_threads=1,
                    provider="mps",
                    language=args.moss_language,
                    seed=args.seed,
                    instruction=args.moss_instruction,
                    use_reference=not args.moss_direct_voice,
                    delivery_override=args.delivery,
                    intensity_override=args.intensity,
                    pace_override=args.pace,
                    voice_mode=("reference" if not args.moss_direct_voice else "designed"),
                    reference_id=(
                        None if managed_reference is None else managed_reference.reference_id
                    ),
                    reference_revision=(
                        None if managed_reference is None else managed_reference.revision
                    ),
                    reference_revision_id=(
                        None if managed_reference is None else managed_reference.revision_id
                    ),
                    reference_audio_sha256=(
                        None if managed_reference is None else managed_reference.audio_sha256
                    ),
                    reference_transcript_sha256=(
                        None if managed_reference is None else managed_reference.transcript_sha256
                    ),
                    reference_consent_scope=(
                        None if managed_reference is None else managed_reference.consent.scope
                    ),
                )
            elif args.tts_model in MLX_QWEN_BACKENDS:
                if args.qwen_reference is not None or args.qwen_reference_revision is not None:
                    raise GenerationError("Experimental MLX Qwen does not accept voice references")
                mlx_installation = resolve_mlx_qwen_installation(
                    models_root_from_artifact_store(args.models_dir),
                    args.tts_model,
                    manifest_path=args.config_dir / "experimental_tts.yaml",
                )
                worker_script = runtime_worker_script("mlx_qwen3_tts_worker.py")
                custom_voice = MLX_QWEN_BACKENDS[args.tts_model] == "custom_voice"
                durable_tts = TTSRunSettings(
                    backend=args.tts_model,
                    voice_name=args.qwen_speaker if custom_voice else args.qwen_voice_design_preset,
                    speaker_id=0,
                    speed=1.0,
                    num_threads=1,
                    provider="mlx-metal",
                    language=args.qwen_language,
                    seed=args.seed,
                    use_reference=False,
                    voice_mode="preset" if custom_voice else "designed",
                    experimental_installation_sha256=mlx_installation.installation_sha256,
                    experimental_model_revision=mlx_installation.model_revision,
                    experimental_runtime_revision=mlx_installation.runtime_revision,
                    experimental_worker_sha256=source_sha256(worker_script),
                    experimental_adapter_sha256=adapter_source_sha256(),
                    experimental_classification="experimental_local",
                    experimental_managed_ready=False,
                    qwen_voice_design_preset=args.qwen_voice_design_preset,
                    delivery_override=args.delivery,
                    intensity_override=args.intensity,
                    pace_override=args.pace,
                )
                raw_synthesizer = MlxQwenAdapter(
                    MlxQwenSettings(
                        installation=mlx_installation,
                        worker_script=worker_script,
                        raw_output_directory=store.run_directory(run_id) / "native-speech",
                        models_root=models_root_from_artifact_store(args.models_dir),
                        voice=durable_tts.voice_name,
                        language=durable_tts.language,
                        seed=args.seed,
                    )
                )
            elif args.tts_model in QWEN_BACKEND_VARIANTS:
                variant = QWEN_BACKEND_VARIANTS[args.tts_model]
                installation = resolve_qwen3_tts_installation(
                    models_root_from_artifact_store(args.models_dir),
                    variant,
                    registry_path=args.config_dir / "model_packs.yaml",
                )
                is_clone = variant.startswith("base-clone")
                if is_clone and args.qwen_reference is None:
                    raise GenerationError(
                        f"{args.tts_model} requires --qwen-reference with explicit consent."
                    )
                if not is_clone and (
                    args.qwen_reference is not None or args.qwen_reference_revision is not None
                ):
                    raise GenerationError(
                        "Only Qwen3-TTS Base cloning accepts --qwen-reference controls."
                    )
                reference = None
                if is_clone:
                    manager = _qwen_reference_manager(args.models_dir)
                    reference = (
                        manager.load(args.qwen_reference)
                        if args.qwen_reference_revision is None
                        else manager.load_revision(
                            args.qwen_reference, args.qwen_reference_revision
                        )
                    )
                voice_mode: Literal["preset", "reference", "designed"]
                if variant.startswith("custom-voice"):
                    voice_mode = "preset"
                    qwen_voice_name = args.qwen_speaker
                elif variant == "voice-design-1.7b":
                    voice_mode = "designed"
                    qwen_voice_name = args.qwen_voice_design_preset
                else:
                    assert reference is not None
                    voice_mode = "reference"
                    qwen_voice_name = reference.reference_id
                durable_tts = TTSRunSettings(
                    backend=args.tts_model,
                    voice_name=qwen_voice_name,
                    speaker_id=0,
                    speed=1.0,
                    num_threads=1,
                    provider="auto",
                    language=args.qwen_language,
                    seed=args.seed,
                    use_reference=is_clone,
                    voice_mode=voice_mode,
                    qwen_model_revision=installation.model_revision,
                    qwen_runtime_revision=installation.runtime_revision,
                    qwen_voice_design_preset=args.qwen_voice_design_preset,
                    delivery_override=args.delivery,
                    intensity_override=args.intensity,
                    pace_override=args.pace,
                    reference_id=None if reference is None else reference.reference_id,
                    reference_revision=None if reference is None else reference.revision,
                    reference_revision_id=None if reference is None else reference.revision_id,
                    reference_audio_sha256=(None if reference is None else reference.audio_sha256),
                    reference_transcript_sha256=(
                        None if reference is None else reference.transcript_sha256
                    ),
                    reference_consent_scope=(
                        None if reference is None else reference.consent.scope
                    ),
                )
                raw_synthesizer = _qwen_adapter_from_durable(
                    durable_tts, models_dir=args.models_dir, config_dir=args.config_dir
                )
            else:
                tts_settings = SherpaOnnxSettings(
                    voice_name=voice_name,
                    speaker_id=kokoro_speaker_id(voice_name),
                    speed=speed,
                    num_threads=2,
                    provider="cpu",
                    language="en-us",
                )
                raw_synthesizer = SherpaOnnxKokoroAdapter.from_artifact_store(
                    artifact_lock=artifact_lock,
                    store=artifact_store,
                    profile_name=profile_name,
                    target=target,
                    settings=tts_settings,
                )
                durable_tts = TTSRunSettings(
                    backend="kokoro",
                    voice_name=tts_settings.voice_name,
                    speaker_id=tts_settings.speaker_id,
                    speed=tts_settings.speed,
                    num_threads=tts_settings.num_threads,
                    provider=tts_settings.provider,
                    language=tts_settings.language,
                    seed=args.seed,
                    delivery_override=args.delivery,
                    intensity_override=args.intensity,
                    pace_override=args.pace,
                )
            processing = SpeechProcessingSettings()
            generation_attempt_id: UUID | None = None
            handoff_heartbeat: _AttemptHeartbeat | None = None

            if prompt_mode:
                assert args.prompt is not None
                if placeholder.prompt != args.prompt:
                    raise GenerationError("This run UUID belongs to a different meditation prompt.")
                duration_seconds = round(args.minutes * 60)
                if not 60 <= duration_seconds <= 1_800:
                    raise GenerationError("--minutes must be between 1 and 30")
                if not args.json:
                    print(
                        f"[1/3] Drafting locally (resumable ID {run_id})...",
                        file=sys.stderr,
                    )
                llm = LlamaCppScriptGenerator.from_artifact_store(
                    artifact_lock=artifact_lock,
                    store=artifact_store,
                    profile_name=profile_name,
                    target=target,
                    device=",".join(result.snapshot.accelerators),
                    settings=_llm_settings_for_writing_method(args.writing_method),
                )
                prompts = load_prompt_bundle(args.config_dir / "prompts")
                model_metadata = RunModelMetadata(
                    tts=raw_synthesizer.metadata,
                    llm=llm.metadata,
                )
                pending_request = PendingGenerationConfig(
                    profile=cast(Literal["lite", "standard", "high"], profile_name),
                    target=target,
                    tts=durable_tts,
                    processing=processing,
                    duration_seconds=duration_seconds,
                    seed=args.seed,
                    plan_prompt_id=prompts.plan.prompt_id,
                    plan_prompt_version=prompts.plan.version,
                    section_prompt_id=prompts.section.prompt_id,
                    section_prompt_version=prompts.section.version,
                    max_parallel_sections=args.parallel_sections,
                    meditation_kind=cast(MeditationKind | None, args.meditation_kind),
                    writing_method=args.writing_method,
                    pacing=pacing_run,
                    model_metadata=model_metadata,
                )
                if placeholder.schema_version == 3:
                    store.write_generation_request(run_id, pending_request)
                elif store.load_generation_request(run_id) != pending_request:
                    raise GenerationError(
                        "This run UUID belongs to different generation or model settings."
                    )
                workspace = GenerationWorkspace(store.root / ".generation-workspaces" / str(run_id))
                try:
                    with _interruptible_sigterm(), RunLock(store.run_directory(run_id)):
                        # We already own the run's OS lock here. Calling the public
                        # reconciliation helper would try to acquire the same lock
                        # again and (correctly) treat it as evidence of a live owner.
                        current = store.reconcile_stale_run_locked(run_id)
                        owner_id = f"{socket.gethostname()}:{os.getpid()}"
                        if current.status is RunStatus.QUEUED:
                            claimed = store.start_generation(
                                run_id,
                                owner_id=owner_id,
                                pid=os.getpid(),
                                started_at=datetime.now(UTC),
                                lease_seconds=GENERATION_LEASE_SECONDS,
                            )
                        elif current.status in (RunStatus.INTERRUPTED, RunStatus.FAILED):
                            claimed = store.restart_generation(
                                run_id,
                                owner_id=owner_id,
                                pid=os.getpid(),
                                started_at=datetime.now(UTC),
                                lease_seconds=GENERATION_LEASE_SECONDS,
                            )
                        else:
                            raise GenerationError(
                                f"Run {run_id} is {current.status.value}; "
                                "it cannot restart planning."
                            )
                        execution = claimed.execution
                        if execution is None or execution.attempt_id is None:
                            raise GenerationError("Generation claim did not receive an attempt ID.")
                        generation_attempt_id = execution.attempt_id
                        heartbeat = _GenerationHeartbeat(
                            store,
                            run_id,
                            generation_attempt_id,
                        )
                        heartbeat.start()
                        try:
                            generated = LocalMeditationGenerator(
                                llm,
                                prompts,
                                blueprints=load_blueprint_library(
                                    args.config_dir / "meditation_blueprints.yaml"
                                ),
                                recipes=(
                                    load_recipe_library(args.config_dir / "session_recipes.yaml")
                                    if args.writing_method == "recipe_whole_session"
                                    else None
                                ),
                                max_parallel_sections=args.parallel_sections,
                                # Kokoro's speech rate scales approximately with this input.
                                articulation_words_per_minute=_planning_words_per_minute(
                                    args.tts_model, speed, args.pace, args.writing_method
                                ),
                                workspace=workspace,
                                progress_callback=heartbeat.set_stage,
                            ).generate(
                                prompt=args.prompt,
                                duration_seconds=duration_seconds,
                                run_id=run_id,
                                created_at=created_at,
                                seed=args.seed,
                                meditation_kind=cast(MeditationKind | None, args.meditation_kind),
                                writing_method=args.writing_method,
                            )
                            heartbeat.raise_if_failed()
                            resolved_config = ScriptRunConfig(
                                mode="generated_prompt",
                                profile=profile_name,
                                target=target,
                                tts=durable_tts,
                                processing=processing,
                                generation=GenerationRunSettings(
                                    duration_seconds=duration_seconds,
                                    seed=args.seed,
                                    plan_prompt_id=prompts.plan.prompt_id,
                                    plan_prompt_version=prompts.plan.version,
                                    section_prompt_id=prompts.section.prompt_id,
                                    section_prompt_version=prompts.section.version,
                                    max_parallel_sections=args.parallel_sections,
                                    meditation_kind=cast(
                                        MeditationKind | None,
                                        args.meditation_kind,
                                    ),
                                    writing_method=args.writing_method,
                                    pacing=pacing_run,
                                    estimated_duration_seconds=(
                                        generated.estimated_duration_seconds
                                    ),
                                ),
                            )
                            heartbeat.stop()
                            queued = store.promote_active_generated_run(
                                run_id,
                                attempt_id=generation_attempt_id,
                                generated=generated,
                                resolved_config=resolved_config,
                                model_metadata=model_metadata,
                                promoted_at=datetime.now(UTC),
                                lease_seconds=GENERATION_LEASE_SECONDS,
                            )
                            handoff_heartbeat = _AttemptHeartbeat(
                                store,
                                run_id,
                                generation_attempt_id,
                            )
                            handoff_heartbeat.start()
                        except Exception as error:
                            heartbeat.stop()
                            store.fail_active_run(
                                run_id,
                                attempt_id=generation_attempt_id,
                                error=f"{type(error).__name__}: {error}",
                            )
                            raise
                        except BaseException as interruption:
                            heartbeat.stop()
                            store.interrupt_active_run(
                                run_id,
                                attempt_id=generation_attempt_id,
                                kind=(
                                    "keyboard_interrupt"
                                    if isinstance(interruption, KeyboardInterrupt)
                                    else "signal"
                                ),
                                message=(
                                    "Meditation generation stopped. Validated planning "
                                    "checkpoints were kept and the run can be resumed."
                                ),
                            )
                            raise
                        finally:
                            heartbeat.stop()
                except RunLockUnavailable as error:
                    raise GenerationError(str(error)) from error
                finally:
                    raw_synthesizer.close()
                if not args.json:
                    print("[2/3] Validated plan and script saved.", file=sys.stderr)
            else:
                assert args.script_file is not None
                try:
                    script = args.script_file.read_text(encoding="utf-8")
                    build_script_timeline(
                        run_id=run_id,
                        script=script,
                        created_at=created_at,
                    )
                    queued = store.promote_queued_script_run(
                        run_id,
                        script=script,
                        source_name=args.script_file.name,
                        resolved_config=ScriptRunConfig(
                            profile=profile_name,
                            target=target,
                            tts=durable_tts,
                            processing=processing,
                            requested_duration_seconds=args.requested_duration_seconds,
                        ),
                        model_metadata=RunModelMetadata(tts=raw_synthesizer.metadata),
                        promoted_at=datetime.now(UTC),
                    )
                finally:
                    # The worker reconstructs its own adapter from durable config.
                    # The metadata-only adapter must not survive alongside it.
                    raw_synthesizer.close()
            if not args.json:
                print("[3/3] Synthesizing, caching, and checking audio...", file=sys.stderr)
            try:
                with _interruptible_sigterm():
                    worker = _real_script_worker(
                        store,
                        queued,
                        artifact_lock_path=_artifact_lock_path(args),
                        models_dir=args.models_dir,
                        config_dir=args.config_dir,
                    )
                    if handoff_heartbeat is not None:
                        handoff_heartbeat.raise_if_failed()
                        handoff_heartbeat.stop()
                    if generation_attempt_id is None:
                        completed = worker.process(queued.run_id)
                    else:
                        completed = worker.continue_generated(
                            queued.run_id,
                            generation_attempt_id=generation_attempt_id,
                        )
            except BaseException as interruption:
                if generation_attempt_id is not None:
                    _persist_generation_handoff_stop(
                        store,
                        run_id,
                        generation_attempt_id,
                        interruption,
                    )
                raise
            finally:
                if handoff_heartbeat is not None:
                    handoff_heartbeat.stop()
        except KeyboardInterrupt:
            if active_run_id is not None and active_store is not None:
                _persist_unstarted_generation_interruption(active_store, active_run_id)
            recovery = (
                f" Resume with `whoopy run resume {active_run_id}` if run.json exists; "
                f"otherwise repeat generate with `--run-id {active_run_id}`."
                if active_run_id is not None
                else ""
            )
            print(f"Generation cancelled.{recovery}", file=sys.stderr)
            return 130
        except (
            ArtifactError,
            AdapterError,
            ConfigError,
            GenerationError,
            OSError,
            PromptLoadError,
            RunStoreError,
            ScriptCompileError,
            ValueError,
            WorkerError,
            WorkspaceError,
        ) as error:
            if active_run_id is not None and active_store is not None:
                _persist_unstarted_generation_failure(active_store, active_run_id, error)
            parser.error(str(error))
        _print_run(completed, store, as_json=args.json)
        return 0

    if args.command == "run" and args.run_command == "create":
        try:
            store = _run_store(args)
            record = LocalControlPlane(store).submit_prompt(args.prompt)
        except (ConfigError, RunStoreError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        if not args.json:
            next_command = f"whoopy worker process {record.run_id}"
            if args.runs_dir is not None:
                next_command += f' --runs-dir "{args.runs_dir}"'
            elif args.config_dir != Path("config"):
                next_command += f' --config-dir "{args.config_dir}"'
            print(f"Next: {next_command}")
        return 0

    if args.command == "run" and args.run_command == "show":
        try:
            store = _run_store(args)
            record = LocalControlPlane(store).get_run(args.run_id)
        except (ConfigError, RunStoreError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    if args.command == "run" and args.run_command == "reconcile":
        try:
            store = _run_store(args)
            if args.run_id is None:
                records = store.reconcile_stale_runs()
            else:
                records = (store.reconcile_stale_run(args.run_id),)
        except (ConfigError, RunStoreError) as error:
            parser.error(str(error))
        if args.json:
            print(
                json.dumps(
                    {"runs": [record.model_dump(mode="json") for record in records]},
                    indent=2,
                )
            )
        else:
            if not records:
                print("No expired running leases were found.")
            for record in records:
                print(f"{record.run_id}: {record.status.value}")
        return 0

    if args.command == "run" and args.run_command == "cancel":
        try:
            store = _run_store(args)
            record = _cancel_active_local_run(store, args.run_id)
        except (ConfigError, RunStoreError) as error:
            parser.error(str(error))
        if args.json:
            print(record.model_dump_json(indent=2))
        else:
            print(f"Cancellation requested for run {record.run_id}.")
            print("Its worker will save an interrupted state before exiting.")
        return 0

    if args.command == "run" and args.run_command == "regenerate-segment":
        try:
            store = _run_store(args)
            prepared = prepare_segment_regeneration(
                store,
                args.run_id,
                args.segment_id,
                regeneration_attempt_id=args.regeneration_attempt_id,
            )
            worker = _worker_for_record(
                store,
                prepared.record,
                artifact_lock_path=_artifact_lock_path(args),
                models_dir=args.models_dir,
                config_dir=args.config_dir,
                bypass_cache_segment_ids=prepared.bypass_cache_segment_ids,
            )
            with _interruptible_sigterm():
                record = worker.resume(args.run_id)
        except (
            AdapterError,
            ArtifactError,
            ConfigError,
            ModelPackOperationError,
            RunStoreError,
            WorkerError,
        ) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    if args.command == "run" and args.run_command == "regenerate-plan":
        try:
            store = _run_store(args)
            with _interruptible_sigterm(), RunLock(store.run_directory(args.run_id)):
                existing = store.load(args.run_id)
                if existing.status in (RunStatus.QUEUED, RunStatus.RUNNING):
                    raise RevisionError("Wait for generation to stop before editing the plan.")
                if existing.source_kind != "generated_prompt":
                    raise RevisionError("Only prompt-generated meditations have drafting context.")
                revisions = RevisionStore(store)
                base_revision_id = revisions.active_revision_id(existing.run_id)
                base_plan = MeditationPlan.model_validate_json(
                    store.plan_path(existing.run_id).read_text(encoding="utf-8")
                )
                requested_plan = MeditationPlan.model_validate_json(
                    args.plan_file.read_text(encoding="utf-8")
                )
                blueprint_path = args.config_dir / "meditation_blueprints.yaml"
                normalized_plan = validate_semantic_plan_edit(
                    base_plan,
                    requested_plan,
                    blueprint_path=blueprint_path,
                )
                pending = store.load_generation_request(existing.run_id)
                result, target, artifact_store, _artifacts = _model_plan(
                    args,
                    requested_profile=pending.profile,
                )
                if not result.supported or result.selected_profile is None:
                    raise RevisionError("The original local LLM profile is not safe to load now.")
                if target != pending.target:
                    raise RevisionError(
                        "The current machine differs from the saved drafting target."
                    )
                prompts = load_prompt_bundle(args.config_dir / "prompts")
                if (
                    prompts.plan.prompt_id != pending.plan_prompt_id
                    or prompts.plan.version != pending.plan_prompt_version
                    or prompts.section.prompt_id != pending.section_prompt_id
                    or prompts.section.version != pending.section_prompt_version
                ):
                    raise RevisionError(
                        "Prompt revisions changed since this run; create a new meditation "
                        "instead of silently mixing drafting contexts."
                    )
                artifact_lock = load_artifact_lock(_artifact_lock_path(args))
                llm = LlamaCppScriptGenerator.from_artifact_store(
                    artifact_lock=artifact_lock,
                    store=artifact_store,
                    profile_name=pending.profile,
                    target=target,
                    device=",".join(result.snapshot.accelerators),
                    settings=_llm_settings_for_writing_method(pending.writing_method),
                )
                if llm.metadata != pending.model_metadata.llm:
                    raise RevisionError(
                        "The local LLM revision or runtime differs from the original run."
                    )
                regeneration_id = uuid4()
                workspace = GenerationWorkspace(
                    store.run_directory(existing.run_id)
                    / "history"
                    / "plan-regeneration"
                    / str(regeneration_id)
                )
                workspace.save_plan(normalized_plan)
                generated = LocalMeditationGenerator(
                    llm,
                    prompts,
                    blueprints=load_blueprint_library(blueprint_path),
                    recipes=(
                        load_recipe_library(args.config_dir / "session_recipes.yaml")
                        if pending.writing_method == "recipe_whole_session"
                        else None
                    ),
                    max_parallel_sections=pending.max_parallel_sections,
                    articulation_words_per_minute=base_plan.words_per_minute,
                    workspace=workspace,
                ).generate(
                    prompt=existing.prompt,
                    duration_seconds=pending.duration_seconds,
                    run_id=existing.run_id,
                    created_at=datetime.now(UTC),
                    seed=pending.seed,
                    meditation_kind=normalized_plan.meditation_kind,
                    writing_method=pending.writing_method,
                )
                revision = revisions.create_semantic_plan_revision_locked(
                    existing.run_id,
                    base_plan=base_plan,
                    base_revision_id=base_revision_id,
                    requested_plan=normalized_plan,
                    generated=generated,
                    note=args.note,
                    blueprint_path=blueprint_path,
                )
                activated = store.load(existing.run_id)
            try:
                worker = _worker_for_record(
                    store,
                    activated,
                    artifact_lock_path=_artifact_lock_path(args),
                    models_dir=args.models_dir,
                    config_dir=args.config_dir,
                )
            except Exception as error:
                with (
                    suppress(OSError, RunStoreError, RunLockUnavailable, ValueError),
                    RunLock(store.run_directory(existing.run_id)),
                ):
                    if (
                        store.load(existing.run_id) == activated
                        and revisions.active_revision_id(existing.run_id) == revision.revision_id
                    ):
                        store.save(
                            activated.transition(
                                RunStatus.FAILED,
                                updated_at=datetime.now(UTC),
                                error=f"{type(error).__name__}: {error}",
                            )
                        )
                raise
            with _interruptible_sigterm():
                record = worker.resume(existing.run_id)
        except (
            AdapterError,
            ArtifactError,
            ConfigError,
            GenerationError,
            ModelPackOperationError,
            OSError,
            PromptLoadError,
            RevisionError,
            RunLockUnavailable,
            RunStoreError,
            ValueError,
            WorkerError,
            WorkspaceError,
        ) as error:
            parser.error(str(error))
        if args.json:
            print(
                json.dumps(
                    {
                        "run": record.model_dump(mode="json"),
                        "revision": revision.model_dump(mode="json"),
                    },
                    indent=2,
                )
            )
        else:
            print(f"Regenerated plan revision {revision.revision_id} for {record.run_id}.")
        return 0

    if args.command == "run" and args.run_command == "resume":
        try:
            store = _run_store(args)
            existing = store.load(args.run_id)
            if (
                existing.schema_version == 6
                and existing.source_kind == "generated_prompt"
                and existing.script_artifact is None
            ):
                return main(_pending_generation_resume_arguments(args, existing, store))
            worker = _worker_for_record(
                store,
                existing,
                artifact_lock_path=_artifact_lock_path(args),
                models_dir=args.models_dir,
                config_dir=args.config_dir,
            )
            with _interruptible_sigterm():
                record = worker.resume(args.run_id)
        except (
            AdapterError,
            ArtifactError,
            ConfigError,
            ModelPackOperationError,
            RunStoreError,
            WorkerError,
        ) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    if args.command == "cache" and args.cache_command == "stats":
        try:
            store = _run_store(args)
            stats = _segment_cache(store).stats()
        except (ConfigError, RunStoreError) as error:
            parser.error(str(error))
        if args.json:
            print(stats.model_dump_json(indent=2))
        else:
            print("Whoopy speech cache")
            print(f"  Entries: {stats.entries}")
            print(f"  Valid: {stats.valid_entries}")
            print(f"  Corrupt: {stats.corrupt_entries}")
            print(f"  Audio bytes: {stats.audio_bytes}")
        return 0

    if args.command == "worker" and args.worker_command == "process":
        try:
            store = _run_store(args)
            existing = store.load(args.run_id)
            worker = _worker_for_record(
                store,
                existing,
                artifact_lock_path=_artifact_lock_path(args),
                models_dir=args.models_dir,
                config_dir=args.config_dir,
            )
            with _interruptible_sigterm():
                record = worker.process(args.run_id)
        except (
            AdapterError,
            ArtifactError,
            ConfigError,
            ModelPackOperationError,
            RunStoreError,
            WorkerError,
        ) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    if args.command in {"app", "web"}:
        if not 1 <= args.port <= 65_535:
            parser.error("--port must be between 1 and 65535")
        # Import lazily so normal CLI commands do not initialize the HTTP layer.
        from whoopy.webui.server import serve

        serve(
            host=args.host,
            port=args.port,
            project_root=Path.cwd(),
            config_directory=args.config_dir,
            models_directory=args.models_dir,
            runs_directory=args.runs_dir,
            open_browser=args.open,
        )
        return 0

    parser.print_help()
    return 0
