from __future__ import annotations

import http.client
import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from http.server import ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pytest import MonkeyPatch

from whoopy.pipeline import RunExecution, RunStage, RunStatus, RunStore
from whoopy.pipeline.worker import LocalWorker
from whoopy.timeline import build_script_timeline
from whoopy.webui.server import GenerationTask, LocalWebApplication, _handler_factory


class _ReadyKokoroPacks:
    def list(self) -> dict[str, object]:
        return {"packs": [{"pack_id": "kokoro", "state": "ready"}]}


def _application(tmp_path: Path) -> LocalWebApplication:
    return LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        model_pack_service_factory=lambda _registry, _models: _ReadyKokoroPacks(),
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


def test_running_summary_reports_live_elapsed_time_and_process_memory(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("Measure this run.")
    assert record.recovery is not None
    now = datetime.now(UTC)
    store.save(
        record.transition(
            RunStatus.RUNNING,
            updated_at=now,
            recovery=record.recovery.model_copy(update={"process_attempts": 1}),
            execution=RunExecution(
                stage=RunStage.SYNTHESIZING,
                attempt_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
                owner_id="test-web",
                pid=os.getpid(),
                started_at=now - timedelta(seconds=2),
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=15),
            ),
        )
    )

    summary = _application(tmp_path).run(str(record.run_id))

    assert summary is not None
    assert summary["live_metrics"]["elapsed_seconds"] >= 2
    assert summary["live_metrics"]["resident_memory_bytes"] > 0


def test_task_stays_running_until_completed_worker_releases_lock(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    class Process:
        def __init__(self, return_code: int | None) -> None:
            self.return_code = return_code

        def poll(self) -> int | None:
            return self.return_code

    application = _application(tmp_path)
    monkeypatch.setattr(
        application,
        "run",
        lambda _run_id: {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "status": "completed",
            "execution": {"stage": "completed", "message": "Done."},
        },
    )
    task = GenerationTask(
        task_id="11111111-1111-4111-8111-111111111111",
        run_id="11111111-1111-4111-8111-111111111111",
        launching=False,
        process=Process(None),
    )

    active = application._task_public(task)
    task.process = Process(0)
    exited = application._task_public(task)
    task.process = None
    released = application._task_public(task)

    assert active["status"] == "running"
    assert active["execution"]["stage"] == "finalizing"
    assert exited["status"] == "running"
    assert exited["execution"]["stage"] == "finalizing"
    assert released["status"] == "completed"


def test_interrupted_task_stays_running_until_worker_releases_lock(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    class Process:
        def __init__(self, return_code: int | None) -> None:
            self.return_code = return_code

        def poll(self) -> int | None:
            return self.return_code

    application = _application(tmp_path)
    monkeypatch.setattr(
        application,
        "run",
        lambda _run_id: {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "status": "interrupted",
            "execution": {"stage": "synthesizing", "message": "Stopped."},
        },
    )
    task = GenerationTask(
        task_id="11111111-1111-4111-8111-111111111111",
        run_id="11111111-1111-4111-8111-111111111111",
        launching=False,
        process=Process(None),
    )

    active = application._task_public(task)
    task.process = Process(143)
    exited = application._task_public(task)
    task.process = None
    released = application._task_public(task)

    assert active["status"] == "running"
    assert active["execution"]["stage"] == "finalizing"
    assert exited["status"] == "running"
    assert exited["execution"]["stage"] == "finalizing"
    assert released["status"] == "interrupted"


def test_library_projection_does_not_hide_runs_after_the_first_page(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    for index in range(30):
        store.create(f"Meditation {index + 1}.")

    runs = _application(tmp_path).list_runs()

    assert len(runs) == 30
    assert {run["title"] for run in runs} == {f"Meditation {index + 1}." for index in range(30)}


def test_legacy_lab_title_is_projected_as_lab_audience(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("Lab legacy-comparison / case: controlled prompt")
    path = store.run_directory(record.run_id) / "run.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("audience")
    path.write_text(json.dumps(raw), encoding="utf-8")

    summary = next(
        run for run in _application(tmp_path).list_runs() if run["run_id"] == str(record.run_id)
    )

    assert summary["audience"] == "lab"


def test_current_studio_title_beginning_with_lab_remains_in_studio(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("Lab notes for my personal meditation", audience="studio")

    summary = next(
        run for run in _application(tmp_path).list_runs() if run["run_id"] == str(record.run_id)
    )

    assert summary["audience"] == "studio"


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

    class Assets(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.paths: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            values = dict(attrs)
            path = values.get("src" if tag == "script" else "href")
            if tag in {"script", "link"} and path and path.startswith("/"):
                self.paths.append(path.lstrip("/"))

    document = Assets()
    document.feed(static.joinpath("index.html").read_text(encoding="utf-8"))
    assert document.paths
    for path in document.paths:
        assert static.joinpath(path).is_file(), f"Missing packaged document asset: {path}"


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
            {"mode": "prompt", "text": "calm", "pacing_profile_id": "invented"},
            "not in reviewed registry",
        ),
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


def test_kokoro_generation_requires_current_ready_smoke_evidence(tmp_path: Path) -> None:
    class MissingKokoroPacks:
        def list(self) -> dict[str, object]:
            return {
                "packs": [
                    {
                        "pack_id": "kokoro",
                        "state": "missing",
                        "checks": [
                            {
                                "passed": False,
                                "message": "Playable synthesis has not passed for this revision",
                            }
                        ],
                    }
                ]
            }

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        model_pack_service_factory=lambda _registry, _models: MissingKokoroPacks(),
    )

    with pytest.raises(ValueError, match="kokoro is not ready"):
        application.start_generation({"mode": "script", "text": "Welcome.", "tts_model": "kokoro"})
    assert application.list_runs() == []


@pytest.mark.parametrize(
    ("timing", "expected_speed", "expected_pacing"),
    [
        ({}, "0.6", "meditative-v2"),
        ({"speed": 0.9}, "0.9", None),
        ({"speed": 0.9, "pacing_profile_id": "meditative-v2"}, "0.6", "meditative-v2"),
    ],
)
def test_prompt_task_preallocates_a_durable_run_and_uses_the_real_cli_contract(
    timing: dict[str, Any],
    expected_speed: str,
    expected_pacing: str | None,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    from fastapi.testclient import TestClient

    from whoopy.webui.api import create_api

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
        model_pack_service_factory=lambda _registry, _models: _ReadyKokoroPacks(),
    )

    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/generate",
            json={
                "mode": "prompt",
                "text": "A gentle grounding pause.",
                "minutes": 1,
                "voice": "af_heart",
                **timing,
            },
        )
    assert response.status_code == 202
    created = response.json()
    run_id = created["run_id"]
    assert created["task_id"] == run_id
    assert RunStore(tmp_path / "runs").load(run_id).audience == "studio"
    _wait_for_capture(captured)
    command = captured["command"]
    assert command[:4] == [command[0], "-m", "whoopy", "generate"]
    assert command[command.index("--run-id") + 1] == run_id
    assert "--profile" in command
    assert command[command.index("--profile") + 1] == "standard"
    assert command[command.index("--delivery") + 1] == "calm"
    assert command[command.index("--pace") + 1] == "slow"
    if expected_pacing is None:
        assert "--pacing-profile" not in command
    else:
        assert command[command.index("--pacing-profile") + 1] == expected_pacing
    assert command[command.index("--speed") + 1] == expected_speed
    assert command[command.index("--audience") + 1] == "studio"


@pytest.mark.parametrize("managed_reference", [True, False])
def test_moss_controls_reach_the_real_cli_contract(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
    managed_reference: bool,
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

    def resolve_pack(*_args: object, **kwargs: object) -> None:
        assert kwargs["resolve_reference"] is False
        assert not (tmp_path / "config/voice_references.yaml").exists()

    monkeypatch.setattr("whoopy.webui.server.resolve_tts_model_pack", resolve_pack)

    class ReadyMossPacks:
        def list(self) -> dict[str, object]:
            return {"packs": [{"pack_id": "moss-local-5b", "state": "ready"}]}

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FakeProcess,
        model_pack_service_factory=lambda _registry, _models: ReadyMossPacks(),
    )
    monkeypatch.setattr(
        application.voice_reference_manager,
        "load",
        lambda reference_id: object() if reference_id == "my-narrator" else None,
    )

    created = application.start_generation(
        {
            "mode": "script",
            "text": "Notice the quiet around you.",
            "surface": "lab",
            "requested_duration_seconds": 180,
            "tts_model": "moss-local-v1.5",
            "moss_language": "German",
            "moss_instruction": "Speak softly and thoughtfully.",
            "moss_use_reference": managed_reference,
            "voice_reference": "my-narrator" if managed_reference else None,
            "intensity": "moderate",
            "pace": "very_slow",
        }
    )
    assert created["status"] == "queued"
    _wait_for_capture(captured)
    command = captured["command"]
    assert command[command.index("--tts-model") + 1] == "moss-local-v1.5"
    assert "--speed" not in command
    assert command[command.index("--moss-language") + 1] == "German"
    assert command[command.index("--moss-instruction") + 1] == ("Speak softly and thoughtfully.")
    assert command[command.index("--delivery") + 1] == "calm"
    assert command[command.index("--intensity") + 1] == "moderate"
    assert command[command.index("--pace") + 1] == "very_slow"
    if managed_reference:
        assert "--moss-direct-voice" not in command
        assert command[command.index("--voice-reference") + 1] == "my-narrator"
    else:
        assert "--moss-direct-voice" in command
        assert "--voice-reference" not in command
    assert command[command.index("--requested-duration-seconds") + 1] == "180"


def test_qwen_speaker_does_not_enter_kokoro_voice_option(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        returncode = 0

        def __init__(self, command: list[str], **_kwargs: Any) -> None:
            captured["command"] = command

        def communicate(self) -> tuple[str, str]:
            return "{}", ""

        def poll(self) -> int:
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

    class ReadyQwenPacks:
        def list(self) -> dict[str, object]:
            return {
                "packs": [
                    {
                        "pack_id": "qwen3-tts-custom-voice-0.6b",
                        "state": "ready",
                    }
                ]
            }

    monkeypatch.setattr(
        "whoopy.webui.server.resolve_qwen3_tts_installation",
        lambda *_args, **_kwargs: None,
    )
    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=FakeProcess,
        model_pack_service_factory=lambda _registry, _models: ReadyQwenPacks(),
    )
    # This unit test exercises CLI argument separation below the product
    # eligibility gate. Qwen 0.6B remains blocked from real Lab selection until
    # its natural-EOS quality review passes (covered by Qwen web integration
    # tests), while the dormant launch path must still keep speaker and Kokoro
    # voice arguments separate.
    monkeypatch.setattr(application, "_require_speech_model_ready", lambda *_args, **_kwargs: None)

    created = application.start_generation(
        {
            "surface": "lab",
            "mode": "script",
            "text": "Notice the quiet around you.",
            "tts_model": "qwen3-custom-voice-0.6b",
            "voice": "Serena",
            "qwen_speaker": "Serena",
            "qwen_language": "English",
            "delivery": "neutral",
            "intensity": "subtle",
            "pace": "natural",
        }
    )

    assert created["status"] == "queued"
    _wait_for_capture(captured)
    command = captured["command"]
    assert "--voice" not in command
    assert "--speed" not in command
    assert command[command.index("--qwen-speaker") + 1] == "Serena"
    assert command[command.index("--pace") + 1] == "natural"


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
        model_pack_service_factory=lambda _registry, _models: _ReadyKokoroPacks(),
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
    while application._tasks[created["run_id"]].active:
        if time.monotonic() >= deadline:
            pytest.fail("cancel supervision did not finish")
        time.sleep(0.01)
    summary = application.run(created["run_id"])
    assert summary is not None
    assert summary["resumable"] is True
    assert (store.run_directory(created["run_id"]) / "web-launch-request.json").is_file()


def test_cancel_intent_wins_a_racing_adapter_failure_and_remains_resumable(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("A script whose adapter is still loading.")
    now = datetime.now(UTC)
    assert record.recovery is not None
    running = record.transition(
        RunStatus.RUNNING,
        updated_at=now,
        recovery=record.recovery.model_copy(update={"process_attempts": 1}),
        execution=RunExecution(
            stage=RunStage.SYNTHESIZING,
            attempt_id=UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
            owner_id=f"test:{os.getpid()}",
            pid=os.getpid(),
            started_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=15),
        ),
    )
    store.save(running)
    assert running.execution is not None and running.execution.attempt_id is not None
    store.fail_active_run(
        record.run_id,
        attempt_id=running.execution.attempt_id,
        error="FatalSynthesisError: runtime import raced cancellation",
    )

    application = _application(tmp_path)
    application._write_web_launch_request(str(record.run_id), {"mode": "script"})
    application._persist_cancel_intent(str(record.run_id))

    interrupted = store.load(record.run_id)
    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.error is None
    assert interrupted.execution is not None
    assert interrupted.execution.interruption_kind == "user_cancelled"
    assert "runtime import raced cancellation" in (interrupted.execution.message or "")
    summary = application.run(str(record.run_id))
    assert summary is not None and summary["resumable"] is True


def test_cancel_intent_finishes_a_child_that_exits_without_saving_signal_state(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("A script stopped during native startup.")
    now = datetime.now(UTC)
    assert record.recovery is not None
    running = record.transition(
        RunStatus.RUNNING,
        updated_at=now,
        recovery=record.recovery.model_copy(update={"process_attempts": 1}),
        execution=RunExecution(
            stage=RunStage.SYNTHESIZING,
            attempt_id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"),
            owner_id=f"test:{os.getpid()}",
            pid=os.getpid(),
            started_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=15),
        ),
    )
    store.save(running)

    _application(tmp_path)._persist_cancel_intent(str(record.run_id))

    interrupted = store.load(record.run_id)
    assert running.execution is not None
    assert interrupted.status is RunStatus.INTERRUPTED
    assert interrupted.execution is not None
    assert interrupted.execution.attempt_id == running.execution.attempt_id
    assert interrupted.execution.interruption_kind == "user_cancelled"
    assert interrupted.execution.is_active is False


def test_restart_reconciles_legacy_queued_placeholder_as_deletable_failure(
    tmp_path: Path,
) -> None:
    store = RunStore(tmp_path / "runs")
    record = store.create("An orphaned request from an older app revision.")
    application = _application(tmp_path)

    application.reconcile_runs()

    reconciled = store.load(record.run_id)
    assert reconciled.status is RunStatus.FAILED
    assert reconciled.error is not None
    assert "No durable launch settings exist" in reconciled.error
    summary = application.run(str(record.run_id))
    assert summary is not None
    assert summary["resumable"] is False


def test_launch_failure_is_persisted_in_the_preallocated_run(tmp_path: Path) -> None:
    def fail_launch(_command: list[str], **_kwargs: Any) -> Any:
        raise OSError("launcher unavailable")

    application = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path("config"),
        models_directory=Path("models"),
        runs_directory=Path("runs"),
        command_launcher=fail_launch,
        model_pack_service_factory=lambda _registry, _models: _ReadyKokoroPacks(),
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
        model_pack_service_factory=lambda _registry, _models: _ReadyKokoroPacks(),
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
    try:
        assert launcher_entered.wait(timeout=2)
        with pytest.raises(ValueError, match="already active"):
            application.resume_run(run_id)
    finally:
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


def test_app_cli_is_the_browser_opening_permanent_entrypoint(
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

    assert main(["app", "--port", "9002"]) == 0

    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9002
    assert captured["open_browser"] is True


def test_audio_endpoint_supports_browser_byte_ranges(tmp_path: Path) -> None:
    application = _application(tmp_path)
    run_id = "23d079b0-5fe0-46d1-ae11-04038ef9d802"
    store = RunStore(application.runs_directory)
    store.create("Notice the room around you.", run_id=UUID(run_id))
    LocalWorker(store).process(run_id)
    payload = store.audio_path(run_id).read_bytes()
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
        assert response.getheader("Content-Range") == f"bytes 2-5/{len(payload)}"
        assert response.read() == payload[2:6]
        connection.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
