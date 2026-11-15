"""Preview transport never promotes a model or bypasses asset integrity."""

from __future__ import annotations

import hashlib
import json
import wave
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from whoopy.webui.api import create_api
from whoopy.webui.server import LocalWebApplication


def _application(tmp_path: Path) -> tuple[LocalWebApplication, Path]:
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models/managed"),
        runs_directory=Path("runs"),
    )
    root = tmp_path / "models/voice-previews"
    root.mkdir(parents=True)
    audio = root / "transport-fixture.wav"
    with wave.open(str(audio), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * 2400)
    record = {
        "model_id": "kokoro",
        "voice_id": "am_michael",
        "path": audio.name,
        "sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "model_revision": "transport-fixture-only",
        "recorded_at": "2026-11-05",
        "human_quality": "unrated",
    }
    (root / "index.json").write_text(json.dumps({"schema_version": 1, "previews": [record]}))
    return application, audio


def test_api_serves_verified_bytes_and_rejects_changed_audio(tmp_path: Path) -> None:
    application, audio = _application(tmp_path)
    expected = audio.read_bytes()
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.get("/api/v1/voice-previews/kokoro/am_michael")
        assert response.status_code == 200
        assert response.content == expected
        assert response.headers["content-type"] == "audio/wav"
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert client.get("/api/v1/voice-previews/kokoro/AM_MICHAEL").status_code == 404
        assert client.get("/api/v1/voice-previews/unknown/am_michael").status_code == 404
        audio.write_bytes(expected[:-2] + b"\x01\x00")
        assert client.get("/api/v1/voice-previews/kokoro/am_michael").status_code == 404


def test_preview_capability_does_not_change_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    application, _audio = _application(tmp_path)
    environment: dict[str, Any] = {
        "ok": True,
        "speech_models": {
            "kokoro": {"ready": False, "state": "missing", "capabilities": {"voices": []}}
        },
    }
    monkeypatch.setattr(application, "_compute_environment_status", lambda: environment)
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        payload = client.get("/api/v1/status").json()
    model = payload["speech_models"]["kokoro"]
    assert model["ready"] is False
    assert model["state"] == "missing"
    assert model["capabilities"]["voice_preview_urls"] == {
        "am_michael": "/api/v1/voice-previews/kokoro/am_michael"
    }
    preview = payload["voice_previews"]["entries"][0]
    assert model["capabilities"]["voice_preview_metadata"] == {"am_michael": preview}
    assert preview["human_quality"] == "unrated"
    assert preview["managed_ready"] is False
    assert str(tmp_path) not in json.dumps(payload)
