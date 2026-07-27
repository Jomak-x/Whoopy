"""Foreground worker with per-segment caching, retry, and durable resume."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Never
from uuid import UUID

from whoopy.audio import TimelineWaveRenderer
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.quality import pcm_integrity_error
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    SpeechSynthesizer,
    TransientSynthesisError,
    cache_key_for,
    synthesis_input_bytes,
)
from whoopy.pipeline.cache import SegmentCache
from whoopy.pipeline.checkpoints import (
    CheckpointStatus,
    FailureKind,
    SegmentAttemptFailure,
    SegmentCheckpoint,
    SegmentCheckpointStore,
)
from whoopy.pipeline.runs import (
    AUDIO_FILENAME,
    AUDIO_MANIFEST_FILENAME,
    QUALITY_FILENAME,
    TIMELINE_FILENAME,
    RunRecord,
    RunRecovery,
    RunStatus,
    RunStore,
    RunStoreError,
)
from whoopy.timeline import SpeechSegment, Timeline, build_fixture_timeline

Clock = Callable[[], datetime]
Sleeper = Callable[[float], None]
TimelineBuilder = Callable[[RunRecord, datetime], Timeline]


class WorkerError(RuntimeError):
    """Raised when a run cannot be claimed, recovered, or completed."""


class _SegmentProcessingError(RuntimeError):
    """Internal error carrying a clear segment-level failure message."""


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
    ) -> None:
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

    def process(self, run_id: UUID | str) -> RunRecord:
        """Start one queued run."""

        record = self.store.load(run_id)
        if record.status is not RunStatus.QUEUED:
            raise WorkerError(
                f"Run {record.run_id} is {record.status.value}; only queued runs can be processed"
            )
        return self._execute(record, is_resume=False)

    def resume(self, run_id: UUID | str) -> RunRecord:
        """Resume a failed or interrupted run from verified segment checkpoints."""

        record = self.store.load(run_id)
        if record.status not in (RunStatus.FAILED, RunStatus.RUNNING):
            raise WorkerError(
                f"Run {record.run_id} is {record.status.value}; "
                "only failed or running runs can be resumed"
            )
        return self._execute(record, is_resume=True)

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
        running = record.transition(
            RunStatus.RUNNING,
            updated_at=self.clock(),
            recovery=recovery,
        )
        self.store.save(running)
        current_segment_id: str | None = None

        try:
            timeline = self._load_or_build_timeline(running)
            speech_segments = [
                segment for segment in timeline.segments if isinstance(segment, SpeechSegment)
            ]
            recovery = _updated_recovery(
                recovery,
                speech_segments_total=len(speech_segments),
                speech_segments_completed=0,
            )
            running = self._save_running_progress(running, recovery)

            prepared_audio: dict[str, PcmAudio] = {}
            cache_keys: dict[str, str] = {}
            for segment in speech_segments:
                current_segment_id = segment.id
                cache_key = cache_key_for(segment, self.synthesizer)
                cache_keys[segment.id] = cache_key

                checkpoint_hit = self.checkpoints.load_completed(
                    running.run_id,
                    segment.id,
                    cache_key=cache_key,
                )
                if checkpoint_hit is not None:
                    audio = checkpoint_hit.audio
                    recovery = _updated_recovery(
                        recovery,
                        checkpoint_reuses=recovery.checkpoint_reuses + 1,
                    )
                else:
                    cache_hit = self.cache.load(
                        cache_key,
                        expected_synthesizer=self.synthesizer,
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
                        running = self._save_running_progress(running, recovery)
                        audio = self._synthesize_with_retry(
                            running,
                            segment,
                            cache_key,
                        )

                prepared_audio[segment.id] = audio
                recovery = _updated_recovery(
                    recovery,
                    speech_segments_completed=recovery.speech_segments_completed + 1,
                )
                running = self._save_running_progress(running, recovery)

            current_segment_id = None
            rendered = self.renderer.render(
                timeline,
                speech_audio=prepared_audio,
                speech_cache_keys=cache_keys,
            )
            self.store.write_audio(running.run_id, rendered.wave_bytes)
            self.store.write_audio_manifest(running.run_id, rendered.manifest)
            self.store.write_quality(running.run_id, rendered.quality)
            completed = running.transition(
                RunStatus.COMPLETED,
                updated_at=self.clock(),
                timeline_artifact=TIMELINE_FILENAME,
                audio_artifact=AUDIO_FILENAME,
                audio_manifest_artifact=AUDIO_MANIFEST_FILENAME,
                quality_artifact=QUALITY_FILENAME,
                recovery=recovery,
            )
            self.store.save(completed)
            return completed
        except Exception as error:
            failed_recovery = _updated_recovery(
                recovery,
                failed_segment_id=current_segment_id,
            )
            failed = running.transition(
                RunStatus.FAILED,
                updated_at=self.clock(),
                recovery=failed_recovery,
                error=f"{type(error).__name__}: {error}",
            )
            try:
                self.store.save(failed)
            except RunStoreError as save_error:
                raise WorkerError(
                    f"Run {running.run_id} failed and its failure state could not be saved: "
                    f"{save_error}"
                ) from error
            raise WorkerError(f"Run {running.run_id} failed: {error}") from error

    def _load_or_build_timeline(self, running: RunRecord) -> Timeline:
        """Reuse the canonical timeline on resume, or create it once."""

        if self.store.timeline_path(running.run_id).is_file():
            return self.store.load_timeline(running.run_id)
        timeline = self.timeline_builder(running, self.clock())
        self.store.write_timeline(running.run_id, timeline)
        return timeline

    def _save_running_progress(
        self,
        running: RunRecord,
        recovery: RunRecovery,
    ) -> RunRecord:
        updated = running.transition(
            RunStatus.RUNNING,
            updated_at=self.clock(),
            recovery=recovery,
        )
        self.store.save(updated)
        return updated

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
