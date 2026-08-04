from __future__ import annotations

import http.client
import json
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pytest import MonkeyPatch

from whoopy.pipeline import RunExecution, RunStage, RunStatus, RunStore
from whoopy.timeline import build_script_timeline
from whoopy.webui.server import LocalWebApplication, _handler_factory


def _application(tmp_path: Path) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
    )


def _wait_for_capture(captured: dict[str, Any]) -> None:
    deadline = time.monotonic() + 2
    while "command" not in captured:
        if time.monotonic() >= deadline:
            pytest.fail("background command did not start")
        time.sleep(0.01)


def _interrupt_record(store: RunStore, prompt: str = "Recover this run.") -> str:
    record = store.create(prompt)
    assert record.recovery is not None
    interrupted = record.transition(
        RunStatus.INTERRUPTED,
        updated_at=datetime.now(UTC),
        recovery=record.recovery.model_copy(update={"process_attempts": 1}),
        execution=RunExecution(
            stage=RunStage.COMPILING,
            interruption_kind="test_interruption",
            message="Test worker stopped.",
        ),
    )
    store.save(interrupted)
    return str(record.run_id)


def _post_json(
    server: ThreadingHTTPServer,
    path: str,
    payload: object,
    *,
    origin: str | None = None,
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
    headers = {"Content-Type": "application/json"}
    if origin is not None:
        headers["Origin"] = origin
    connection.request("POST", path, body=json.dumps(payload), headers=headers)
    response = connection.getresponse()
    status = response.status
    body = json.loads(response.read())
    connection.close()
    return status, body


def test_static_interface_is_packaged() -> None:
    static = files("whoopy.webui").joinpath("static")

    html = static.joinpath("index.html").read_text(encoding="utf-8")
    assert "Whoopy Local Studio" in html
    assert static.joinpath("styles.css").is_file()
    assert static.joinpath("app.js").is_file()


def test_model_pack_routes_are_allow_listed_and_keep_removal_explicit(tmp_path: Path) -> None:
    calls: list[tuple[str, object]] = []

    class FakePacks:
        def list(self) -> dict[str, object]:
            calls.append(("list", None))
            return {"packs": [{"pack_id": "moss-local-5b", "state": "missing"}]}

        def verify(self, pack_id: str) -> dict[str, object]:
            calls.append(("verify", pack_id))
            return {"pack_id": pack_id, "state": "ready"}

        def remove(self, pack_id: str, *, confirmed: bool) -> dict[str, object]:
            calls.append(("remove", (pack_id, confirmed)))
            return {"pack_id": pack_id, "receipt_id": "trash-1"}

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        model_pack_service_factory=lambda _registry, _models: FakePacks(),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", "/api/model-packs")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read()) == {
            "packs": [{"pack_id": "moss-local-5b", "state": "missing"}]
        }
        connection.close()

        origin = f"http://127.0.0.1:{server.server_port}"
        status, payload = _post_json(
            server,
            "/api/model-packs/moss-local-5b/verify",
            {},
            origin=origin,
        )
        assert status == 200
        assert payload["state"] == "ready"

        status, payload = _post_json(
            server,
            "/api/model-packs/moss-local-5b/remove",
            {},
            origin=origin,
        )
        assert status == 400
        assert "confirm" in payload["error"]

        status, payload = _post_json(
            server,
            "/api/model-packs/moss-local-5b/remove",
            {"confirm": True},
            origin=origin,
        )
        assert status == 200
        assert payload["receipt_id"] == "trash-1"
        assert calls == [
            ("list", None),
            ("verify", "moss-local-5b"),
            ("remove", ("moss-local-5b", True)),
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_model_pack_api_maps_the_baseline_artifact_store_to_models_root(tmp_path: Path) -> None:
    captured: dict[str, Path] = {}

    class FakePacks:
        def list(self) -> dict[str, object]:
            return {"packs": []}

    def factory(registry: Path, models_root: Path) -> FakePacks:
        captured["registry"] = registry
        captured["models_root"] = models_root
        return FakePacks()

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models/managed"),
        runs_directory=Path("runs"),
        model_pack_service_factory=factory,
    )

    assert application.list_model_packs() == {"packs": []}
    assert captured == {
        "registry": tmp_path / "config" / "model_packs.yaml",
        "models_root": tmp_path / "models",
    }


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


def test_prompt_task_preallocates_a_durable_run_and_uses_the_real_cli_contract(
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
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FakeProcess,
    )

    created = application.start_generation(
        {
            "mode": "prompt",
            "text": "A gentle grounding pause.",
            "minutes": 1,
            "voice": "af_heart",
            "speed": 0.9,
        }
    )
    run_id = created["run_id"]
    assert created["task_id"] == run_id
    assert RunStore(tmp_path / "runs").load(run_id).status.value == "queued"
    _wait_for_capture(captured)
    command = captured["command"]
    assert command[:4] == [command[0], "-m", "whoopy", "generate"]
    assert command[command.index("--run-id") + 1] == run_id
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
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

    monkeypatch.setattr(
        "whoopy.webui.server.resolve_tts_model_pack",
        lambda *_args, **_kwargs: None,
    )
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FakeProcess,
    )

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
    assert created["status"] == "queued"
    _wait_for_capture(captured)
    command = captured["command"]
    assert command[command.index("--tts-model") + 1] == "moss-local-v1.5"
    assert command[command.index("--moss-language") + 1] == "German"
    assert command[command.index("--moss-instruction") + 1] == ("Speak softly and thoughtfully.")
    assert "--moss-direct-voice" in command


def test_run_status_survives_a_web_application_restart(tmp_path: Path) -> None:
    record = RunStore(tmp_path / "runs").create("A durable breathing practice.")

    restarted = _application(tmp_path)

    run = restarted.run(str(record.run_id))
    task = restarted.task(str(record.run_id))
    assert run is not None and run["status"] == "queued"
    assert task is not None and task["status"] == "queued"
    assert task["run_id"] == str(record.run_id)


def test_recovery_commands_use_the_cli_and_reject_overlapping_workers(
    tmp_path: Path,
) -> None:
    run_id = _interrupt_record(RunStore(tmp_path / "runs"))
    captured: list[list[str]] = []

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured.append(command)

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FakeProcess,
    )
    result = application.resume_run(run_id)
    assert result is not None
    deadline = time.monotonic() + 2
    while not captured:
        if time.monotonic() >= deadline:
            pytest.fail("recovery worker did not start")
        time.sleep(0.01)
    command = captured[0]
    assert command[:5] == [command[0], "-m", "whoopy", "run", "resume"]
    assert command[5] == run_id


def test_immediate_cancel_is_not_lost_before_process_publication(tmp_path: Path) -> None:
    launcher_entered = threading.Event()
    release_launcher = threading.Event()
    terminated = threading.Event()

    class BlockingProcess:
        returncode: int | None = None

        def __init__(self, _command: list[str], **_kwargs: Any) -> None:
            launcher_entered.set()
            assert release_launcher.wait(timeout=2)

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15
            terminated.set()

        def wait(self, *, timeout: float) -> int:
            assert timeout > 0
            return self.returncode or 0

        def kill(self) -> None:
            self.returncode = -9
            terminated.set()

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=BlockingProcess,
    )
    created = application.start_generation({"mode": "prompt", "text": "Pause.", "minutes": 1})
    assert launcher_entered.wait(timeout=2)

    cancelled = application.cancel_task(created["run_id"])
    assert cancelled is not None
    release_launcher.set()
    assert terminated.wait(timeout=2)
    store = RunStore(tmp_path / "runs")
    deadline = time.monotonic() + 2
    record = store.load(created["run_id"])
    while record.status is RunStatus.QUEUED:
        if time.monotonic() >= deadline:
            pytest.fail("cancel intent was not made durable")
        time.sleep(0.01)
        record = store.load(created["run_id"])
    assert record.status is RunStatus.INTERRUPTED
    assert record.execution is not None
    assert record.execution.interruption_kind == "user_cancelled"


def test_launch_failure_is_persisted_in_the_preallocated_run(tmp_path: Path) -> None:
    def fail_launch(_command: list[str], **_kwargs: Any) -> Any:
        raise OSError("launcher unavailable")

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=fail_launch,
    )
    created = application.start_generation({"mode": "prompt", "text": "Pause.", "minutes": 1})
    store = RunStore(tmp_path / "runs")
    deadline = time.monotonic() + 2
    record = store.load(created["run_id"])
    while record.status is RunStatus.QUEUED:
        if time.monotonic() >= deadline:
            pytest.fail("launcher failure was not persisted")
        time.sleep(0.01)
        record = store.load(created["run_id"])
    assert record.status is RunStatus.FAILED
    assert record.error is not None and "launcher unavailable" in record.error


def test_script_input_write_failure_is_persisted(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    original_write_text = Path.write_text

    def fail_web_input(path: Path, data: str, **kwargs: Any) -> int:
        if path.parent.name == ".web-inputs":
            raise OSError("input disk unavailable")
        return original_write_text(path, data, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_web_input)
    application = _application(tmp_path)
    created = application.start_generation({"mode": "script", "text": "Welcome."})
    store = RunStore(tmp_path / "runs")
    deadline = time.monotonic() + 2
    record = store.load(created["run_id"])
    while record.status is RunStatus.QUEUED:
        if time.monotonic() >= deadline:
            pytest.fail("input-write failure was not persisted")
        time.sleep(0.01)
        record = store.load(created["run_id"])
    assert record.status is RunStatus.FAILED
    assert record.error is not None and "input disk unavailable" in record.error


def test_nonzero_generation_process_cannot_leave_a_queued_run(tmp_path: Path) -> None:
    class FailedProcess:
        returncode = 2

        def __init__(self, _command: list[str], **_kwargs: Any) -> None:
            pass

        def communicate(self) -> tuple[str, str]:
            return "", "whoopy: error: model preflight failed"

        def poll(self) -> int:
            return self.returncode

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FailedProcess,
    )
    created = application.start_generation({"mode": "prompt", "text": "Pause.", "minutes": 1})
    store = RunStore(tmp_path / "runs")
    deadline = time.monotonic() + 2
    record = store.load(created["run_id"])
    while record.status is RunStatus.QUEUED:
        if time.monotonic() >= deadline:
            pytest.fail("nonzero child exit was not persisted")
        time.sleep(0.01)
        record = store.load(created["run_id"])
    assert record.status is RunStatus.FAILED
    assert record.error == "model preflight failed"


def test_duplicate_resume_is_rejected_during_the_launch_window(tmp_path: Path) -> None:
    run_id = _interrupt_record(RunStore(tmp_path / "runs"))
    launcher_entered = threading.Event()
    release_launcher = threading.Event()

    class BlockingProcess:
        returncode = 0

        def __init__(self, _command: list[str], **_kwargs: Any) -> None:
            launcher_entered.set()
            assert release_launcher.wait(timeout=2)

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=BlockingProcess,
    )
    assert application.resume_run(run_id) is not None
    assert launcher_entered.wait(timeout=2)
    with pytest.raises(ValueError, match="active local worker"):
        application.resume_run(run_id)
    release_launcher.set()


def test_restart_durable_cancel_uses_the_cli(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("Cancel after restart.")
    assert record.recovery is not None
    now = datetime.now(UTC)
    running = record.transition(
        RunStatus.RUNNING,
        updated_at=now,
        recovery=record.recovery.model_copy(update={"process_attempts": 1}),
        execution=RunExecution(
            stage=RunStage.PLANNING,
            attempt_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            owner_id="old-web",
            pid=123,
            started_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=15),
        ),
    )
    store.save(running)
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured["command"] = command

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    restarted = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FakeProcess,
    )
    assert restarted.cancel_task(str(record.run_id)) is not None
    _wait_for_capture(captured)
    command = captured["command"]
    assert command[:6] == [command[0], "-m", "whoopy", "run", "cancel", str(record.run_id)]


def test_resume_and_regeneration_prevalidate_durable_state(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    queued = store.create("Not resumable yet.")
    application = _application(tmp_path)
    with pytest.raises(ValueError, match="only failed or interrupted"):
        application.resume_run(str(queued.run_id))

    run_id = _interrupt_record(store, "Has a timeline.")
    timeline = build_script_timeline(
        run_id=UUID(run_id),
        script="Welcome.\n\n[pause: 2s]\n\nBreathe.",
        created_at=datetime.now(UTC),
    )
    store.write_timeline(run_id, timeline)
    with pytest.raises(ValueError, match="not present"):
        application.regenerate_segment(run_id, "speech-9999")


def test_post_routes_validate_body_origin_and_encoded_segment(tmp_path: Path) -> None:
    run_id = _interrupt_record(RunStore(tmp_path / "runs"))
    application = _application(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        valid_origin = f"http://127.0.0.1:{server.server_port}"
        status, _body = _post_json(
            server,
            f"/api/runs/{run_id}/resume",
            {"unexpected": True},
            origin=valid_origin,
        )
        assert status == 400
        status, _body = _post_json(
            server,
            f"/api/runs/{run_id}/segments/%2e%2e/regenerate",
            {},
            origin=valid_origin,
        )
        assert status == 400
        status, _body = _post_json(
            server,
            f"/api/runs/{run_id}/resume",
            {},
            origin="http://localhost:1",
        )
        assert status == 403
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_valid_resume_post_launches_the_durable_cli_command(tmp_path: Path) -> None:
    run_id = _interrupt_record(RunStore(tmp_path / "runs"))
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured["command"] = command

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FakeProcess,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, body = _post_json(
            server,
            f"/api/runs/{run_id}/resume",
            {},
            origin=f"http://127.0.0.1:{server.server_port}",
        )
        assert status == 202
        assert body["run_id"] == run_id
        _wait_for_capture(captured)
        assert captured["command"][:6] == [
            captured["command"][0],
            "-m",
            "whoopy",
            "run",
            "resume",
            run_id,
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_run_endpoint_reads_a_durable_record(tmp_path: Path) -> None:
    record = RunStore(tmp_path / "runs").create("Read me from HTTP.")
    application = _application(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_factory(application))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request("GET", f"/api/runs/{record.run_id}")
        response = connection.getresponse()
        assert response.status == 200
        assert json.loads(response.read())["run_id"] == str(record.run_id)
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


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
