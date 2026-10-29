from __future__ import annotations

import base64
import runpy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from whoopy.adapters.tts.moss_tts import MossTTSAdapter, MossTTSSettings
from whoopy.pipeline import RunStatus, RunStore
from whoopy.pipeline.cache import SegmentCache
from whoopy.pipeline.worker import LocalWorker
from whoopy.timeline import Timeline, build_script_timeline

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 5, 17, 0, tzinfo=UTC)


def test_worker_protocol_rejects_unbounded_or_excessive_generation_caps() -> None:
    namespace = runpy.run_path(str(ROOT / "scripts/moss_tts_worker.py"))
    validate = namespace["_validated_generation_budget"]
    valid = {
        "duration_budget_strategy": "moss-text-pace-v1",
        "duration_tokens": 27,
        "max_new_tokens": 98,
    }

    assert validate(valid) == (27, 98)
    with pytest.raises(ValueError, match="duration_budget_strategy"):
        validate({"duration_tokens": 27, "max_new_tokens": 98})
    with pytest.raises(ValueError, match="duration-derived ceiling"):
        validate({**valid, "max_new_tokens": 1_536})
    with pytest.raises(ValueError, match="between 96 and 1536"):
        validate({**valid, "max_new_tokens": 2_048})


def test_full_worker_pipeline_gives_targetless_moss_speech_a_bounded_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference-audio")
    adapter = MossTTSAdapter(
        MossTTSSettings(
            runtime_directory=tmp_path / "runtime",
            worker_script=tmp_path / "worker.py",
            model_directory=tmp_path / "model",
            codec_directory=tmp_path / "codec",
            variant="moss-local-v1.5",
            reference_audio=reference,
        )
    )
    requests: list[dict[str, object]] = []
    pcm = b"".join(
        (1_000 if index % 2 else -1_000).to_bytes(2, "little", signed=True)
        for index in range(24_000)
    )

    class Controller:
        def request(self, payload: dict[str, object]) -> dict[str, object]:
            requests.append(payload)
            return {
                "status": "ok",
                "pcm_s16le": base64.b64encode(pcm).decode("ascii"),
            }

    monkeypatch.setattr(adapter, "_start", lambda: Controller())
    store = RunStore(tmp_path / "runs")
    record = store.create("Notice the support beneath you.", created_at=NOW)

    def timeline_builder(active: Any, created_at: datetime) -> Timeline:
        return build_script_timeline(
            run_id=active.run_id,
            script="[pace: slow]\nNotice the support beneath you.",
            created_at=created_at,
            source="script_file",
        )

    completed = LocalWorker(
        store,
        cache=SegmentCache(tmp_path / "cache"),
        synthesizer=adapter,
        timeline_builder=timeline_builder,
        clock=lambda: NOW,
        sleeper=lambda _seconds: None,
    ).process(record.run_id)

    assert completed.status is RunStatus.COMPLETED
    assert len(requests) == 1
    assert requests[0]["duration_budget_source"] == "text_pace_fallback"
    assert requests[0]["duration_tokens"] == 36
    assert requests[0]["max_new_tokens"] == 109
    assert int(requests[0]["max_new_tokens"]) < 2_048
