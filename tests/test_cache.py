from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.models import PcmAudio
from whoopy.audio.synthesis import cache_key_for
from whoopy.pipeline.cache import CACHE_AUDIO_FILENAME, SegmentCache
from whoopy.pipeline.runs import RunRecord, RunStore
from whoopy.pipeline.worker import LocalWorker
from whoopy.timeline import SpeechSegment

START = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
FIRST_RUN_ID = UUID("44444444-4444-4444-8444-444444444444")
SECOND_RUN_ID = UUID("55555555-5555-4555-8555-555555555555")


class CountingSynthesizer:
    metadata = FixtureSpeechSynthesizer.metadata.model_copy(
        update={"adapter_id": "tests.counting_fixture"}
    )
    cache_identity: str = metadata.cache_identity
    sample_rate: int = 24_000

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fixture = FixtureSpeechSynthesizer()

    def synthesize(self, segment: SpeechSegment) -> PcmAudio:
        self.calls.append(segment.text)
        return self.fixture.synthesize(segment)


def _process(
    store: RunStore,
    cache: SegmentCache,
    synthesizer: CountingSynthesizer,
    run_id: UUID,
    prompt: str,
) -> RunRecord:
    store.create(prompt, run_id=run_id, created_at=START)
    return LocalWorker(
        store,
        cache=cache,
        synthesizer=synthesizer,
        clock=lambda: START,
        sleeper=lambda _seconds: None,
    ).process(run_id)


def test_repeated_render_reuses_every_cached_speech_segment(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = CountingSynthesizer()

    first = _process(store, cache, synthesizer, FIRST_RUN_ID, "A steady breath.")
    second = _process(store, cache, synthesizer, SECOND_RUN_ID, "A steady breath.")

    assert len(synthesizer.calls) == 2
    assert first.recovery is not None
    assert first.recovery.cache_misses == 2
    assert second.recovery is not None
    assert second.recovery.cache_hits == 2
    assert second.recovery.cache_misses == 0
    assert cache.stats().entries == 2
    assert cache.stats().valid_entries == 2


def test_changed_synthesis_input_misses_only_the_changed_segment(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = CountingSynthesizer()

    _process(store, cache, synthesizer, FIRST_RUN_ID, "A steady breath.")
    second = _process(store, cache, synthesizer, SECOND_RUN_ID, "A softer breath.")

    assert len(synthesizer.calls) == 3
    assert second.recovery is not None
    assert second.recovery.cache_hits == 1
    assert second.recovery.cache_misses == 1


def test_corrupt_cache_entry_is_detected_and_regenerated(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    cache = SegmentCache(tmp_path / "cache")
    synthesizer = CountingSynthesizer()
    prompt = "A steady breath."

    _process(store, cache, synthesizer, FIRST_RUN_ID, prompt)
    segment = SpeechSegment(id="speech-0001", text=prompt)
    cache_key = cache_key_for(segment, synthesizer)
    audio_path = cache.entry_directory(cache_key) / CACHE_AUDIO_FILENAME
    audio_path.write_bytes(b"\x00\x00")
    assert cache.load(cache_key) is None

    second = _process(store, cache, synthesizer, SECOND_RUN_ID, prompt)

    assert len(synthesizer.calls) == 3
    assert second.recovery is not None
    assert second.recovery.cache_hits == 1
    assert second.recovery.cache_misses == 1
    assert cache.load(cache_key) is not None
