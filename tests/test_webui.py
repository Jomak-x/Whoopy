from __future__ import annotations

import json
import time
from importlib.resources import files
from pathlib import Path
from typing import Any

import pytest
from pytest import MonkeyPatch

from whoopy.pipeline import RunStore
from whoopy.webui.server import LocalWebApplication


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
        ({"mode": "script", "text": "calm", "speed": 3}, "between 0.7 and 1.2"),
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
