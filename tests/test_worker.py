from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest

from whoopy.artifacts import TargetPlatform
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.processing import SpeechProcessingSettings
from whoopy.audio.synthesis import TransientSynthesisError
from whoopy.pipeline.generation import RunModelMetadata, ScriptRunConfig, TTSRunSettings
from whoopy.pipeline.runs import RunStage, RunStatus, RunStore
from whoopy.pipeline.worker import (
    LocalWorker,
    RetryPolicy,
    WorkerError,
    fit_generated_timeline_to_real_speech,
)
from whoopy.timeline import SilenceSegment, SpeechSegment, Timeline, build_script_timeline

RUN_ID = UUID("22222222-2222-4222-8222-222222222222")
START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


def _clock(values: Iterator[datetime]) -> datetime:
    return next(values)


def test_generated_timeline_fits_quiet_to_real_tts_duration() -> None:
    timeline = Timeline(
        schema_version=4,
        run_id=RUN_ID,
        created_at=START,
        source="generated_prompt",
        segments=[
            SpeechSegment(id="speech-a", text="Arrive here."),
            SilenceSegment(id="quiet-a", duration_ms=5_000),
            SpeechSegment(id="speech-b", text="Return gently."),
            SilenceSegment(id="quiet-b", duration_ms=5_000),
        ],
    )
    twenty_seconds = PcmAudio(pcm_s16le=b"\x01\x00" * 24_000 * 20)

    fitted = fit_generated_timeline_to_real_speech(
        timeline,
        speech_audio={"speech-a": twenty_seconds, "speech-b": twenty_seconds},
        target_seconds=60,
    )

    quiet = [segment.duration_ms for segment in fitted.segments if segment.type == "SILENCE"]
    assert quiet == [10_000, 10_000]


def test_real_speech_fit_respects_recipe_quiet_cap() -> None:
    timeline = Timeline(
        schema_version=4,
        run_id=RUN_ID,
        created_at=START,
        source="generated_prompt",
        segments=[
            SpeechSegment(id="speech-a", text="Listen here."),
            SilenceSegment(id="quiet-a", duration_ms=15_000),
            SpeechSegment(id="speech-b", text="Continue listening."),
            SilenceSegment(id="quiet-b", duration_ms=1_000),
        ],
    )
    speech = PcmAudio(pcm_s16le=b"\x01\x00" * 24_000 * 20)
    fitted = fit_generated_timeline_to_real_speech(
        timeline,
        speech_audio={"speech-a": speech},
        target_seconds=40,
        maximum_silence_ms=16_000,
    )
    assert [s.duration_ms for s in fitted.segments if s.type == "SILENCE"] == [16_000, 4_000]
    with pytest.raises(WorkerError, match="unprepared long silence"):
        fit_generated_timeline_to_real_speech(
            timeline,
            speech_audio={"speech-a": speech},
            target_seconds=100,
            maximum_silence_ms=16_000,
        )


def test_controlled_authored_script_enforces_requested_duration_without_mutating_quiet(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path)
    metadata = RunModelMetadata(tts=FixtureSpeechSynthesizer.metadata)
    store.create_script_run(
        script="Arrive here.\n\n[pause: 2s]\n\nReturn gently.",
        source_name="controlled-lab.md",
        resolved_config=ScriptRunConfig(
            profile="basic",
            target=TargetPlatform(operating_system="linux", architecture="x86_64"),
            tts=TTSRunSettings(
                voice_name="fixture",
                speaker_id=0,
                speed=1,
                num_threads=1,
                provider="cpu",
                language="English",
            ),
            processing=SpeechProcessingSettings(),
            requested_duration_seconds=60,
        ),
        model_metadata=metadata,
        run_id=RUN_ID,
        created_at=START,
    )

    controlled_timeline = Timeline(
        schema_version=3,
        run_id=RUN_ID,
        created_at=START,
        source="script_file",
        segments=[
            SpeechSegment(id="speech-a", text="Arrive here."),
            SilenceSegment(id="quiet-a", duration_ms=2_000),
            SpeechSegment(id="speech-b", text="Return gently."),
        ],
    )

    with pytest.raises(WorkerError, match=r"outside the requested.*duration gate"):
        LocalWorker(
            store,
            timeline_builder=lambda _record, _created_at: controlled_timeline,
        ).process(RUN_ID)

    failed = store.load(RUN_ID)
    assert failed.status is RunStatus.FAILED
    quiet = store.load_timeline(RUN_ID).segments[1]
    assert isinstance(quiet, SilenceSegment)
    assert quiet.duration_ms == 2_000
    assert not store.quality_path(RUN_ID).exists()


def test_worker_completes_run_and_writes_valid_timeline(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    worker = LocalWorker(store, clock=lambda: START + timedelta(seconds=1))

    completed = worker.process(RUN_ID)

    assert completed.status is RunStatus.COMPLETED
    assert completed.timeline_artifact == "timeline.json"
    assert completed.audio_artifact == "narration.wav"
    assert completed.audio_manifest_artifact == "audio-manifest.json"
    assert completed.quality_artifact == "quality.json"
    assert completed.recovery is not None
    assert completed.recovery.speech_segments_completed == 2
    assert completed.recovery.cache_misses == 2
    assert completed.error is None
    assert store.load(RUN_ID) == completed

    timeline = store.load_timeline(RUN_ID)
    assert timeline.run_id == RUN_ID
    assert timeline.source == "phase_2_fixture_meditation"
    assert timeline.segments[0].type == "SPEECH"
    assert timeline.segments[0].text == "Breathe slowly."
    assert timeline.segments[1].type == "SILENCE"

    assert store.audio_path(RUN_ID).is_file()
    assert store.audio_manifest_path(RUN_ID).is_file()
    assert store.quality_path(RUN_ID).is_file()
    assert store.audio_path(RUN_ID).read_bytes().startswith(b"RIFF")


def test_worker_persists_truthful_run_telemetry_without_changing_provenance(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path)
    metadata = RunModelMetadata(tts=FixtureSpeechSynthesizer.metadata)
    store.create_script_run(
        script="Arrive here.\n\n[pause: 2s]\n\nReturn gently.",
        source_name="telemetry.md",
        resolved_config=ScriptRunConfig(
            profile="basic",
            target=TargetPlatform(operating_system="linux", architecture="x86_64"),
            tts=TTSRunSettings(
                voice_name="fixture",
                speaker_id=0,
                speed=1,
                num_threads=1,
                provider="cpu",
                language="English",
            ),
            processing=SpeechProcessingSettings(),
        ),
        model_metadata=metadata,
        run_id=RUN_ID,
        created_at=START,
    )

    LocalWorker(store).process(RUN_ID)

    saved = store.load_model_metadata(RUN_ID)
    assert saved.tts == metadata.tts
    assert saved.telemetry is not None
    assert saved.telemetry.hardware_before.snapshot is not None
    assert saved.telemetry.tts.load_elapsed.value is None
    assert "load-complete" in (saved.telemetry.tts.load_elapsed.unavailable_reason or "")
    assert saved.telemetry.tts.inference_calls.value == 2
    assert (saved.telemetry.tts.inference_elapsed.value or 0) >= 0
    assert (saved.telemetry.tts.inference_speed.value or 0) >= 0
    peak = saved.telemetry.tts.peak_process_tree_memory
    assert (peak.value or 0) > 0 or peak.unavailable_reason is not None


def test_worker_aggregates_measured_llm_attempt_telemetry_without_zero_placeholders(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    directory = store.raw_model_output_path(RUN_ID)
    directory.mkdir(parents=True)
    attempts = (
        (0.8, 0.2, 10, 0.25, 100_000_000),
        (1.2, 0.3, 15, 0.50, 140_000_000),
    )
    for index, (elapsed, load, tokens, token_elapsed, peak) in enumerate(attempts, 1):
        (directory / f"attempt-{index}.json").write_text(
            json.dumps(
                {
                    "elapsed_seconds": elapsed,
                    "telemetry": {
                        "load_elapsed_seconds": load,
                        "output_tokens": tokens,
                        "output_token_elapsed_seconds": token_elapsed,
                        "peak_process_tree_memory_bytes": peak,
                        "runtime_device": "Metal0",
                    },
                }
            ),
            encoding="utf-8",
        )

    telemetry = LocalWorker(store)._llm_telemetry(RUN_ID)

    assert telemetry.load_elapsed.value == pytest.approx(0.5)
    assert telemetry.inference_elapsed.value == pytest.approx(2.0)
    assert telemetry.inference_speed.unit == "tokens_per_second"
    assert telemetry.inference_speed.value == pytest.approx(25 / 0.75)
    assert telemetry.peak_process_tree_memory.value == 140_000_000
    assert telemetry.live_process_tree_memory_after.value is None
    assert telemetry.live_process_tree_memory_after.unavailable_reason is not None
    assert telemetry.inference_calls.value == 2
    assert telemetry.runtime_device == "Metal0"


def test_worker_marks_incomplete_llm_attempt_metrics_unavailable_instead_of_zero(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    directory = store.raw_model_output_path(RUN_ID)
    directory.mkdir(parents=True)
    (directory / "legacy.json").write_text(json.dumps({"elapsed_seconds": 0.4}), encoding="utf-8")

    telemetry = LocalWorker(store)._llm_telemetry(RUN_ID)

    assert telemetry.inference_elapsed.value == pytest.approx(0.4)
    for measurement in (
        telemetry.load_elapsed,
        telemetry.inference_speed,
        telemetry.peak_process_tree_memory,
        telemetry.live_process_tree_memory_after,
    ):
        assert measurement.value is None
        assert measurement.unavailable_reason


def test_worker_aggregates_direct_llama_cli_token_throughput_when_counts_are_absent(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    directory = store.raw_model_output_path(RUN_ID)
    directory.mkdir(parents=True)
    for index, speed in enumerate((100.0, 140.0), 1):
        (directory / f"attempt-{index}.json").write_text(
            json.dumps(
                {
                    "elapsed_seconds": 1.0,
                    "telemetry": {"output_tokens_per_second": speed},
                }
            ),
            encoding="utf-8",
        )

    telemetry = LocalWorker(store)._llm_telemetry(RUN_ID)

    assert telemetry.inference_speed.unit == "tokens_per_second"
    assert telemetry.inference_speed.value == pytest.approx(120.0)
    assert telemetry.inference_speed.unavailable_reason is None


def test_worker_refuses_to_process_a_completed_run_twice(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    worker = LocalWorker(store, clock=lambda: START + timedelta(seconds=1))
    worker.process(RUN_ID)

    with pytest.raises(WorkerError, match="only queued runs"):
        worker.process(RUN_ID)


def test_worker_saves_failed_state_when_timeline_building_fails(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    timestamps = iter(
        [
            START + timedelta(seconds=1),
            START + timedelta(seconds=2),
            START + timedelta(seconds=3),
        ]
    )

    def fail_to_build_timeline(_record: object, _created_at: datetime) -> Timeline:
        raise RuntimeError("fixture builder failed")

    worker = LocalWorker(
        store,
        clock=lambda: _clock(timestamps),
        timeline_builder=fail_to_build_timeline,
    )

    with pytest.raises(WorkerError, match="fixture builder failed"):
        worker.process(RUN_ID)

    failed = store.load(RUN_ID)
    assert failed.status is RunStatus.FAILED
    assert failed.error == "RuntimeError: fixture builder failed"
    assert failed.timeline_artifact is None
    assert not store.timeline_path(RUN_ID).exists()


@pytest.mark.parametrize("failures", [1, 3])
def test_inference_telemetry_counts_every_retry(tmp_path: Path, failures: int) -> None:
    class RetryingSynthesizer(FixtureSpeechSynthesizer):
        calls = 0

        def synthesize(self, segment: SpeechSegment) -> PcmAudio:
            self.calls += 1
            if self.calls <= failures:
                raise TransientSynthesisError("temporary failure")
            return super().synthesize(segment)

    store = RunStore(tmp_path / "runs")
    synthesizer = RetryingSynthesizer()
    store.create_script_run(
        script="Notice the support beneath you.",
        source_name="retry.md",
        resolved_config=ScriptRunConfig(
            profile="basic",
            target=TargetPlatform(operating_system="linux", architecture="x86_64"),
            tts=TTSRunSettings(
                voice_name="fixture",
                speaker_id=0,
                speed=1,
                num_threads=1,
                provider="cpu",
                language="English",
            ),
            processing=SpeechProcessingSettings(),
        ),
        model_metadata=RunModelMetadata(tts=synthesizer.metadata),
        run_id=RUN_ID,
        created_at=START,
    )
    worker = LocalWorker(
        store,
        synthesizer=synthesizer,
        retry_policy=RetryPolicy(max_attempts=3),
        sleeper=lambda _delay: None,
        timeline_builder=lambda record, created_at: build_script_timeline(
            run_id=record.run_id, script=store.load_script(record.run_id), created_at=created_at
        ),
    )
    if failures == 3:
        with pytest.raises(WorkerError, match="exhausted"):
            worker.process(RUN_ID)
    else:
        worker.process(RUN_ID)

    telemetry = store.load_model_metadata(RUN_ID).telemetry
    assert telemetry is not None
    assert synthesizer.calls == (3 if failures == 3 else 2)
    assert telemetry.tts.inference_calls.value == synthesizer.calls


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, RuntimeError])
def test_directory_flush_failure_preserves_committed_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interruption: type[BaseException],
) -> None:
    store = RunStore(tmp_path / "runs")
    store.create("Breathe slowly.", run_id=RUN_ID, created_at=START)
    flush = RunStore._fsync_directory
    interrupted = False

    def fail_flush_once(directory: Path) -> None:
        nonlocal interrupted
        durable = store.load(RUN_ID)
        if (
            not interrupted
            and directory == store.run_directory(RUN_ID)
            and durable.execution is not None
            and durable.execution.stage is RunStage.ASSEMBLING
        ):
            interrupted = True
            raise interruption("interrupted directory flush")
        flush(directory)

    monkeypatch.setattr(RunStore, "_fsync_directory", staticmethod(fail_flush_once))
    expected = KeyboardInterrupt if interruption is KeyboardInterrupt else WorkerError
    with pytest.raises(expected, match="interrupted directory flush"):
        LocalWorker(store).process(RUN_ID)

    saved = store.load(RUN_ID)
    assert interrupted
    assert saved.status is (
        RunStatus.INTERRUPTED if interruption is KeyboardInterrupt else RunStatus.FAILED
    )
    assert saved.execution is not None
    assert saved.execution.stage is RunStage.ASSEMBLING
