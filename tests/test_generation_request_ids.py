from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from whoopy.pipeline import RunStatus, RunStore
from whoopy.webui.api import GenerationRequest, create_api
from whoopy.webui.generation_requests import GenerationRequestError
from whoopy.webui.server import LocalWebApplication


def app_at(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, launcher: Any = None
) -> LocalWebApplication:
    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("test worker unavailable; no inference")

    app = LocalWebApplication(
        project_root=tmp_path,
        config_directory=Path(__file__).resolve().parents[1] / "config",
        models_directory=tmp_path / "models/managed",
        runs_directory=tmp_path / "runs",
        command_launcher=launcher or unavailable,
    )
    monkeypatch.setattr(app, "_require_speech_model_ready", lambda *_a, **_k: None)
    return app


def body(**changes: Any) -> dict[str, Any]:
    return {"request_id": str(uuid4()), "mode": "prompt", "text": "Notice the room.", **changes}


def wait_stopped(app: LocalWebApplication, run_id: str) -> None:
    deadline = time.monotonic() + 5
    while app._tasks[run_id].active and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not app._tasks[run_id].active


def test_identical_retry_replays_without_readiness_or_implicit_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def refuse(*a: Any, **k: Any) -> Any:
        calls.append(a)
        raise OSError("no inference")

    app = app_at(tmp_path, monkeypatch, launcher=refuse)
    request = body()
    first = app.start_generation(request)
    wait_stopped(app, first["run_id"])
    monkeypatch.setattr(
        app,
        "_require_speech_model_ready",
        lambda *_a, **_k: pytest.fail("Replay must skip readiness"),
    )
    replay = app.start_generation(request)
    assert replay["idempotent_replay"] is True and replay["run_id"] == request["request_id"]
    assert replay["task_id"] == request["request_id"] and replay["status"] == "failed"
    assert replay["request_hash_version"] == 1 and len(replay["request_payload_sha256"]) == 64
    assert len(calls) == 1
    recovery = RunStore(app.runs_directory).load(first["run_id"]).recovery
    assert recovery is not None and recovery.process_attempts == 1


def test_canonical_defaults_whitespace_and_conflict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body(text="  Notice the room.  ")
    first = app.start_generation(request)
    wait_stopped(app, first["run_id"])
    explicit = GenerationRequest.model_validate(request).model_dump(mode="json", exclude_none=True)
    explicit["text"] = "Notice the room."
    assert app.start_generation(explicit)["idempotent_replay"] is True
    with pytest.raises(GenerationRequestError) as error:
        app.start_generation({**request, "voice": "am_michael"})
    assert error.value.payload["code"] == "request_id_conflict"
    assert error.value.status == 409


def test_failed_readiness_can_retry_same_unadmitted_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()

    def unavailable(*_a: Any, **_k: Any) -> None:
        raise ValueError("model unavailable")

    monkeypatch.setattr(app, "_require_speech_model_ready", unavailable)
    with pytest.raises(ValueError, match="model unavailable"):
        app.start_generation(request)
    assert not (app.runs_directory / request["request_id"]).exists()
    assert (app.generation_requests.directory(request["request_id"]) / "request.json").is_file()
    monkeypatch.setattr(app, "_require_speech_model_ready", lambda *_a, **_k: None)
    assert app.start_generation(request)["idempotent_replay"] is False
    wait_stopped(app, request["request_id"])


def test_cancel_before_post_prevents_late_admission_and_ack_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()
    with TestClient(create_api(app), base_url="http://127.0.0.1") as client:
        path = f"/api/v1/generation-requests/{request['request_id']}/cancel"
        ack = client.post(path, json={})
        assert ack.status_code == 200 and ack.json() == {
            "request_id": request["request_id"],
            "cancelled": True,
            "run_id": None,
        }
        assert client.post(path, json={}).json() == ack.json()
        denied = client.post("/api/v1/generate", json=request)
        assert denied.status_code == 409 and denied.json()["code"] == "request_cancelled"
        assert client.get(f"/api/v1/runs/{request['request_id']}").status_code == 404
    assert not app._tasks


def test_cancel_during_readiness_is_rechecked_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    other = app_at(tmp_path, monkeypatch)
    entered, release = threading.Event(), threading.Event()

    def ready(*_a: Any, **_k: Any) -> None:
        entered.set()
        assert release.wait(5)

    monkeypatch.setattr(app, "_require_speech_model_ready", ready)
    request = body()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(app.start_generation, request)
        assert entered.wait(5)
        assert other.cancel_generation_request(request["request_id"])["cancelled"] is True
        release.set()
        with pytest.raises(GenerationRequestError) as denied:
            future.result(timeout=5)
    assert denied.value.payload["code"] == "request_cancelled"
    assert not (app.runs_directory / request["request_id"]).exists()


def test_deleted_admitted_run_is_not_resurrected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()
    result = app.start_generation(request)
    wait_stopped(app, result["run_id"])
    assert app.delete_run(result["run_id"]) is not None
    with TestClient(create_api(app), base_url="http://127.0.0.1") as client:
        replay = client.post("/api/v1/generate", json=request)
        assert replay.status_code == 410 and replay.json()["code"] == "request_run_deleted"
    assert not (app.runs_directory / result["run_id"]).exists()


def test_restart_repairs_mapping_after_atomic_publish_without_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()

    def crash(_receipt: dict[str, Any]) -> None:
        raise OSError("crash after run publication")

    monkeypatch.setattr(app.generation_requests, "admit", crash)
    with pytest.raises(OSError, match="crash after"):
        app.start_generation(request)
    assert RunStore(app.runs_directory).load(request["request_id"]).status is RunStatus.QUEUED
    restarted = app_at(tmp_path, monkeypatch)
    restarted.reconcile_runs()
    replay = restarted.start_generation(request)
    assert replay["idempotent_replay"] is True and replay["resumable"] is True
    assert replay["status"] == "interrupted" and not restarted._tasks
    assert (
        restarted.generation_requests.directory(request["request_id"]) / "admitted.json"
    ).is_file()


def test_crash_before_publish_leaves_no_run_and_same_id_can_admit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()
    original = Path.rename

    def crash(path: Path, target: Path) -> Path:
        if target == app.runs_directory / request["request_id"]:
            raise OSError("crash before publish")
        return original(path, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", crash)
        with pytest.raises(OSError, match="before publish"):
            app.start_generation(request)
    assert not (app.runs_directory / request["request_id"]).exists()
    assert not list(app.generation_requests.directory(request["request_id"]).glob(".staging-*"))
    app.start_generation(request)
    wait_stopped(app, request["request_id"])


def test_two_applications_share_one_owned_worker_and_cancel_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    processes: list[subprocess.Popen[str]] = []

    def launch(_command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    first = app_at(tmp_path, monkeypatch, launcher=launch)
    second = app_at(tmp_path, monkeypatch)
    request = body()
    created = first.start_generation(request)
    assert second.start_generation(request)["idempotent_replay"] is True
    deadline = time.monotonic() + 5
    while not processes and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(processes) == 1
    try:
        ack = second.cancel_generation_request(created["run_id"])
        assert ack["run_id"] == created["run_id"] and ack["cancelled"] is True
        wait_stopped(first, created["run_id"])
        assert processes[0].poll() is not None
        assert (
            RunStore(first.runs_directory).load(created["run_id"]).status is RunStatus.INTERRUPTED
        )
        # Explicit Resume is a separate user action, exempt from original Create tombstone.
        first.resume_run(created["run_id"])
        deadline = time.monotonic() + 5
        while len(processes) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert len(processes) == 2 and processes[1].poll() is None
        second.cancel_generation_request(
            created["run_id"]
        )  # Old repeated ack cannot cancel Resume.
        time.sleep(0.4)
        assert processes[1].poll() is None
        first.cancel_task(created["run_id"])
        wait_stopped(first, created["run_id"])
    finally:
        for process in processes:
            if process.poll() is None:
                first._stop_process(process)
            process.communicate(timeout=5)


def test_concurrent_posts_in_two_processes_launch_once(tmp_path: Path) -> None:
    request = body()
    runner = tmp_path / "independent-server.py"
    runner.write_text("""import json,os,pathlib,subprocess,sys,time
from whoopy.webui.server import LocalWebApplication
root=pathlib.Path(sys.argv[1]); request=json.loads(sys.argv[2]); number=sys.argv[3]
def launch(command,**kwargs):
    program=("import os,time;f=os.open("+repr(str(root/'launches'))+
        ",os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600);"+
        "os.write(f,b'worker');os.close(f);time.sleep(.3)")
    return subprocess.Popen([sys.executable,'-c',program],**kwargs)
app=LocalWebApplication(project_root=root,config_directory=pathlib.Path(sys.argv[4]),models_directory=root/'models/managed',runs_directory=root/'runs',command_launcher=launch)
app._require_speech_model_ready=lambda *a,**k:None
(root/('ready'+number)).touch()
while not (root/'go').exists():time.sleep(.01)
result=app.start_generation(request)
(root/('result'+number+'.json')).write_text(json.dumps(result))
deadline=time.monotonic()+10
while any(t.active for t in app._tasks.values()) and time.monotonic()<deadline:time.sleep(.02)
assert not any(t.active for t in app._tasks.values())
""")
    children = [
        subprocess.Popen(
            [
                sys.executable,
                str(runner),
                str(tmp_path),
                json.dumps(request),
                str(index),
                str(Path(__file__).resolve().parents[1] / "config"),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(2)
    ]
    try:
        deadline = time.monotonic() + 5
        while (
            not all((tmp_path / f"ready{i}").exists() for i in range(2))
            and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert all((tmp_path / f"ready{i}").exists() for i in range(2))
        (tmp_path / "go").touch()
        for child in children:
            stdout, stderr = child.communicate(timeout=15)
            assert child.returncode == 0, stdout + stderr
        results = [
            json.loads((tmp_path / f"result{i}.json").read_text(encoding="utf-8")) for i in range(2)
        ]
        assert {r["run_id"] for r in results} == {request["request_id"]}
        assert sorted(r["idempotent_replay"] for r in results) == [False, True]
        assert (tmp_path / "launches").read_text(encoding="utf-8").count("worker") == 1
        assert (
            len([p for p in (tmp_path / "runs").iterdir() if p.name == request["request_id"]]) == 1
        )
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=5)


def test_cancellation_racing_popen_stops_the_owned_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, release = threading.Event(), threading.Event()
    processes: list[subprocess.Popen[str]] = []

    def launch(_command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        entered.set()
        assert release.wait(5)
        process = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"], **kwargs)
        processes.append(process)
        return process

    app = app_at(tmp_path, monkeypatch, launcher=launch)
    other = app_at(tmp_path, monkeypatch)
    request = body()
    app.start_generation(request)
    assert entered.wait(5)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            cancellation = executor.submit(other.cancel_generation_request, request["request_id"])
            time.sleep(0.05)
            assert not cancellation.done()  # Marker commit serializes with Popen registration.
            release.set()
            assert cancellation.result(timeout=5)["cancelled"] is True
        wait_stopped(app, request["request_id"])
        assert len(processes) == 1 and processes[0].poll() is not None
        assert (
            RunStore(app.runs_directory).load(request["request_id"]).status is RunStatus.INTERRUPTED
        )
    finally:
        release.set()
        for process in processes:
            if process.poll() is None:
                app._stop_process(process)
            process.communicate(timeout=5)


def test_uuid_collision_does_not_attach_to_legacy_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()
    store = RunStore(app.runs_directory)
    from uuid import UUID

    original = store.create("Existing legacy run", run_id=UUID(request["request_id"]))
    with pytest.raises(GenerationRequestError) as error:
        app.start_generation(request)
    assert error.value.payload["code"] == "request_id_conflict"
    assert store.load(original.run_id) == original and not app._tasks


def test_api_conflict_dto_and_legacy_without_id_remain_distinct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()
    with TestClient(create_api(app), base_url="http://127.0.0.1") as client:
        first = client.post("/api/v1/generate", json=request)
        assert first.status_code == 202 and first.json()["request_id"] == request["request_id"]
        wait_stopped(app, request["request_id"])
        conflict = client.post("/api/v1/generate", json={**request, "text": "Different words."})
        assert conflict.status_code == 409
        assert conflict.json() == {
            "code": "request_id_conflict",
            "request_id": request["request_id"],
            "error": "This request ID already belongs to different generation settings.",
        }
        legacy = client.post(
            "/api/v1/generate", json={"mode": "prompt", "text": "A legacy request."}
        )
        assert legacy.status_code == 202
        assert "request_id" not in legacy.json() and "idempotent_replay" not in legacy.json()
        assert legacy.json()["run_id"] != request["request_id"]
        wait_stopped(app, legacy.json()["run_id"])


@pytest.mark.parametrize("phase", ["before_publish", "after_publish"])
def test_real_crash_window_retains_staged_evidence_and_never_duplicates_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    request = body()
    program = """import json,os,pathlib,sys
from whoopy.webui.server import LocalWebApplication
from whoopy.webui.generation_requests import GenerationRequests
root=pathlib.Path(sys.argv[1]); payload=json.loads(sys.argv[2]); phase=sys.argv[3]
app=LocalWebApplication(project_root=root,config_directory=pathlib.Path(sys.argv[4]),models_directory=root/'models/managed',runs_directory=root/'runs')
app._require_speech_model_ready=lambda *a,**k:None
if phase=='before_publish':
    rename=pathlib.Path.rename
    def crash(path,target):
        if target==root/'runs'/payload['request_id']:os._exit(78)
        return rename(path,target)
    pathlib.Path.rename=crash
else:
    GenerationRequests.admit=lambda *a,**k:os._exit(79)
app.start_generation(payload)
"""
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            program,
            str(tmp_path),
            json.dumps(request),
            phase,
            str(Path(__file__).resolve().parents[1] / "config"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == (78 if phase == "before_publish" else 79), child.stderr
    app = app_at(tmp_path, monkeypatch)
    staged = list(app.generation_requests.directory(request["request_id"]).glob(".staging-*"))
    assert staged
    frozen = {
        str(p): p.read_bytes() for directory in staged for p in directory.rglob("*") if p.is_file()
    }
    if phase == "after_publish":
        app.reconcile_runs()
    result = app.start_generation(request)
    assert result["idempotent_replay"] is (phase == "after_publish")
    if phase == "before_publish":
        wait_stopped(app, request["request_id"])
        assert RunStore(app.runs_directory).load(request["request_id"]).recovery is not None
    else:
        assert result["status"] == "interrupted" and not app._tasks
    assert frozen == {path: Path(path).read_bytes() for path in frozen}


def test_multibyte_input_keeps_readable_receipts_and_replays(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body(text="静" * 20_000)
    app.start_generation(request)
    wait_stopped(app, request["request_id"])
    assert app.start_generation(request)["idempotent_replay"] is True
    summary = app.generation_request_summary(request["request_id"])
    assert summary is not None and summary["text"] == request["text"]


def test_failed_supervisor_start_is_recoverable_and_replay_never_launches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()

    def refuse(_thread: threading.Thread) -> None:
        raise RuntimeError("test thread resource refusal")

    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", refuse)
        with pytest.raises(ValueError, match="request was saved"):
            app.start_generation(request)
    result = app.start_generation(request)
    assert result["idempotent_replay"] is True
    assert result["status"] == "failed" and result["resumable"] is True
    assert not app._tasks[request["request_id"]].active


def test_busy_request_lock_is_not_a_cancellation_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    other = app_at(tmp_path, monkeypatch)
    request_id = str(uuid4())
    with app.generation_requests.lock(request_id), pytest.raises(GenerationRequestError) as denied:
        other.cancel_generation_request(request_id)
    assert denied.value.payload["code"] == "request_in_progress" and denied.value.status == 409
    assert not app.generation_requests.cancelled(request_id)
    assert other.cancel_generation_request(request_id)["cancelled"] is True


def test_mismatched_client_receipt_cannot_expose_identity_or_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = app_at(tmp_path, monkeypatch)
    request = body()
    app.start_generation(request)
    wait_stopped(app, request["request_id"])
    path = app.runs_directory / request["request_id"] / "web-client-request.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    value["payload"]["text"] = "Changed receipt"
    path.chmod(0o644)
    path.write_text(json.dumps(value))
    summary = app.run(request["request_id"])
    assert summary is not None and "request_id" not in summary
    with pytest.raises(GenerationRequestError) as error:
        app.start_generation(request)
    assert error.value.payload["code"] == "request_id_conflict"
    assert not app._tasks[request["request_id"]].active
