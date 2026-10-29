from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from whoopy.pipeline import RunStore
from whoopy.webui.api import STUDIO_MOSS_INSTRUCTION, create_api
from whoopy.webui.server import GenerationTask, LocalWebApplication


def _application(tmp_path: Path) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )


@pytest.mark.parametrize("mode", ["prompt", "script"])
@pytest.mark.parametrize("tts_model", ["kokoro", "fish-1.4", "moss-local-v1.5"])
def test_retained_generation_payload_reaches_application(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, tts_model: str
) -> None:
    application = _application(tmp_path)
    dispatched: list[dict[str, Any]] = []

    def start(payload: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(payload)
        return {"task_id": "accepted"}

    monkeypatch.setattr(application, "start_generation", start)
    body = {
        "mode": mode,
        "text": "Notice the support beneath you.",
        "tts_model": tts_model,
        "voice": "af_heart",
        "speed": 0.6,
        "moss_language": "English",
        "moss_instruction": STUDIO_MOSS_INSTRUCTION,
        "moss_use_reference": True,
    }
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        legacy = client.post("/api/generate", json=body)
        versioned = client.post("/api/v1/generate", json=body)

    assert legacy.status_code == 202
    assert legacy.json() == {"task_id": "accepted"}
    assert dispatched[0]["mode"] == mode
    assert dispatched[0]["tts_model"] == tts_model
    if tts_model.startswith("moss-"):
        assert versioned.status_code == 202
        assert len(dispatched) == 2
        assert dispatched[0]["moss_language"] == "English"
        assert dispatched[0]["moss_instruction"] == STUDIO_MOSS_INSTRUCTION
        assert dispatched[0]["moss_use_reference"] is True
    else:
        assert versioned.status_code == 422
        assert len(dispatched) == 1
        assert (
            not {"moss_language", "moss_instruction", "moss_use_reference"} & dispatched[0].keys()
        )


@pytest.mark.parametrize(
    "extra",
    [
        {"voice_reference": "unsupported"},
        {"qwen_speaker": "Ryan"},
        {"unexpected": True},
        {"text": ""},
    ],
)
def test_legacy_normalization_preserves_other_request_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: dict[str, object]
) -> None:
    application = _application(tmp_path)
    dispatched: list[object] = []
    monkeypatch.setattr(application, "start_generation", dispatched.append)
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/generate",
            json={"mode": "script", "text": "Pause.", "moss_language": "English", **extra},
        )

    assert response.status_code == 422
    assert dispatched == []


@pytest.mark.parametrize("launching", [True, False])
def test_retained_cancel_route_reaches_active_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launching: bool
) -> None:
    application = _application(tmp_path)
    stopped: list[object] = []

    class Process:
        def poll(self) -> None:
            return None

    process = None if launching else Process()
    monkeypatch.setattr(application, "_stop_process", stopped.append)
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        record = RunStore(tmp_path / "runs").create("Pause.")
        task_id = str(record.run_id)
        task = GenerationTask(task_id=task_id, run_id=task_id, launching=launching, process=process)
        application._tasks[task_id] = task
        response = client.post(f"/api/tasks/{task_id}/cancel", json={})

    assert response.status_code == 202
    assert response.json()["task_id"] == task_id
    assert task.cancel_requested is True
    assert stopped == ([] if launching else [process])


def test_retained_cancel_route_reports_unknown_task(tmp_path: Path) -> None:
    with TestClient(create_api(_application(tmp_path)), base_url="http://127.0.0.1") as client:
        missing = client.post(f"/api/tasks/{uuid4()}/cancel", json={})
        malformed = client.post("/api/tasks/not-a-uuid/cancel", json={})

    assert missing.status_code == 404
    assert malformed.status_code == 422


@pytest.mark.parametrize("tts_model", ["moss-local-v1.5", "moss-v1.5"])
@pytest.mark.parametrize("mode", ["prompt", "script"])
def test_custom_moss_instruction_preserves_legacy_studio_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tts_model: str, mode: str
) -> None:
    application = _application(tmp_path)
    dispatched: list[dict[str, Any]] = []

    def start(payload: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(payload)
        return {"task_id": "accepted"}

    monkeypatch.setattr(application, "start_generation", start)
    body = {
        "mode": mode,
        "text": "Notice the room around you.",
        "tts_model": tts_model,
        "moss_instruction": "Speak gently with a steady, low voice.",
        "moss_use_reference": False,
    }
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        legacy = client.post("/api/generate", json=body)
        versioned = client.post("/api/v1/generate", json=body)
        lab = client.post("/api/v1/generate", json={**body, "surface": "lab"})
        wrong_seed = client.post("/api/generate", json={**body, "seed": 43})
        overlong = client.post("/api/generate", json={**body, "moss_instruction": "x" * 301})

    assert legacy.status_code == lab.status_code == 202
    assert versioned.status_code == wrong_seed.status_code == overlong.status_code == 422
    assert [payload["surface"] for payload in dispatched] == ["studio", "lab"]
    assert all(payload["moss_instruction"] == body["moss_instruction"] for payload in dispatched)
    assert all(payload["moss_use_reference"] is False for payload in dispatched)
