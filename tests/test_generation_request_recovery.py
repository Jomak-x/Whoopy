"""Admitted request identity survives loss of the browser's POST response."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from whoopy.pipeline import RunStore
from whoopy.webui.api import create_api
from whoopy.webui.server import LocalWebApplication


def test_read_only_run_identity_survives_without_live_task(tmp_path: Path) -> None:
    app = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models/managed"),
        runs_directory=Path("runs"),
    )
    record = RunStore(app.runs_directory).create("A precise local request", audience="studio")
    run_id = str(record.run_id)
    request = {
        "mode": "prompt",
        "text": record.prompt,
        "profile": "standard",
        "minutes": 3.0,
        "tts_model": "kokoro",
        "voice": "am_michael",
        "speed": 0.6,
        "surface": "studio",
        "writing_method": "recipe_whole_session",
        "qwen_reference": "/private/never-publish.wav",
        "internal_path": "/private/never-publish.json",
        "moss_instruction": "Internal inactive default",
    }
    app._write_web_launch_request(run_id, request)
    assert app._tasks == {}
    with TestClient(create_api(app), base_url="http://127.0.0.1") as client:
        detail = client.get(f"/api/v1/runs/{run_id}")
        listing = client.get("/api/v1/runs")
    assert detail.status_code == listing.status_code == 200
    expected = {
        key: value
        for key, value in request.items()
        if key not in {"qwen_reference", "internal_path", "moss_instruction"}
    }
    assert detail.json()["generation_request"] == expected
    assert listing.json()["runs"][0]["generation_request"] == expected
    assert "never-publish" not in json.dumps([detail.json(), listing.json()])


def test_corrupt_or_absent_request_never_supplies_recovery_identity(tmp_path: Path) -> None:
    app = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models/managed"),
        runs_directory=Path("runs"),
    )
    record = RunStore(app.runs_directory).create("A local request")
    run_id = str(record.run_id)
    assert app.generation_request_summary(run_id) is None
    app._write_web_launch_request(run_id, {"profile": "standard", "text": record.prompt})
    path = app._web_launch_request_path(run_id)
    value = json.loads(path.read_text())
    value["payload"]["profile"] = "high"
    path.write_text(json.dumps(value))
    assert app.generation_request_summary(run_id) is None
    assert app.run(run_id)["generation_request"] is None  # type: ignore[index]
