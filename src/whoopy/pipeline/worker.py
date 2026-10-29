"""Foreground worker with per-segment caching, retry, and durable resume."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Never
from uuid import UUID, uuid4

from whoopy.audio import TimelineWaveRenderer
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio, QualityCheck, RenderedWave
from whoopy.audio.quality import pcm_integrity_error
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    SpeechSynthesizer,
    TransientSynthesisError,
    cache_key_for,
    synthesis_input_bytes,
)
from whoopy.meditation.models import DraftedSection, MeditationPlan
from whoopy.meditation.pacing import allocate_bounded_quiet
from whoopy.meditation.whole_session import recipe_quiet_limits
from whoopy.pipeline.acceptance_stage import observe_acceptance_stage
from whoopy.pipeline.cache import SegmentCache
from whoopy.pipeline.checkpoints import (
    CheckpointStatus,
    FailureKind,
    SegmentAttemptFailure,
    SegmentCheckpoint,
    SegmentCheckpointStore,
)
from whoopy.pipeline.events import RunEventLog
from whoopy.pipeline.generation import AdapterRunTelemetry, HardwareTelemetry, RunTelemetry
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.logs import WorkerLog
from whoopy.pipeline.runs import (
    AUDIO_FILENAME,
    AUDIO_MANIFEST_FILENAME,
    QUALITY_FILENAME,
    TIMELINE_FILENAME,
    RunExecution,
    RunRecord,
    RunRecovery,
    RunStage,
    RunStatus,
    RunStore,
    RunStoreError,
)
from whoopy.pipeline.telemetry import (
    PeakProcessTreeMemory,
    hardware_snapshot,
    measured,
    unavailable,
)
from whoopy.timeline import (
    BreathSegment,
    SilenceSegment,
    SpeechSegment,
    Timeline,
    build_fixture_timeline,
)

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
TimelineBuilder = Callable[[RunRecord, datetime], Timeline]
DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 2.0
DEFAULT_LEASE_SECONDS = 15.0


class WorkerError(RuntimeError):
    """Raised when a run cannot be claimed, recovered, or completed."""


class _SegmentProcessingError(RuntimeError):
    """Internal error carrying a clear segment-level failure message."""


def _execution_message(error: BaseException) -> str:
    """Fit diagnostics inside the durable execution-message contract."""

    return f"{type(error).__name__}: {error}"[:2_000]


class _HeartbeatLoop:
    """Renew a worker lease while a blocking model call is in progress."""

    def __init__(
        self,
        callback: Callable[[], None],
        *,
        interval_seconds: float,
    ) -> None:
        self.callback = callback
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="whoopy-run-heartbeat",
            daemon=True,
        )
        self.error: Exception | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.interval_seconds * 2))

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            try:
                self.callback()
            except Exception as error:
                self.error = error
                self._stop.set()
                return


@dataclass(frozen=True)
class RetryPolicy:
    """Bound attempts and exponential delay for transient segment failures."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.25
    max_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("retry max_attempts must be at least one")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative")

    def delay_after_failure(self, invocation_attempt: int) -> float:
        """Return the bounded delay before the next attempt."""

        return float(
            min(
                self.max_delay_seconds,
                self.base_delay_seconds * (2.0 ** (invocation_attempt - 1)),
            )
        )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_timeline_builder(record: RunRecord, created_at: datetime) -> Timeline:
    return build_fixture_timeline(
        run_id=record.run_id,
        prompt=record.prompt,
        created_at=created_at,
    )


def _updated_recovery(recovery: RunRecovery, **changes: object) -> RunRecovery:
    values = recovery.model_dump()
    values.update(changes)
    return RunRecovery.model_validate(values)


def _acceptance_stage_name(stage: RunStage) -> str:
    """Map product progress names to the PR21 acceptance vocabulary."""

    return {
        RunStage.SYNTHESIZING: "synthesis",
        RunStage.ASSEMBLING: "assembly",
        RunStage.ENCODING: "encoding",
        RunStage.QUALITY_CHECK: "qc",
    }.get(stage, stage.value)


def fit_generated_timeline_to_real_speech(
    timeline: Timeline,
    *,
    speech_audio: dict[str, PcmAudio],
    target_seconds: int,
    maximum_silence_ms: int | None = None,
    quiet_limits: dict[str, int] | None = None,
) -> Timeline:
    """Fit adjustable quiet to the requested duration after real TTS is known."""

    silences = [segment for segment in timeline.segments if isinstance(segment, SilenceSegment)]
    if not silences:
        return timeline
    speech_ms = round(
        sum(audio.frame_count / audio.sample_rate * 1_000 for audio in speech_audio.values())
    )
    breath_ms = sum(
        segment.duration_ms for segment in timeline.segments if isinstance(segment, BreathSegment)
    )
    silence_budget = target_seconds * 1_000 - speech_ms - breath_ms
    minimum_ms = 1_000
    if silence_budget < minimum_ms * len(silences):
        raise WorkerError(
            "Real speech is too long for the requested duration while preserving "
            "one-second minimum quiet periods. Shorten the script or increase the duration."
        )
    caps = [
        min(
            maximum_silence_ms if maximum_silence_ms is not None else 600_000,
            quiet_limits[segment.id] if quiet_limits is not None else 600_000,
        )
        for segment in silences
    ]
    try:
        allocated = allocate_bounded_quiet(
            silence_budget,
            [float(max(1, segment.duration_ms - minimum_ms)) for segment in silences],
            caps,
        )
    except ValueError as error:
        raise WorkerError(str(error)) from error
    durations = dict(zip((segment.id for segment in silences), allocated, strict=True))
    return timeline.model_copy(
        update={
            "segments": [
                segment.model_copy(update={"duration_ms": durations[segment.id]})
                if isinstance(segment, SilenceSegment)
                else segment
                for segment in timeline.segments
            ]
        }
    )


class LocalWorker:
    """Process one selected run with verified checkpoints and cache reuse."""

    def __init__(
        self,
        store: RunStore,
        *,
        clock: Clock = _utc_now,
        timeline_builder: TimelineBuilder = _default_timeline_builder,
        synthesizer: SpeechSynthesizer | None = None,
        renderer: TimelineWaveRenderer | None = None,
        cache: SegmentCache | None = None,
        retry_policy: RetryPolicy | None = None,
        sleeper: Sleeper = time.sleep,
        heartbeat_interval_seconds: float = DEFAULT_HEARTBEAT_INTERVAL_SECONDS,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        owner_id: str | None = None,
        bypass_cache_segment_ids: frozenset[str] | None = None,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        if lease_seconds <= heartbeat_interval_seconds:
            raise ValueError("execution lease must be longer than the heartbeat interval")
        self.store = store
        self.clock = clock
        self.timeline_builder = timeline_builder
        self.synthesizer: SpeechSynthesizer = (
            synthesizer if synthesizer is not None else FixtureSpeechSynthesizer()
        )
        self.renderer = renderer or TimelineWaveRenderer(self.synthesizer)
        self.cache = cache or SegmentCache(store.root / ".cache" / "segments")
        self.checkpoints = SegmentCheckpointStore(store)
        self.retry_policy = retry_policy or RetryPolicy()
        self.sleeper = sleeper
        self.heartbeat_interval_seconds = heartbeat_interval_seconds
        self.lease_seconds = lease_seconds
        self.owner_id = owner_id or f"{socket.gethostname()}:{os.getpid()}"
        self.bypass_cache_segment_ids = bypass_cache_segment_ids or frozenset()
        self.events = RunEventLog(store)
        self.worker_log = WorkerLog(store)
        self._state_lock = threading.RLock()

    def process(self, run_id: UUID | str) -> RunRecord:
        """Start one queued run."""

        return self._claim_and_execute(run_id, is_resume=False)

    def resume(self, run_id: UUID | str) -> RunRecord:
        """Resume a failed or interrupted run from verified segment checkpoints."""

        return self._claim_and_execute(run_id, is_resume=True)

    def continue_generated(
        self,
        run_id: UUID | str,
        *,
        generation_attempt_id: UUID,
    ) -> RunRecord:
        """Accept an active generated run at the durable generation/audio handoff."""

        parsed_run_id = self.store.parse_run_id(run_id)
        try:
            with RunLock(self.store.run_directory(parsed_run_id)):
                record = self.store.load(parsed_run_id)
                execution = record.execution
                if (
                    record.status is not RunStatus.RUNNING
                    or record.source_kind != "generated_prompt"
                    or execution is None
                    or execution.attempt_id != generation_attempt_id
                    or execution.stage is not RunStage.COMPILING
                ):
                    raise WorkerError(
                        f"Run {record.run_id} is not an active generated run ready to compile"
                    )
                return self._execute(record, is_resume=False)
        except RunLockUnavailable as error:
            raise WorkerError(str(error)) from error

    def _claim_and_execute(self, run_id: UUID | str, *, is_resume: bool) -> RunRecord:
        parsed_run_id = self.store.parse_run_id(run_id)
        lock = RunLock(self.store.run_directory(parsed_run_id))
        try:
            with lock:
                if is_resume:
                    # Legacy RUNNING records had no lease. New RUNNING records can
                    # only resume after their 15-second lease expires.
                    self.store.reconcile_stale_run_locked(parsed_run_id, now=self.clock())
                record = self.store.load(parsed_run_id)
                if not is_resume and record.status is not RunStatus.QUEUED:
                    raise WorkerError(
                        f"Run {record.run_id} is {record.status.value}; "
                        "only queued runs can be processed"
                    )
                if is_resume and record.status not in (
                    RunStatus.FAILED,
                    RunStatus.INTERRUPTED,
                ):
                    raise WorkerError(
                        f"Run {record.run_id} is {record.status.value}; "
                        "only failed or interrupted runs can be resumed"
                    )
                if (
                    is_resume
                    and record.source_kind == "fixture_prompt"
                    and record.execution is not None
                    and record.execution.stage in (RunStage.PLANNING, RunStage.DRAFTING)
                ):
                    raise WorkerError(
                        f"Run {record.run_id} was interrupted during generation; "
                        "resume it through the script generator"
                    )
                if (
                    is_resume
                    and record.execution is not None
                    and record.execution.interruption_kind == "manual_regeneration"
                    and record.execution.current_segment_id is not None
                ):
                    # Persisted intent is authoritative after a crash between
                    # regeneration preparation and worker launch. Reusing the
                    # old checkpoint or shared cache here would make the
                    # requested regeneration a silent no-op.
                    self.bypass_cache_segment_ids = self.bypass_cache_segment_ids.union(
                        (record.execution.current_segment_id,)
                    )
                return self._execute(record, is_resume=is_resume)
        except RunLockUnavailable as error:
            raise WorkerError(str(error)) from error

    def _execute(self, record: RunRecord, *, is_resume: bool) -> RunRecord:
        recovery = record.recovery
        if recovery is None:
            raise WorkerError(
                f"Run {record.run_id} uses schema v{record.schema_version}; "
                "Phase 3 recovery requires a schema-v3 run"
            )

        recovery = _updated_recovery(
            recovery,
            process_attempts=recovery.process_attempts + 1,
            resume_count=recovery.resume_count + int(is_resume),
            failed_segment_id=None,
        )
        started_at = self.clock()
        execution = RunExecution(
            stage=RunStage.COMPILING,
            attempt_id=uuid4(),
            owner_id=self.owner_id,
            pid=os.getpid(),
            started_at=started_at,
            heartbeat_at=started_at,
            lease_expires_at=started_at + timedelta(seconds=self.lease_seconds),
            message="Preparing the canonical timeline.",
        )
        running = record.transition(
            RunStatus.RUNNING,
            updated_at=started_at,
            recovery=recovery,
            execution=execution,
        )
        current_segment_id: str | None = None
        heartbeat = _HeartbeatLoop(
            lambda: self._heartbeat(running.run_id, execution.attempt_id),
            interval_seconds=self.heartbeat_interval_seconds,
        )
        hardware_before = hardware_snapshot(self.store.root)
        memory = PeakProcessTreeMemory()
        memory.start()
        load_elapsed_seconds: float | None = None
        load_unavailable_reason = "all speech segments were restored from cache or checkpoints"
        model_prepared = False
        inference_elapsed_seconds = 0.0
        inference_calls = 0
        inference_audio_seconds = 0.0

        def record_inference_call() -> None:
            nonlocal inference_calls
            inference_calls += 1

        try:
            with self._state_lock:
                self.store.save(running)
                self._record_event_best_effort(
                    running.run_id,
                    occurred_at=started_at,
                    kind="attempt_started",
                    stage=RunStage.COMPILING,
                    attempt_id=execution.attempt_id,
                    message=(
                        "Resumed local processing." if is_resume else "Started local processing."
                    ),
                )
            heartbeat.start()
            timeline = self._load_or_build_timeline(running)
            speech_segments = [
                segment for segment in timeline.segments if isinstance(segment, SpeechSegment)
            ]
            recovery = _updated_recovery(
                recovery,
                speech_segments_total=len(speech_segments),
                speech_segments_completed=0,
            )
            running = self._save_running_progress(
                running,
                recovery,
                stage=RunStage.SYNTHESIZING,
                message="Synthesizing speech segments.",
            )

            prepared_audio: dict[str, PcmAudio] = {}
            cache_keys: dict[str, str] = {}
            for segment in speech_segments:
                current_segment_id = segment.id
                running = self._save_running_progress(
                    running,
                    recovery,
                    stage=RunStage.SYNTHESIZING,
                    current_segment_id=segment.id,
                    message=f"Synthesizing segment {segment.id}.",
                )
                if heartbeat.error is not None:
                    raise WorkerError(f"Run heartbeat failed: {heartbeat.error}")
                cache_key = cache_key_for(segment, self.synthesizer)
                cache_keys[segment.id] = cache_key

                bypass_cache = segment.id in self.bypass_cache_segment_ids
                checkpoint_hit = (
                    None
                    if bypass_cache
                    else self.checkpoints.load_completed(
                        running.run_id,
                        segment.id,
                        cache_key=cache_key,
                    )
                )
                if checkpoint_hit is not None:
                    audio = checkpoint_hit.audio
                    recovery = _updated_recovery(
                        recovery,
                        checkpoint_reuses=recovery.checkpoint_reuses + 1,
                    )
                else:
                    cache_hit = (
                        None
                        if bypass_cache
                        else self.cache.load(
                            cache_key,
                            expected_synthesizer=self.synthesizer,
                        )
                    )
                    if cache_hit is not None:
                        audio = cache_hit.audio
                        self._checkpoint_cache_hit(
                            running,
                            segment,
                            cache_key,
                            audio,
                        )
                        recovery = _updated_recovery(
                            recovery,
                            cache_hits=recovery.cache_hits + 1,
                        )
                    else:
                        recovery = _updated_recovery(
                            recovery,
                            cache_misses=recovery.cache_misses + 1,
                        )
                        running = self._save_running_progress(
                            running,
                            recovery,
                            stage=RunStage.SYNTHESIZING,
                            current_segment_id=segment.id,
                        )
                        if not model_prepared:
                            prepare = getattr(self.synthesizer, "prepare", None)
                            load_unavailable_reason = (
                                "adapter does not expose a separate load-complete signal"
                            )
                            if callable(prepare):
                                running = self._save_running_progress(
                                    running,
                                    recovery,
                                    stage=RunStage.MODEL_STARTUP,
                                    current_segment_id=segment.id,
                                    message="Loading and validating the speech model.",
                                )
                                load_unavailable_reason = "model load did not complete successfully"
                                load_started = time.perf_counter()
                                prepared = prepare()
                                if prepared is not False:
                                    load_elapsed_seconds = time.perf_counter() - load_started
                            model_prepared = True
                            running = self._save_running_progress(
                                running,
                                recovery,
                                stage=RunStage.SYNTHESIZING,
                                current_segment_id=segment.id,
                            )
                        inference_started = time.perf_counter()
                        try:
                            audio = self._synthesize_with_retry(
                                running,
                                segment,
                                cache_key,
                                on_inference=record_inference_call,
                            )
                        finally:
                            inference_elapsed_seconds += time.perf_counter() - inference_started
                        inference_audio_seconds += audio.frame_count / audio.sample_rate

                prepared_audio[segment.id] = audio
                recovery = _updated_recovery(
                    recovery,
                    speech_segments_completed=recovery.speech_segments_completed + 1,
                )
                running = self._save_running_progress(
                    running,
                    recovery,
                    stage=RunStage.SYNTHESIZING,
                    current_segment_id=segment.id,
                )

            current_segment_id = None
            running = self._save_running_progress(
                running,
                recovery,
                stage=RunStage.ASSEMBLING,
                message="Assembling the canonical audio timeline.",
            )
            target_duration_seconds: int | None = None
            resolved = None
            if running.source_kind in {"script_file", "generated_prompt"}:
                resolved = self.store.load_resolved_config(running.run_id)
                target_duration_seconds = resolved.requested_duration_seconds
            if timeline.preserve_timing:
                target_duration_seconds = None
            elif running.source_kind == "generated_prompt":
                assert resolved is not None
                if resolved.generation is None:
                    raise WorkerError("Generated run has no durable duration settings.")
                target_duration_seconds = resolved.generation.duration_seconds
                quiet_limits = None
                if resolved.generation.writing_method == "recipe_whole_session":
                    plan = MeditationPlan.model_validate_json(
                        self.store.plan_path(running.run_id).read_text(encoding="utf-8")
                    )
                    quiet_limits = recipe_quiet_limits(
                        timeline,
                        plan,
                        [
                            DraftedSection.model_validate_json(path.read_text(encoding="utf-8"))
                            for path in self.store.draft_sections_path(running.run_id).glob(
                                "*.json"
                            )
                        ],
                    )
                timeline = fit_generated_timeline_to_real_speech(
                    timeline,
                    speech_audio=prepared_audio,
                    target_seconds=target_duration_seconds,
                    quiet_limits=quiet_limits,
                )
                with self._state_lock:
                    self.store.write_timeline(running.run_id, timeline)
            running = self._save_running_progress(
                running,
                recovery,
                stage=RunStage.ENCODING,
                message="Encoding the assembled canonical audio.",
            )
            rendered = self.renderer.render(
                timeline,
                speech_audio=prepared_audio,
                speech_cache_keys=cache_keys,
            )
            if target_duration_seconds is not None:
                target_ms = target_duration_seconds * 1_000
                error_percent = abs(rendered.manifest.duration_ms - target_ms) / target_ms * 100
                duration_check = QualityCheck(
                    name="requested_duration",
                    passed=error_percent <= 5,
                    detail=(
                        f"rendered_ms={rendered.manifest.duration_ms}, target_ms={target_ms}, "
                        f"error_percent={error_percent:.3f}, limit=5"
                    ),
                )
                quality = rendered.quality.model_copy(
                    update={
                        "passed": rendered.quality.passed and duration_check.passed,
                        "checks": [*rendered.quality.checks, duration_check],
                    }
                )
                if not quality.passed:
                    raise WorkerError("Rendered audio is outside the requested ±5% duration gate.")
                rendered = RenderedWave(
                    wave_bytes=rendered.wave_bytes,
                    manifest=rendered.manifest,
                    quality=quality,
                )
            running = self._save_running_progress(
                running,
                recovery,
                stage=RunStage.QUALITY_CHECK,
                message="Saving audio and quality evidence.",
            )
            with self._state_lock:
                self.store.write_audio(running.run_id, rendered.wave_bytes)
                self.store.write_audio_manifest(running.run_id, rendered.manifest)
                self.store.write_quality(running.run_id, rendered.quality)
            heartbeat.stop()
            completed_execution = self._required_execution(running).finish(
                stage=RunStage.COMPLETED,
                message="Meditation audio completed successfully.",
            )
            completed = running.transition(
                RunStatus.COMPLETED,
                updated_at=self.clock(),
                timeline_artifact=TIMELINE_FILENAME,
                audio_artifact=AUDIO_FILENAME,
                audio_manifest_artifact=AUDIO_MANIFEST_FILENAME,
                quality_artifact=QUALITY_FILENAME,
                recovery=recovery,
                execution=completed_execution,
            )
            with self._state_lock:
                self.store.save(completed)
                self._record_event_best_effort(
                    running.run_id,
                    occurred_at=completed.updated_at,
                    kind="attempt_completed",
                    stage=RunStage.COMPLETED,
                    attempt_id=completed_execution.attempt_id,
                    message="Run completed and all artifacts were committed.",
                )
            return completed
        except Exception as error:
            heartbeat.stop()
            try:
                with self._state_lock:
                    running = self._load_owned_progress(running)
                    if running.status is not RunStatus.RUNNING:
                        raise
                    failed_recovery = _updated_recovery(
                        recovery,
                        failed_segment_id=current_segment_id,
                    )
                    failed_execution = self._required_execution(running).finish(
                        current_segment_id=current_segment_id,
                        message=_execution_message(error),
                    )
                    failed = running.transition(
                        RunStatus.FAILED,
                        updated_at=self.clock(),
                        recovery=failed_recovery,
                        execution=failed_execution,
                        error=f"{type(error).__name__}: {error}",
                    )
                    self.store.save(failed)
                    self._record_event_best_effort(
                        running.run_id,
                        occurred_at=failed.updated_at,
                        kind="attempt_failed",
                        stage=failed_execution.stage,
                        attempt_id=failed_execution.attempt_id,
                        segment_id=current_segment_id,
                        message=failed.error or "Run failed.",
                    )
            except RunStoreError as save_error:
                raise WorkerError(
                    f"Run {running.run_id} failed and its failure state could not be saved: "
                    f"{save_error}"
                ) from error
            raise WorkerError(f"Run {running.run_id} failed: {error}") from error
        except BaseException as interruption:
            heartbeat.stop()
            with self._state_lock:
                running = self._load_owned_progress(running)
                if running.status is not RunStatus.RUNNING:
                    raise
                interrupted_execution = self._required_execution(running).finish(
                    current_segment_id=current_segment_id,
                    interruption_kind=self._interruption_kind(interruption),
                    message=(
                        "Processing stopped unexpectedly. Completed segment checkpoints "
                        "were kept and this run can be resumed."
                    ),
                )
                interrupted = running.transition(
                    RunStatus.INTERRUPTED,
                    updated_at=self.clock(),
                    recovery=recovery,
                    execution=interrupted_execution,
                )
                self.store.save(interrupted)
                self._record_event_best_effort(
                    running.run_id,
                    occurred_at=interrupted.updated_at,
                    kind="attempt_interrupted",
                    stage=interrupted_execution.stage,
                    attempt_id=interrupted_execution.attempt_id,
                    segment_id=current_segment_id,
                    message=interrupted_execution.message or "Run interrupted.",
                )
            raise
        finally:
            heartbeat.stop()
            self._close_synthesizer()
            memory.stop()
            self._persist_run_telemetry(
                running.run_id,
                hardware_before=hardware_before,
                memory=memory,
                load_elapsed_seconds=load_elapsed_seconds,
                load_unavailable_reason=load_unavailable_reason,
                inference_elapsed_seconds=inference_elapsed_seconds,
                inference_calls=inference_calls,
                inference_audio_seconds=inference_audio_seconds,
            )
            self._persist_synthesizer_diagnostics(running.run_id)

    def _persist_run_telemetry(
        self,
        run_id: UUID,
        *,
        hardware_before: HardwareTelemetry,
        memory: PeakProcessTreeMemory,
        load_elapsed_seconds: float | None,
        load_unavailable_reason: str,
        inference_elapsed_seconds: float,
        inference_calls: int,
        inference_audio_seconds: float,
    ) -> None:
        """Merge measured runtime evidence without changing immutable controls."""

        if not self.store.model_metadata_path(run_id).is_file():
            return
        try:
            metadata = self.store.load_model_metadata(run_id)
            if load_elapsed_seconds is None:
                load = unavailable("seconds", load_unavailable_reason)
            else:
                load = measured("seconds", load_elapsed_seconds)
            if inference_calls:
                inference_elapsed = measured("seconds", inference_elapsed_seconds)
                inference_speed = measured(
                    "audio_seconds_per_second",
                    inference_audio_seconds / inference_elapsed_seconds
                    if inference_elapsed_seconds > 0
                    else 0,
                )
            else:
                reason = "all speech segments were restored from cache or checkpoints"
                inference_elapsed = unavailable("seconds", reason)
                inference_speed = unavailable("audio_seconds_per_second", reason)
            memory_reason = memory.unavailable_reason or "process memory inspection unavailable"
            peak_memory = (
                measured("bytes", memory.peak_bytes)
                if memory.peak_bytes is not None and memory.unavailable_reason is None
                else unavailable("bytes", memory_reason)
            )
            live_memory = (
                measured("bytes", memory.live_bytes_after)
                if memory.live_bytes_after is not None and memory.unavailable_reason is None
                else unavailable("bytes", memory_reason)
            )
            tts = AdapterRunTelemetry(
                load_elapsed=load,
                inference_elapsed=inference_elapsed,
                inference_speed=inference_speed,
                peak_process_tree_memory=peak_memory,
                live_process_tree_memory_after=live_memory,
                inference_calls=measured("calls", inference_calls),
                **self._runtime_device_evidence(self.synthesizer),
            )
            llm = self._llm_telemetry(run_id) if metadata.llm is not None else None
            telemetry = RunTelemetry(
                captured_at=_utc_now(),
                hardware_before=hardware_before,
                hardware_after=hardware_snapshot(self.store.root),
                tts=tts,
                llm=llm,
            )
            self.store.write_model_metadata(
                run_id,
                metadata.model_copy(update={"telemetry": telemetry}),
            )
        except Exception as error:
            with suppress(Exception):
                self.worker_log.record(
                    run_id,
                    occurred_at=_utc_now(),
                    source="worker",
                    message=f"telemetry unavailable: {type(error).__name__}: {error}"[:2_000],
                )

    def _llm_telemetry(self, run_id: UUID) -> AdapterRunTelemetry:
        elapsed: list[float] = []
        attempt_telemetry: list[dict[str, object]] = []
        directory = self.store.raw_model_output_path(run_id)
        if directory.is_dir():
            for path in directory.glob("*.json"):
                with suppress(OSError, ValueError, TypeError):
                    document = json.loads(path.read_text(encoding="utf-8"))
                    elapsed.append(float(document["elapsed_seconds"]))
                    telemetry = document.get("telemetry")
                    attempt_telemetry.append(telemetry if isinstance(telemetry, dict) else {})
        if elapsed:
            inference_elapsed = measured("seconds", sum(elapsed))
            calls = measured("calls", len(elapsed))
        else:
            reason = "no persisted LLM attempt timings were available"
            inference_elapsed = unavailable("seconds", reason)
            calls = unavailable("calls", reason)

        def complete_values(name: str) -> list[float | int] | None:
            values = [attempt.get(name) for attempt in attempt_telemetry]
            if not values or len(values) != len(elapsed):
                return None
            if any(not isinstance(value, (float, int)) for value in values):
                return None
            return [value for value in values if isinstance(value, (float, int))]

        load_values = complete_values("load_elapsed_seconds")
        token_counts = complete_values("output_tokens")
        token_elapsed = complete_values("output_token_elapsed_seconds")
        direct_token_speeds = complete_values("output_tokens_per_second")
        peak_values = complete_values("peak_process_tree_memory_bytes")
        live_values = complete_values("live_process_tree_memory_after_bytes")
        devices = [attempt.get("runtime_device") for attempt in attempt_telemetry]

        incomplete = "one or more LLM attempts did not expose this measurement"
        load = (
            measured("seconds", sum(load_values))
            if load_values is not None
            else unavailable("seconds", incomplete)
        )
        if token_counts is not None and token_elapsed is not None and sum(token_elapsed) > 0:
            inference_speed = measured("tokens_per_second", sum(token_counts) / sum(token_elapsed))
        elif direct_token_speeds is not None:
            inference_speed = measured(
                "tokens_per_second", sum(direct_token_speeds) / len(direct_token_speeds)
            )
        else:
            inference_speed = unavailable(
                "tokens_per_second",
                "one or more LLM attempts lacked token counts/timing or direct throughput",
            )
        peak_memory = (
            measured("bytes", max(peak_values))
            if peak_values is not None
            else unavailable("bytes", incomplete)
        )
        live_memory = (
            measured("bytes", live_values[-1])
            if live_values is not None
            else unavailable(
                "bytes",
                "llama.cpp subprocesses exit after each invocation; no live process tree "
                "remains after completion",
            )
        )
        selected_devices = {value for value in devices if isinstance(value, str) and value}
        complete_devices = len(devices) == len(elapsed) and len(selected_devices) == 1
        runtime_device = next(iter(selected_devices)) if complete_devices else None
        return AdapterRunTelemetry(
            load_elapsed=load,
            inference_elapsed=inference_elapsed,
            inference_speed=inference_speed,
            peak_process_tree_memory=peak_memory,
            live_process_tree_memory_after=live_memory,
            inference_calls=calls,
            runtime_device=runtime_device,
            runtime_device_unavailable_reason=(None if runtime_device is not None else incomplete),
        )

    @staticmethod
    def _runtime_device_evidence(synthesizer: SpeechSynthesizer) -> dict[str, str | None]:
        value = getattr(synthesizer, "runtime_device", None)
        if value is None:
            return {
                "runtime_device": None,
                "runtime_device_unavailable_reason": (
                    "the speech adapter did not report its selected runtime device"
                ),
            }
        return {
            "runtime_device": str(value),
            "runtime_device_unavailable_reason": None,
        }

    def _load_or_build_timeline(self, running: RunRecord) -> Timeline:
        """Reuse the canonical timeline on resume, or create it once."""

        if self.store.timeline_path(running.run_id).is_file():
            return self.store.load_timeline(running.run_id)
        timeline = self.timeline_builder(running, self.clock())
        self.store.write_timeline(running.run_id, timeline)
        return timeline

    def _record_event_best_effort(self, run_id: UUID, **values: Any) -> None:
        """Keep diagnostic I/O from changing the authoritative processing result."""

        with suppress(Exception):
            self.events.record(run_id, **values)

    def _save_running_progress(
        self,
        running: RunRecord,
        recovery: RunRecovery,
        *,
        stage: RunStage,
        current_segment_id: str | None = None,
        message: str | None = None,
    ) -> RunRecord:
        execution = self._required_execution(running)
        now = self.clock()
        updated_execution = execution.heartbeat(
            now=now,
            lease_seconds=self.lease_seconds,
            stage=stage,
            current_segment_id=current_segment_id,
            message=message,
        )
        updated = running.transition(
            RunStatus.RUNNING,
            updated_at=now,
            recovery=recovery,
            execution=updated_execution,
        )
        with self._state_lock:
            self.store.save(updated)
            if execution.stage is not stage or execution.current_segment_id != current_segment_id:
                self._record_event_best_effort(
                    running.run_id,
                    occurred_at=now,
                    kind="stage_changed",
                    stage=stage,
                    attempt_id=updated_execution.attempt_id,
                    segment_id=current_segment_id,
                    message=message or f"Run entered {stage.value}.",
                )
        # For synthesis, wait until at least one completed speech checkpoint is
        # durable.  That makes the destructive acceptance case prove healthy
        # work is reused instead of merely proving an early stage transition.
        if stage is not RunStage.SYNTHESIZING or recovery.speech_segments_completed > 0:
            observe_acceptance_stage(
                run_id=updated.run_id,
                stage=_acceptance_stage_name(stage),
                durable_run_status=updated.status.value,
                durable_run_stage=stage.value,
            )
        return updated

    def _load_owned_progress(self, running: RunRecord) -> RunRecord:
        durable = self.store.load(running.run_id)
        if durable.status is RunStatus.QUEUED:
            return running
        if (
            durable.execution is None
            or durable.execution.attempt_id != self._required_execution(running).attempt_id
        ):
            raise WorkerError(f"Run {running.run_id} is no longer owned by this attempt")
        return durable

    def _heartbeat(self, run_id: UUID, attempt_id: UUID | None) -> None:
        if attempt_id is None:
            raise WorkerError(f"Run {run_id} has no attempt ID to heartbeat")
        with self._state_lock:
            self.store.heartbeat(
                run_id,
                attempt_id=attempt_id,
                now=self.clock(),
                lease_seconds=self.lease_seconds,
            )

    @staticmethod
    def _required_execution(record: RunRecord) -> RunExecution:
        if record.execution is None:
            raise WorkerError(f"Run {record.run_id} is missing execution metadata")
        return record.execution

    @staticmethod
    def _interruption_kind(interruption: BaseException) -> str:
        if isinstance(interruption, KeyboardInterrupt):
            return "keyboard_interrupt"
        if isinstance(interruption, SystemExit):
            return "system_exit"
        return "process_interrupted"

    def _close_synthesizer(self) -> None:
        """Close the outer synthesizer or its wrapped adapter exactly once per attempt."""

        candidate: object | None = self.synthesizer
        visited: set[int] = set()
        while candidate is not None and id(candidate) not in visited:
            visited.add(id(candidate))
            close = getattr(candidate, "close", None)
            if callable(close):
                with suppress(Exception):
                    close()
                return
            candidate = getattr(candidate, "inner", None)

    def _persist_synthesizer_diagnostics(self, run_id: UUID) -> None:
        """Copy bounded adapter diagnostics beside the durable run record."""

        diagnostics = getattr(self.synthesizer, "diagnostics", None)
        if not callable(diagnostics):
            return
        with suppress(Exception):
            for message in diagnostics():
                self.worker_log.record(
                    run_id,
                    occurred_at=self.clock(),
                    source="tts",
                    message=str(message),
                )

    def _checkpoint_cache_hit(
        self,
        running: RunRecord,
        segment: SpeechSegment,
        cache_key: str,
        audio: PcmAudio,
    ) -> None:
        previous = self.checkpoints.load_optional(running.run_id, segment.id)
        now = self.clock()
        if previous is not None and previous.cache_key == cache_key:
            attempt_count = previous.attempt_count
            started_at = previous.started_at
            failures = list(previous.failures)
        else:
            attempt_count = 0
            started_at = now
            failures = []
        checkpoint = self.checkpoints.completed_checkpoint(
            run_id=running.run_id,
            segment_id=segment.id,
            cache_key=cache_key,
            audio=audio,
            attempt_count=attempt_count,
            cache_hit=True,
            started_at=started_at,
            completed_at=now,
            failures=failures,
        )
        self.checkpoints.save(checkpoint, audio=audio)

    def _synthesize_with_retry(
        self,
        running: RunRecord,
        segment: SpeechSegment,
        cache_key: str,
        *,
        on_inference: Callable[[], None],
    ) -> PcmAudio:
        previous = self.checkpoints.load_optional(running.run_id, segment.id)
        if previous is not None and previous.cache_key == cache_key:
            attempt_count = previous.attempt_count
            failures = list(previous.failures)
            started_at = previous.started_at
        else:
            attempt_count = 0
            failures = []
            started_at = self.clock()

        for invocation_attempt in range(1, self.retry_policy.max_attempts + 1):
            attempt_count += 1
            running_checkpoint = SegmentCheckpoint(
                run_id=running.run_id,
                segment_id=segment.id,
                cache_key=cache_key,
                status=CheckpointStatus.RUNNING,
                attempt_count=attempt_count,
                started_at=started_at,
                updated_at=self.clock(),
                failures=failures,
            )
            self.checkpoints.save(running_checkpoint)

            try:
                on_inference()
                audio = self.synthesizer.synthesize(segment)
                integrity_error = pcm_integrity_error(audio)
                if integrity_error is not None:
                    self._record_attempt_failure(
                        running_checkpoint,
                        failures,
                        FailureKind.QUALITY,
                        f"AudioIntegrityError: {integrity_error}",
                        final=invocation_attempt == self.retry_policy.max_attempts,
                    )
                    if invocation_attempt == self.retry_policy.max_attempts:
                        raise _SegmentProcessingError(
                            f"Segment {segment.id} failed quality checks after "
                            f"{self.retry_policy.max_attempts} attempts: {integrity_error}"
                        )
                    self.sleeper(self.retry_policy.delay_after_failure(invocation_attempt))
                    continue
            except TransientSynthesisError as error:
                self._record_attempt_failure(
                    running_checkpoint,
                    failures,
                    FailureKind.TRANSIENT,
                    f"{type(error).__name__}: {error}",
                    final=invocation_attempt == self.retry_policy.max_attempts,
                )
                if invocation_attempt == self.retry_policy.max_attempts:
                    raise _SegmentProcessingError(
                        f"Segment {segment.id} exhausted "
                        f"{self.retry_policy.max_attempts} transient attempts: {error}"
                    ) from error
                self.sleeper(self.retry_policy.delay_after_failure(invocation_attempt))
                continue
            except FatalSynthesisError as error:
                self._record_attempt_failure(
                    running_checkpoint,
                    failures,
                    FailureKind.FATAL,
                    f"{type(error).__name__}: {error}",
                    final=True,
                )
                raise _SegmentProcessingError(
                    f"Segment {segment.id} failed fatally without retry: {error}"
                ) from error
            except _SegmentProcessingError:
                raise
            except Exception as error:
                self._record_attempt_failure(
                    running_checkpoint,
                    failures,
                    FailureKind.UNEXPECTED,
                    f"{type(error).__name__}: {error}",
                    final=True,
                )
                raise _SegmentProcessingError(
                    f"Segment {segment.id} failed unexpectedly without retry: {error}"
                ) from error

            completed_at = self.clock()
            completed_checkpoint = self.checkpoints.completed_checkpoint(
                run_id=running.run_id,
                segment_id=segment.id,
                cache_key=cache_key,
                audio=audio,
                attempt_count=attempt_count,
                cache_hit=False,
                started_at=started_at,
                completed_at=completed_at,
                failures=failures,
            )
            self.checkpoints.save(completed_checkpoint, audio=audio)
            self.cache.store(
                cache_key,
                audio,
                synthesis_inputs=synthesis_input_bytes(segment, self.synthesizer),
                synthesizer_identity=self.synthesizer.cache_identity,
                created_at=completed_at,
            )
            return audio

        return self._unreachable_retry_loop()

    def _record_attempt_failure(
        self,
        checkpoint: SegmentCheckpoint,
        failures: list[SegmentAttemptFailure],
        kind: FailureKind,
        error: str,
        *,
        final: bool,
    ) -> None:
        occurred_at = self.clock()
        failures.append(
            SegmentAttemptFailure(
                attempt=checkpoint.attempt_count,
                kind=kind,
                error=error,
                occurred_at=occurred_at,
            )
        )
        failed_checkpoint = SegmentCheckpoint(
            run_id=checkpoint.run_id,
            segment_id=checkpoint.segment_id,
            cache_key=checkpoint.cache_key,
            status=CheckpointStatus.FAILED if final else CheckpointStatus.RUNNING,
            attempt_count=checkpoint.attempt_count,
            started_at=checkpoint.started_at,
            updated_at=occurred_at,
            failures=failures,
        )
        self.checkpoints.save(failed_checkpoint)

    @staticmethod
    def _unreachable_retry_loop() -> Never:
        raise AssertionError("retry loop exited without returning or raising")
