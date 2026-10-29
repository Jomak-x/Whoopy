from __future__ import annotations

import io
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_takes import RUN_ID, _completed_run
from tests.test_task_finalization import _application
from whoopy.pipeline.takes import SegmentTakeStore
from whoopy.webui.api import create_api


def test_take_preview_serves_verified_pcm(tmp_path: Path) -> None:
    store, audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    take = takes.capture_active(RUN_ID, "speech-0001", source="original")
    path = takes.audio_path(RUN_ID, "speech-0001", take.take_id)

    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.get(
            f"/api/v1/runs/{RUN_ID}/segments/speech-0001/takes/{take.take_id}/audio"
        )

    assert response.status_code == 200
    assert response.content == path.read_bytes()
    with wave.open(io.BytesIO(response.content), "rb") as preview:
        assert preview.getnchannels() == 1
        assert preview.getsampwidth() == 2
        assert preview.getframerate() == audio.sample_rate
        assert preview.getnframes() == audio.frame_count
        assert preview.readframes(audio.frame_count + 1) == audio.pcm_s16le


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "malformed",
        "truncated_header",
        "truncated_frames",
        "replaced_frames",
        "sample_rate",
        "channels",
        "sample_width",
        "frame_count",
    ],
)
def test_take_preview_rejects_unbound_wav_without_repair(tmp_path: Path, damage: str) -> None:
    store, audio = _completed_run(tmp_path / "runs")
    takes = SegmentTakeStore(store)
    take = takes.capture_active(RUN_ID, "speech-0001", source="original")
    path = takes.audio_path(RUN_ID, "speech-0001", take.take_id)
    original_wav = path.read_bytes()
    if damage == "missing":
        path.unlink()
    elif damage == "malformed":
        path.write_bytes(b"not a wave file")
    elif damage == "truncated_header":
        path.write_bytes(original_wav[:20])
    elif damage == "truncated_frames":
        path.write_bytes(original_wav[:-2])
    else:
        pcm = audio.pcm_s16le
        if damage == "replaced_frames":
            pcm = b"\x11\x00" * audio.frame_count
        elif damage == "frame_count":
            pcm = pcm[:-2]
        with wave.open(str(path), "wb") as output:
            output.setnchannels(2 if damage == "channels" else 1)
            output.setsampwidth(1 if damage == "sample_width" else 2)
            output.setframerate(16_000 if damage == "sample_rate" else audio.sample_rate)
            output.writeframes(pcm)
    run_directory = store.run_directory(RUN_ID)
    before = {
        item.relative_to(run_directory): item.read_bytes()
        for item in run_directory.rglob("*")
        if item.is_file()
    }

    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        response = client.get(
            f"/api/v1/runs/{RUN_ID}/segments/speech-0001/takes/{take.take_id}/audio"
        )

    assert response.status_code == 409
    assert "Take preview WAV" in response.json()["error"]
    assert takes.selected(RUN_ID, "speech-0001") == take.take_id
    after = {
        item.relative_to(run_directory): item.read_bytes()
        for item in run_directory.rglob("*")
        if item.is_file()
    }
    assert after == before
