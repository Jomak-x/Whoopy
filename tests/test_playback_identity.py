from __future__ import annotations

import json
import struct
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_webui_review_boundaries import _application, _rendered_script_run
from whoopy.audio.models import AudioQualityReport, QualityCheck
from whoopy.webui.api import create_api
from whoopy.webui.audio_inspection import current_audio_artifacts


def test_current_playback_reads_bounded_pcm_without_repeating_sample_quality(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application = _application(tmp_path)
    store, run_id = _rendered_script_run(application)
    audio = store.audio_path(run_id)
    original_readframes = wave.Wave_read.readframes
    original_read_bytes = Path.read_bytes
    reads: list[int] = []

    def bounded_readframes(source: wave.Wave_read, nframes: int) -> bytes:
        assert 0 < nframes <= 32_768
        reads.append(nframes)
        return original_readframes(source, nframes)

    def bounded_read_bytes(path: Path) -> bytes:
        assert path != audio, "Current playback must not load the entire narration"
        return original_read_bytes(path)

    def forbidden_sample_scan(_pcm: bytes) -> None:
        pytest.fail("Current playback must reuse retained sample quality evidence")

    monkeypatch.setattr(wave.Wave_read, "readframes", bounded_readframes)
    monkeypatch.setattr(Path, "read_bytes", bounded_read_bytes)
    monkeypatch.setattr("whoopy.audio.quality._samples_from_pcm", forbidden_sample_scan)
    record = store.load(run_id)
    path, manifest, quality = current_audio_artifacts(store, record)
    assert path == audio
    assert quality.passed
    assert manifest.total_frames > 32_768
    assert sum(reads) == manifest.total_frames
    assert len(reads) > 1
    assert application.audio_path(run_id) == audio
    assert application.artifact_path(run_id, "quality") == store.quality_path(run_id)
    summary = application.run(run_id)
    assert summary is not None and summary["has_audio"] and summary["quality_passed"]
    listed = application.list_runs()
    assert len(listed) == 1 and listed[0]["has_audio"] and listed[0]["quality_passed"]
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/v1/runs/{run_id}/audio")
    assert response.status_code == 200
    assert len(response.content) == audio.stat().st_size


@pytest.mark.parametrize(
    "damage",
    [
        "missing_audio",
        "invalid_audio",
        "changed_pcm",
        "truncated_pcm",
        "wrong_rate",
        "zero_rate",
        "missing_quality_hash",
        "wrong_quality_frames",
        "missing_quality_check",
        "failed_quality_check",
        "wrong_segment_hash",
        "noncontiguous_segments",
        "wrong_pause_frames",
    ],
)
def test_current_playback_rejects_damaged_bound_artifacts(tmp_path: Path, damage: str) -> None:
    application = _application(tmp_path)
    store, run_id = _rendered_script_run(application)
    audio = store.audio_path(run_id)
    assert application.audio_path(run_id) == audio
    if damage == "missing_audio":
        audio.unlink()
    elif damage == "invalid_audio":
        audio.write_bytes(b"not a WAV container")
    elif damage == "changed_pcm":
        payload = audio.read_bytes()
        audio.write_bytes(payload[:-1] + bytes([payload[-1] ^ 1]))
    elif damage == "truncated_pcm":
        audio.write_bytes(audio.read_bytes()[:-2])
    elif damage == "zero_rate":
        mutable_payload = bytearray(audio.read_bytes())
        struct.pack_into("<I", mutable_payload, 24, 0)
        audio.write_bytes(mutable_payload)
    elif damage == "wrong_rate":
        with wave.open(str(audio), "rb") as source:
            params = source.getparams()
            pcm = source.readframes(source.getnframes())
        with wave.open(str(audio), "wb") as target:
            target.setparams(params._replace(framerate=params.framerate + 1))
            target.writeframes(pcm)
    elif damage in {
        "missing_quality_hash",
        "wrong_quality_frames",
        "missing_quality_check",
        "failed_quality_check",
    }:
        path = store.quality_path(run_id)
        quality = json.loads(path.read_text())
        if damage == "missing_quality_hash":
            quality["pcm_sha256"] = None
        elif damage == "wrong_quality_frames":
            quality["frame_count"] += 1
        elif damage == "missing_quality_check":
            quality["checks"] = [
                check for check in quality["checks"] if check["name"] != "exact_silence"
            ]
        else:
            quality["checks"][0]["passed"] = False
            quality["passed"] = False
        path.write_text(json.dumps(quality))
    else:
        path = store.audio_manifest_path(run_id)
        manifest = json.loads(path.read_text())
        if damage == "wrong_segment_hash":
            manifest["segments"][0]["pcm_sha256"] = "0" * 64
        elif damage == "noncontiguous_segments":
            manifest["segments"][1]["start_frame"] += 1
        else:
            manifest["segments"][1]["frame_count"] += 1
            manifest["segments"][1]["end_frame"] += 1
        path.write_text(json.dumps(manifest))

    assert application.audio_path(run_id) is None
    assert application.artifact_path(run_id, "quality") is None
    summary = application.run(run_id)
    assert summary is not None and summary["has_audio"] is False
    assert summary["quality_passed"] is None
    listed = application.list_runs()
    assert len(listed) == 1 and listed[0]["has_audio"] is False
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        assert client.get(f"/api/v1/runs/{run_id}/audio").status_code == 404


def test_current_playback_retains_additional_failed_quality_evidence(tmp_path: Path) -> None:
    application = _application(tmp_path)
    store, run_id = _rendered_script_run(application)
    path = store.quality_path(run_id)
    quality = AudioQualityReport.model_validate_json(path.read_text())
    quality = quality.model_copy(
        update={
            "passed": False,
            "checks": [
                *quality.checks,
                QualityCheck(name="independent_review", passed=False, detail="Review pending"),
            ],
        }
    )
    path.write_text(quality.model_dump_json())

    assert application.audio_path(run_id) == store.audio_path(run_id)
    assert application.artifact_path(run_id, "quality") == path
    summary = application.run(run_id)
    assert summary is not None and summary["has_audio"] is True
    assert summary["quality_passed"] is False
    assert path.read_text() == quality.model_dump_json()
