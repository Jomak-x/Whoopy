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
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast
from uuid import UUID, uuid4

import yaml

from whoopy.adapters.llm import LlamaCppScriptGenerator, LlamaCppSettings
from whoopy.adapters.tts import (
    MOSS_LANGUAGES,
    FishSpeech14Adapter,
    FishSpeechSettings,
    MossTTSAdapter,
    MossTTSSettings,
    MossVariant,
    SherpaOnnxKokoroAdapter,
    SherpaOnnxSettings,
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
from whoopy.hardware import DoctorResult, diagnose, inspect_hardware, load_runtime_profiles
from whoopy.meditation import (
    GenerationError,
    GenerationWorkspace,
    LocalMeditationGenerator,
    load_prompt_bundle,
)
from whoopy.meditation.prompts import PromptLoadError
from whoopy.meditation.workspace import WorkspaceError
from whoopy.pipeline import (
    GenerationRunSettings,
    LocalWorker,
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
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.runs import RunNotFoundError, RunStoreError
from whoopy.pipeline.worker import WorkerError
from whoopy.ports import AdapterError, SpeechSynthesizer
from whoopy.timeline import ScriptCompileError, Timeline, build_script_timeline
from whoopy.voices import KOKORO_ENGLISH_VOICES, kokoro_speaker_id

PROJECT_ROOT = Path(__file__).resolve().parents[2]
FISH_14_RUNTIME = PROJECT_ROOT / "models" / "experimental" / "fish-speech-1.4-runtime"
MOSS_RUNTIME = PROJECT_ROOT / "models" / "experimental" / "moss-tts-runtime"
MOSS_MODELS: dict[MossVariant, Path] = {
    "moss-local-v1.5": MOSS_RUNTIME / "checkpoints" / "MOSS-TTS-Local-Transformer-v1.5",
    "moss-v1.5": MOSS_RUNTIME / "checkpoints" / "MOSS-TTS-v1.5",
}
MOSS_CODEC = MOSS_RUNTIME / "checkpoints" / "MOSS-Audio-Tokenizer-v2"
MOSS_REFERENCE = FISH_14_RUNTIME / "whoopy-reference.wav"
GENERATION_HEARTBEAT_SECONDS = 2.0
GENERATION_LEASE_SECONDS = 15.0


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
        default=Path("config"),
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
        default=Path("config"),
        help="Directory containing runtime_profiles.yaml.",
    )
    _add_runtime_model_arguments(parser)


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


def _real_script_worker(
    store: RunStore,
    record: RunRecord,
    *,
    artifact_lock_path: Path,
    models_dir: Path,
    bypass_cache_segment_ids: frozenset[str] | None = None,
) -> LocalWorker:
    """Reconstruct a schema-v4 worker only from durable run configuration."""

    resolved = store.load_resolved_config(record.run_id)
    raw_synthesizer: SpeechSynthesizer
    if resolved.tts.backend == "fish-1.4":
        raw_synthesizer = FishSpeech14Adapter(
            FishSpeechSettings(
                runtime_directory=FISH_14_RUNTIME,
                worker_script=PROJECT_ROOT / "scripts" / "fish_speech_1_4_worker.py",
                reference_audio=FISH_14_RUNTIME / "whoopy-reference.wav",
                reference_text=FISH_14_RUNTIME / "whoopy-reference.txt",
                seed=resolved.tts.seed,
            )
        )
    elif resolved.tts.backend in MOSS_MODELS:
        raw_synthesizer = MossTTSAdapter(
            MossTTSSettings(
                runtime_directory=MOSS_RUNTIME,
                worker_script=PROJECT_ROOT / "scripts" / "moss_tts_worker.py",
                model_directory=MOSS_MODELS[resolved.tts.backend],
                codec_directory=MOSS_CODEC,
                variant=resolved.tts.backend,
                reference_audio=MOSS_REFERENCE,
                language=resolved.tts.language,
                instruction=resolved.tts.instruction,
                use_reference=resolved.tts.use_reference,
                seed=resolved.tts.seed,
            )
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
    synthesizer = ProcessedSpeechSynthesizer(
        raw_synthesizer,
        settings=resolved.processing,
    )

    def timeline_builder(active_record: RunRecord, created_at: datetime) -> Timeline:
        return build_script_timeline(
            run_id=active_record.run_id,
            script=store.load_script(active_record.run_id),
            created_at=created_at,
            source=(
                "generated_prompt"
                if active_record.source_kind == "generated_prompt"
                else "script_file"
            ),
        )

    return LocalWorker(
        store,
        cache=_segment_cache(store),
        synthesizer=synthesizer,
        timeline_builder=timeline_builder,
        bypass_cache_segment_ids=bypass_cache_segment_ids,
    )


def _worker_for_record(
    store: RunStore,
    record: RunRecord,
    *,
    artifact_lock_path: Path,
    models_dir: Path,
    bypass_cache_segment_ids: frozenset[str] | None = None,
) -> LocalWorker:
    if record.source_kind in ("script_file", "generated_prompt"):
        return _real_script_worker(
            store,
            record,
            artifact_lock_path=artifact_lock_path,
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
        "--tts-model",
        pending.tts.backend,
        "--config-dir",
        str(args.config_dir),
        "--models-dir",
        str(args.models_dir),
        "--runs-dir",
        str(store.root),
    ]
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
        default=Path("config"),
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
        default=Path("config"),
        help="Directory containing runtime_profiles.yaml.",
    )
    doctor_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable output for installers and the future UI.",
    )
    doctor_parser.add_argument(
        "--profile",
        choices=("auto", "basic", "lite", "standard", "high", "studio"),
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
        choices=("auto", "basic", "lite", "standard", "high", "studio"),
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
        choices=("auto", "basic", "lite", "standard", "high", "studio"),
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
        choices=("auto", "lite", "standard"),
        default="auto",
        help="Choose a local LLM profile or select one safely.",
    )
    draft_parser.add_argument("--seed", type=int, default=42, help="Reproducible model seed.")
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
        choices=("lite", "standard"),
        default=("lite", "standard"),
        help="Candidates to measure against the same cases.",
    )
    evaluate_parser.add_argument(
        "--evaluation-set",
        type=Path,
        default=Path("config/evaluation/phase-3-5.yaml"),
        help="Versioned YAML evaluation fixture.",
    )
    evaluate_parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="New directory for report.json and per-case artifacts.",
    )
    evaluate_parser.add_argument("--seed", type=int, default=42)
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
        choices=("auto", "basic", "lite", "standard"),
        default="auto",
        help="Use Basic for scripts; prompt mode safely selects Lite or Standard.",
    )
    generate_parser.add_argument(
        "--tts-model",
        choices=("kokoro", "fish-1.4", "moss-local-v1.5", "moss-v1.5"),
        default="kokoro",
        help="Speech model; larger experimental models require local checkpoints.",
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
        "--voice",
        choices=tuple(sorted(KOKORO_ENGLISH_VOICES)),
        help="Kokoro voice name; defaults to config/default.yaml.",
    )
    generate_parser.add_argument("--speed", type=float, help="Positive Kokoro speech-speed factor.")
    generate_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable completed run record.",
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
    run_regenerate_parser.add_argument(
        "--json",
        action="store_true",
        help="Print only the machine-readable completed run record.",
    )
    _add_run_location_arguments(run_regenerate_parser)
    _add_runtime_model_arguments(run_regenerate_parser)

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

    web_parser = subcommands.add_parser(
        "web",
        help="Start the private local browser interface.",
    )
    web_parser.add_argument(
        "--host",
        choices=("127.0.0.1", "localhost"),
        default="127.0.0.1",
        help="Loopback address to use (default: 127.0.0.1).",
    )
    web_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local TCP port from 1 to 65535 (default: 8765).",
    )
    web_parser.add_argument(
        "--open",
        action="store_true",
        help="Open the interface in the default browser after starting.",
    )
    web_parser.add_argument(
        "--config-dir",
        type=Path,
        default=Path("config"),
        help="Directory containing Whoopy configuration.",
    )
    web_parser.add_argument(
        "--models-dir",
        type=Path,
        default=Path("models/managed"),
        help="Directory containing verified model artifacts.",
    )
    web_parser.add_argument(
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
                Literal["basic", "lite", "standard"],
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
                Literal["lite", "standard"],
                result.selected_profile.name,
            )
            if args.profile == "auto" and profile_name == "lite":
                raise GenerationError(
                    "The v1 bake-off did not qualify Lite for automatic drafting. "
                    "Use Standard, Basic script mode, or explicitly request "
                    "--profile lite for experimentation."
                )
            if profile_name not in ("lite", "standard"):
                raise GenerationError("Drafting requires the Lite or Standard LLM profile.")
            artifact_lock = load_artifact_lock(_artifact_lock_path(args))
            adapter = LlamaCppScriptGenerator.from_artifact_store(
                artifact_lock=artifact_lock,
                store=artifact_store,
                profile_name=profile_name,
                target=target,
                device=",".join(result.snapshot.accelerators),
                settings=LlamaCppSettings(),
            )
            run_id = UUID(args.draft_id) if args.draft_id is not None else uuid4()
            workspace = GenerationWorkspace(args.output_dir / str(run_id))
            generated = LocalMeditationGenerator(
                adapter,
                load_prompt_bundle(args.config_dir / "prompts"),
                max_parallel_sections=args.parallel_sections,
                workspace=workspace,
            ).generate(
                prompt=args.prompt,
                duration_seconds=duration_seconds,
                run_id=run_id,
                seed=args.seed,
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
                output_directory=args.output_dir,
                seed=args.seed,
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
                profile_name = cast(Literal["lite", "standard"], requested)
                component = "llm_model_lite" if profile_name == "lite" else "llm_model_standard"
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
                    settings=LlamaCppSettings(),
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
            if (
                args.run_id is not None
                and args.draft_id is not None
                and args.run_id != args.draft_id
            ):
                raise GenerationError("--run-id and the deprecated --draft-id must match.")
            settings = load_settings(args.config_dir)
            prompt_mode = args.prompt is not None
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
                Literal["basic", "lite", "standard"],
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
                )
            created_at = placeholder.created_at
            artifact_lock = load_artifact_lock(_artifact_lock_path(args))
            voice_name = args.voice or settings.tts.voice
            speed = args.speed if args.speed is not None else settings.tts.speed
            if not 0.5 <= speed <= 1.2:
                raise GenerationError("--speed must be between 0.5 and 1.2")
            raw_synthesizer: SpeechSynthesizer
            if args.tts_model == "fish-1.4":
                fish_error = FishSpeech14Adapter.availability_error(FISH_14_RUNTIME)
                if fish_error is not None:
                    raise GenerationError(f"Fish Speech 1.4 is not ready: {fish_error}.")
                raw_synthesizer = FishSpeech14Adapter(
                    FishSpeechSettings(
                        runtime_directory=FISH_14_RUNTIME,
                        worker_script=PROJECT_ROOT / "scripts" / "fish_speech_1_4_worker.py",
                        reference_audio=FISH_14_RUNTIME / "whoopy-reference.wav",
                        reference_text=FISH_14_RUNTIME / "whoopy-reference.txt",
                        seed=args.seed,
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
                )
            elif args.tts_model in MOSS_MODELS:
                moss_model = MOSS_MODELS[args.tts_model]
                moss_error = MossTTSAdapter.availability_error(
                    MOSS_RUNTIME,
                    moss_model,
                    MOSS_CODEC,
                    MOSS_REFERENCE,
                )
                if moss_error is not None:
                    raise GenerationError(f"{args.tts_model} is not ready: {moss_error}.")
                raw_synthesizer = MossTTSAdapter(
                    MossTTSSettings(
                        runtime_directory=MOSS_RUNTIME,
                        worker_script=PROJECT_ROOT / "scripts" / "moss_tts_worker.py",
                        model_directory=moss_model,
                        codec_directory=MOSS_CODEC,
                        variant=cast(MossVariant, args.tts_model),
                        reference_audio=MOSS_REFERENCE,
                        language=args.moss_language,
                        instruction=args.moss_instruction,
                        use_reference=not args.moss_direct_voice,
                        seed=args.seed,
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
                    settings=LlamaCppSettings(),
                )
                prompts = load_prompt_bundle(args.config_dir / "prompts")
                model_metadata = RunModelMetadata(
                    tts=raw_synthesizer.metadata,
                    llm=llm.metadata,
                )
                pending_request = PendingGenerationConfig(
                    profile=cast(Literal["lite", "standard"], profile_name),
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
                                max_parallel_sections=args.parallel_sections,
                                # Kokoro's speech rate scales approximately with this input.
                                # Real-device calibration measured about 122 WPM at 0.6.
                                articulation_words_per_minute=(
                                    133
                                    if args.tts_model
                                    in (
                                        "fish-1.4",
                                        "moss-local-v1.5",
                                        "moss-v1.5",
                                    )
                                    else max(80, round(205 * speed))
                                ),
                                workspace=workspace,
                                progress_callback=heartbeat.set_stage,
                            ).generate(
                                prompt=args.prompt,
                                duration_seconds=duration_seconds,
                                run_id=run_id,
                                created_at=created_at,
                                seed=args.seed,
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
            )
            worker = _worker_for_record(
                store,
                prepared.record,
                artifact_lock_path=_artifact_lock_path(args),
                models_dir=args.models_dir,
                bypass_cache_segment_ids=prepared.bypass_cache_segment_ids,
            )
            with _interruptible_sigterm():
                record = worker.resume(args.run_id)
        except (AdapterError, ArtifactError, ConfigError, RunStoreError, WorkerError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
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
            )
            with _interruptible_sigterm():
                record = worker.resume(args.run_id)
        except (AdapterError, ArtifactError, ConfigError, RunStoreError, WorkerError) as error:
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
            )
            with _interruptible_sigterm():
                record = worker.process(args.run_id)
        except (AdapterError, ArtifactError, ConfigError, RunStoreError, WorkerError) as error:
            parser.error(str(error))
        _print_run(record, store, as_json=args.json)
        return 0

    if args.command == "web":
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
