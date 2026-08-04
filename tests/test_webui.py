from __future__ import annotations

import http.client
import json
import threading
import time
from http.server import ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from whoopy.adapters.tts import MossTTSAdapter
from whoopy.pipeline import RunStore
from whoopy.webui.server import LocalWebApplication, _handler_factory


def _application(tmp_path: Path) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )


def test_static_interface_is_packaged() -> None:
    static = files("whoopy.webui").joinpath("static")

    html = static.joinpath("index.html").read_text(encoding="utf-8")
    assert "Whoopy Local Studio" in html
    assert static.joinpath("styles.css").is_file()
    assert static.joinpath("app.js").is_file()


def test_recent_runs_ignore_non_uuid_directories_and_expose_safe_artifacts(
    tmp_path: Path,
) -> None:
    application = _application(tmp_path)
    record = RunStore(tmp_path / "runs").create("A calm local test.")
    ignored = tmp_path / "runs" / ".cache"
    ignored.mkdir(parents=True)

    runs = application.list_runs()

    assert [run["run_id"] for run in runs] == [str(record.run_id)]
    assert runs[0]["title"] == "A calm local test."
    assert application.artifact_path(str(record.run_id), "run") == (
        tmp_path / "runs" / str(record.run_id) / "run.json"
    )
    assert application.artifact_path("../../etc", "run") is None
    assert application.artifact_path(str(record.run_id), "../run") is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({}, "mode must"),
        ({"mode": "prompt", "text": ""}, "Please enter"),
        ({"mode": "prompt", "text": "calm", "minutes": 0}, "between 1 and 30"),
        ({"mode": "script", "text": "calm", "voice": "unknown"}, "reviewed voices"),
        ({"mode": "script", "text": "calm", "speed": 3}, "between 0.5 and 1.2"),
        (
            {"mode": "script", "text": "calm", "moss_language": "Klingon"},
            "31 supported languages",
        ),
    ],
)
def test_generation_request_validation(
    tmp_path: Path,
    payload: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _application(tmp_path).start_generation(payload)


def test_prompt_task_uses_the_real_cli_contract(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            captured["command"] = command
            captured["kwargs"] = kwargs

        def communicate(self) -> tuple[str, str]:
            return json.dumps({"run_id": "23d079b0-5fe0-46d1-ae11-04038ef9d802"}), ""

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

    monkeypatch.setattr("whoopy.webui.server.subprocess.Popen", FakeProcess)

    created = application.start_generation(
        {
            "mode": "prompt",
            "text": "A gentle grounding pause.",
            "minutes": 1,
            "voice": "af_heart",
            "speed": 0.9,
        }
    )
    deadline = time.monotonic() + 2
    task = application.task(created["task_id"])
    while task is not None and task["status"] not in ("completed", "failed"):
        if time.monotonic() >= deadline:
            pytest.fail("background task did not finish")
        time.sleep(0.01)
        task = application.task(created["task_id"])

    assert task is not None
    assert task["status"] == "completed"
    assert task["run_id"] == "23d079b0-5fe0-46d1-ae11-04038ef9d802"
    command = captured["command"]
    assert command[:4] == [command[0], "-m", "whoopy", "generate"]
    assert "--draft-id" in command
    assert "--profile" in command
    assert command[command.index("--profile") + 1] == "standard"


def test_moss_controls_reach_the_real_cli_contract(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    application = _application(tmp_path)
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **kwargs: Any) -> None:
            captured["command"] = command

        def communicate(self) -> tuple[str, str]:
            return json.dumps({"run_id": "23d079b0-5fe0-46d1-ae11-04038ef9d802"}), ""

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

    monkeypatch.setattr(
        MossTTSAdapter,
        "availability_error",
        staticmethod(lambda *_paths: None),
    )
    monkeypatch.setattr("whoopy.webui.server.subprocess.Popen", FakeProcess)

    created = application.start_generation(
        {
            "mode": "script",
            "text": "Notice the quiet around you.",
            "tts_model": "moss-local-v1.5",
            "moss_language": "German",
            "moss_instruction": "Speak softly and thoughtfully.",
            "moss_use_reference": False,
        }
    )
    deadline = time.monotonic() + 2
    task = application.task(created["task_id"])
    while task is not None and task["status"] not in ("completed", "failed"):
        if time.monotonic() >= deadline:
            pytest.fail("background task did not finish")
        time.sleep(0.01)
        task = application.task(created["task_id"])

    assert task is not None and task["status"] == "completed"
    command = captured["command"]
    assert command[command.index("--tts-model") + 1] == "moss-local-v1.5"
    assert command[command.index("--moss-language") + 1] == "German"
    assert command[command.index("--moss-instruction") + 1] == ("Speak softly and thoughtfully.")
    assert "--moss-direct-voice" in command


def test_web_cli_passes_local_paths_without_starting_a_real_server(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from whoopy.cli import main

    captured: dict[str, Any] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "whoopy.webui.server.serve",
        lambda **kwargs: captured.update(kwargs),
    )

    assert main(["web", "--port", "9001"]) == 0

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9001
    assert captured["project_root"] == tmp_path
    assert captured["runs_directory"] == Path("runs")


def test_audio_endpoint_supports_browser_byte_ranges(tmp_path: Path) -> None:
    application = _application(tmp_path)
    run_id = "23d079b0-5fe0-46d1-ae11-04038ef9d802"
    audio = tmp_path / "runs" / run_id / "narration.wav"
    audio.parent.mkdir(parents=True)
    audio.write_bytes(b"0123456789")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "GET",
            f"/api/runs/{run_id}/audio",
            headers={"Range": "bytes=2-5"},
        )
        response = connection.getresponse()

        assert response.status == 206
        assert response.getheader("Accept-Ranges") == "bytes"
        assert response.getheader("Content-Range") == "bytes 2-5/10"
        assert response.read() == b"2345"
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
