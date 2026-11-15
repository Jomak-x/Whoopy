from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from fastapi.testclient import TestClient

import whoopy.evaluation.speech_screening as screening
from whoopy.evaluation.audio_verification import file_sha256
from whoopy.model_packs.operations import _atomic_json
from whoopy.pipeline import RunStore
from whoopy.pipeline.locks import RunLock
from whoopy.pipeline.worker import LocalWorker
from whoopy.webui.api import create_api
from whoopy.webui.server import LocalWebApplication


class StubCoordinator:
    def poll(self) -> None:
        return None


class StubAsrScreening(screening.SpeechScreening):
    observed: str | None = None
    commands: list[list[str]]

    def _run_phase(
        self,
        command: list[str],
        attempt: Path,
        stage: str,
        started: float,
        baseline_swap: int,
        state: dict[str, Any],
    ) -> int:
        self.commands.append(command)
        if stage == "asr":
            assert "--expected-text" not in command and "--expected-file" not in command
            request = self._request(attempt.parent.parent.name, attempt)
            # This stub tests lifecycle/policy, not recognition accuracy.
            text = (
                (attempt / "expected.txt").read_text() if self.observed is None else self.observed
            )
            screening._exclusive_json(
                attempt / "asr.json",
                {
                    "engine": "mlx-whisper",
                    "python_sha256": request.installation.python_sha256,
                    "text": text,
                    "segments": [],
                    "audio_sha256": request.identity.audio_sha256,
                    "model_weights_sha256": request.installation.weights_sha256,
                    "model_config_sha256": request.installation.config_sha256,
                    "worker_source_sha256": request.worker_source_sha256,
                    "model_revision": request.installation.model_directory.name,
                    "decoding": {
                        "language": "en",
                        "temperature": 0.0,
                        "condition_on_previous_text": False,
                        "expected_text_prompted": False,
                    },
                },
            )
            return 0
        return super()._run_phase(command, attempt, stage, started, baseline_swap, state)


@pytest.fixture
def setup_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[StubAsrScreening, str]:
    monkeypatch.setattr(screening, "_supported_host", lambda: True)
    monkeypatch.setattr(
        psutil, "virtual_memory", lambda: SimpleNamespace(available=16 * screening.GIB)
    )
    monkeypatch.setattr(psutil, "swap_memory", lambda: SimpleNamespace(used=0))
    runs, models = tmp_path / "runs", tmp_path / "models"
    store = RunStore(runs)
    record = store.create("A local word-screen test.")
    LocalWorker(store).process(str(record.run_id))
    model = models / "verification/whisper-fixture-revision"
    model.mkdir(parents=True)
    (model / "weights.npz").write_bytes(b"test weights" * 100_000)
    (model / "config.json").write_text('{"fixture":true}')
    screening._exclusive_json(
        models / "verification/speech-screening.json",
        {
            "schema_version": 1,
            "engine": "mlx-whisper",
            "python_executable": sys.executable,
            "python_sha256": file_sha256(Path(sys.executable)),
            "model_directory": str(model),
            "weights_sha256": file_sha256(model / "weights.npz"),
            "config_sha256": file_sha256(model / "config.json"),
        },
    )
    service = StubAsrScreening(runs, models, launcher=lambda *_args, **_kwargs: StubCoordinator())
    service.commands = []
    return service, str(record.run_id)


def finish(service: StubAsrScreening, run_id: str) -> tuple[dict[str, Any], Path]:
    start = service.start(run_id)
    attempt = service._attempt(run_id, start["attempt_id"])
    service.execute(run_id, attempt.name)
    return service.status(run_id), attempt


@pytest.mark.parametrize(
    "observation,expected", [(None, "pass"), ("", "fail"), ("unrelated loud noise", "fail")]
)
def test_screen_preserves_completed_run_and_canonical_audio(
    setup_screen: tuple[StubAsrScreening, str],
    observation: str | None,
    expected: str,
) -> None:
    service, run_id = setup_screen
    originals = {
        name: (service.store.run_directory(run_id) / name).read_bytes()
        for name in ("run.json", "narration.wav", "timeline.json")
    }
    service.observed = observation
    status, attempt = finish(service, run_id)
    assert status["operation_status"] == "completed", status
    assert status["word_status"] == expected, status
    assert status["current"] is True and status["human_review"] == "not_assessed"
    assert status["cancellable"] is False
    assert all(
        (service.store.run_directory(run_id) / name).read_bytes() == data
        for name, data in originals.items()
    )
    assert service.report(run_id, attempt.name)["current"] is True
    assert (attempt / "request.json").stat().st_mode & 0o222 == 0
    assert (attempt / "result.json").stat().st_mode & 0o222 == 0
    assert (attempt / "comparison.stdout.log").is_file()
    assert len(service.commands) == 2


def test_one_word_change_reviews_and_evidence_survives_runtime_removal(
    setup_screen: tuple[StubAsrScreening, str],
) -> None:
    service, run_id = setup_screen
    words = screening._canonical_inputs(service.store, run_id)[1].split()
    words[0] = "changed"
    service.observed = " ".join(words)
    status, _attempt = finish(service, run_id)
    assert status["word_status"] == "review", status
    service.installation_path.unlink()
    restarted = screening.SpeechScreening(service.runs_root, service.models_root)
    status = restarted.status(run_id)
    assert status["available"] is False
    assert status["word_status"] == "review" and status["current"] is True


@pytest.mark.parametrize("filename", ["narration.wav", "timeline.json"])
def test_changed_canonical_inputs_invalidate_word_pass(
    setup_screen: tuple[StubAsrScreening, str],
    filename: str,
) -> None:
    service, run_id = setup_screen
    status, attempt = finish(service, run_id)
    assert status["word_status"] == "pass", status
    source = service.store.run_directory(run_id) / filename
    source.write_bytes(source.read_bytes() + b" ")
    status = service.status(run_id)
    assert status["word_status"] == "stale" and status["current"] is False
    assert service.report(run_id, attempt.name)["current"] is False


@pytest.mark.parametrize(
    "filename",
    [
        "asr.json",
        "report.json",
        "expected.txt",
        "input.wav",
        "timeline.json",
        "whisper-worker.py",
        "request.json",
    ],
)
def test_tampered_attempt_cannot_retain_pass(
    setup_screen: tuple[StubAsrScreening, str],
    filename: str,
) -> None:
    service, run_id = setup_screen
    _status, attempt = finish(service, run_id)
    path = attempt / filename
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b"changed")
    status = service.status(run_id)
    assert status["word_status"] == "unverified" and status["current"] is False
    assert status["operation_status"] == "failed"


def test_missing_or_changed_installation_stays_unverified_without_inference(
    setup_screen: tuple[StubAsrScreening, str],
) -> None:
    service, run_id = setup_screen
    installation = screening.Installation.model_validate_json(service.installation_path.read_text())
    (installation.model_directory / "weights.npz").write_bytes(b"changed")
    status, _attempt = finish(service, run_id)
    assert status["operation_status"] == "failed" and status["word_status"] == "unverified"
    assert service.commands == []
    service.installation_path.unlink()
    with pytest.raises(screening.SpeechScreenError, match="not configured"):
        service.start(run_id)


def test_duplicate_start_cancel_marker_and_fresh_attempt_retention(
    setup_screen: tuple[StubAsrScreening, str],
) -> None:
    service, run_id = setup_screen
    start = service.start(run_id)
    with pytest.raises(screening.SpeechScreenError, match="already active"):
        service.start(run_id)
    attempt = service._attempt(run_id, start["attempt_id"])
    assert service.has_active()
    service.cancel(run_id, attempt.name)
    assert service.execute(run_id, attempt.name) == 1
    status = service.status(run_id)
    assert status["operation_status"] == "cancelled" and status["word_status"] == "unverified"
    assert service.commands == []
    retained = (attempt / "result.json").read_bytes()
    assert service.start(run_id)["attempt_id"] != attempt.name
    assert service.execute(run_id, attempt.name) == 1
    assert (attempt / "result.json").read_bytes() == retained


def test_restart_distinguishes_live_owner_and_abandoned_attempt(
    setup_screen: tuple[StubAsrScreening, str],
) -> None:
    service, run_id = setup_screen
    start = service.start(run_id)
    attempt = service._attempt(run_id, start["attempt_id"])
    restarted = screening.SpeechScreening(service.runs_root, service.models_root)
    assert restarted.status(run_id)["operation_status"] == "queued"
    _atomic_json(
        attempt / "state.json",
        {
            "operation_status": "running",
            "stage": "asr",
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "process_created_at": psutil.Process().create_time(),
        },
    )
    assert restarted.status(run_id)["cancellable"] is True
    _atomic_json(
        attempt / "state.json",
        {"operation_status": "running", "pid": os.getpid(), "host": "another-machine"},
    )
    status = restarted.status(run_id)
    assert status["operation_status"] == "interrupted"
    assert status["word_status"] == "unverified" and status["cancellable"] is False


def test_run_lock_blocks_screen_without_overwriting_active_attempt(
    setup_screen: tuple[StubAsrScreening, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, run_id = setup_screen
    start = service.start(run_id)
    attempt = service._attempt(run_id, start["attempt_id"])
    clock = iter([0.0, 6.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    with RunLock(service.runs_root / run_id):
        assert service.execute(run_id, attempt.name) == 1
    assert not (attempt / "result.json").exists() and service.commands == []


@pytest.mark.parametrize("stop", ["cancel", "timeout", "memory", "swap"])
def test_supervisor_stops_only_owned_stub_process_and_retains_logs(
    setup_screen: tuple[StubAsrScreening, str],
    monkeypatch: pytest.MonkeyPatch,
    stop: str,
) -> None:
    service, run_id = setup_screen
    attempt = service._attempt(run_id, service.start(run_id)["attempt_id"])
    if stop == "cancel":
        service.cancel(run_id, attempt.name)
    elif stop == "timeout":
        monkeypatch.setattr(screening, "TIMEOUT_SECONDS", 0)
    elif stop == "memory":
        values = iter([16 * screening.GIB, screening.GIB, screening.GIB])
        monkeypatch.setattr(
            psutil, "virtual_memory", lambda: SimpleNamespace(available=next(values))
        )
        clock = iter([0.0, 0.0, 0.0, 3.0, 3.0])
        monkeypatch.setattr(time, "monotonic", lambda: next(clock))
    elif stop == "swap":
        monkeypatch.setattr(psutil, "swap_memory", lambda: SimpleNamespace(used=5 * screening.GIB))
    command = [sys.executable, "-c", "import time;time.sleep(30)"]
    with pytest.raises(screening._Stopped):
        screening.SpeechScreening._run_phase(
            service, command, attempt, "stub", 0 if stop == "memory" else time.monotonic(), 0, {}
        )
    receipt = screening._read_json(attempt / "stub.process.json")
    assert receipt["command"] == command and receipt["failure"]
    if stop != "timeout":
        assert (attempt / "stub.stdout.log").is_file() and (attempt / "stub.stderr.log").is_file()
    else:
        assert receipt["returncode"] is None


def test_api_keeps_pcm_and_words_separate_and_forbids_browser_paths(
    setup_screen: tuple[StubAsrScreening, str],
) -> None:
    service, run_id = setup_screen
    application = LocalWebApplication(
        project_root=Path(__file__).resolve().parents[1],
        config_directory=Path("config"),
        models_directory=service.models_root / "managed",
        runs_directory=service.runs_root,
    )
    application.speech_screening = service
    with TestClient(create_api(application), base_url="http://127.0.0.1") as client:
        before = client.get(f"/api/v1/runs/{run_id}").json()
        assert before["status"] == "completed" and before["quality_passed"] is True
        assert before["speech_screening"]["word_status"] == "unverified"
        assert (
            client.post(
                f"/api/v1/runs/{run_id}/speech-screen", json={"model_directory": "/private"}
            ).status_code
            == 422
        )
        started = client.post(f"/api/v1/runs/{run_id}/speech-screen", json={})
        assert started.status_code == 202, started.text
        service.execute(run_id, started.json()["attempt_id"])
        status = client.get(f"/api/v1/runs/{run_id}/speech-screen").json()
        assert status["word_status"] == "pass" and status["current"] is True, status
        report = client.get(status["report_url"])
        assert report.status_code == 200 and report.json()["run_id"] == run_id
        assert report.json()["report"]["asr"]["decoding"]["expected_text_prompted"] is False
        after = client.get(f"/api/v1/runs/{run_id}").json()
        assert after["status"] == "completed" and after["pcm_quality_passed"] is True
        assert after["speech_screening"]["word_status"] == "pass"


def test_queued_screen_blocks_deletion_and_unknown_cancel_is_clean_error(
    setup_screen: tuple[StubAsrScreening, str],
) -> None:
    service, run_id = setup_screen
    app = LocalWebApplication(
        project_root=Path(__file__).resolve().parents[1],
        config_directory=Path("config"),
        models_directory=service.models_root / "managed",
        runs_directory=service.runs_root,
    )
    app.speech_screening = service
    with TestClient(create_api(app), base_url="http://127.0.0.1") as client:
        assert client.post(f"/api/v1/runs/{run_id}/speech-screen", json={}).status_code == 202
        denied = client.delete(f"/api/v1/runs/{run_id}")
        assert denied.status_code == 400 and "word check" in denied.text
        assert service.store.load(run_id).status.value == "completed"
        unknown = client.post(
            f"/api/v1/runs/{run_id}/speech-screen/00000000-0000-0000-0000-000000000001/cancel",
            json={},
        )
        assert unknown.status_code == 400 and "unavailable" in unknown.text


def test_detached_coordinator_finishes_after_manager_replacement_without_models(
    setup_screen: tuple[StubAsrScreening, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The fixture patches host support; the real child uses the actual host.
    import platform

    if sys.platform != "darwin" or platform.machine() != "arm64":
        pytest.skip("The real isolated coordinator supports macOS Apple Silicon only")
    service, run_id = setup_screen
    fixture_leaf = tmp_path / "fake-asr.py"
    fixture_leaf.write_text("""import hashlib,json,pathlib,sys,time
# Lifecycle fixture only: no speech model or optional scientific package is imported.
a=pathlib.Path(sys.argv[sys.argv.index('--audio')+1]); root=a.parent
r=json.loads((root/'request.json').read_text()); i=r['installation']; time.sleep(.2)
s={'engine':'mlx-whisper','text':(root/'expected.txt').read_text(),'segments':[],
'audio_sha256':r['identity']['audio_sha256'],'model_weights_sha256':i['weights_sha256'],
'model_config_sha256':i['config_sha256'],'python_sha256':i['python_sha256'],
'model_revision':pathlib.Path(i['model_directory']).name,
'worker_source_sha256':hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
'decoding':{'language':'en','temperature':0.0,'condition_on_previous_text':False,'expected_text_prompted':False}}
with (root/'asr.json').open('x') as out: json.dump(s,out)
""")
    monkeypatch.setattr(screening, "runtime_worker_script", lambda _name: fixture_leaf)

    def launch_with_stub_system_metrics(command: list[str], **kwargs: Any) -> subprocess.Popen[str]:
        # macOS sysctl can be denied by the test sandbox. No model is run here.
        bootstrap = (
            "import runpy,sys,psutil;from types import SimpleNamespace;"
            "psutil.virtual_memory=lambda:SimpleNamespace(available=16*1024**3);"
            "psutil.swap_memory=lambda:SimpleNamespace(used=0);"
            "sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
        )
        return subprocess.Popen([command[0], "-B", "-c", bootstrap, *command[2:]], **kwargs)

    launcher = screening.SpeechScreening(
        service.runs_root, service.models_root, launcher=launch_with_stub_system_metrics
    )
    started = launcher.start(run_id)
    restarted = screening.SpeechScreening(service.runs_root, service.models_root)
    deadline = time.monotonic() + 20
    status = restarted.status(run_id)
    while status["operation_status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(0.1)
        status = restarted.status(run_id)
    assert status["operation_status"] == "completed" and status["word_status"] == "pass", status
    attempt = restarted._attempt(run_id, started["attempt_id"])
    for phase in ("asr", "comparison"):
        receipt = screening._read_json(attempt / f"{phase}.process.json")
        assert receipt["returncode"] == 0 and receipt["elapsed_seconds"] >= 0
    # Reap only our directly launched test coordinator; lifecycle belongs to the child.
    launcher._children[run_id].wait(timeout=5)


def test_comparison_stops_after_its_owned_coordinator_is_killed(
    setup_screen: tuple[StubAsrScreening, str],
) -> None:
    service, run_id = setup_screen
    attempt = service._attempt(run_id, service.start(run_id)["attempt_id"])
    request = service._request(run_id, attempt)
    (attempt / "input.wav").write_bytes(service.store.audio_path(run_id).read_bytes())
    service._run_phase([], attempt, "asr", time.monotonic(), 0, {})
    verifier = attempt / "verifier-source.py"
    verifier.chmod(0o644)
    verifier.write_text(
        "import time\nfrom pathlib import Path\n"
        "def verify_audio(*a,**k):\n"
        " Path(__file__).with_name('comparison-started').touch()\n"
        " time.sleep(2)\n Path(__file__).with_name('comparison-orphaned').touch()\n"
        " return {'status':'pass'}\n"
    )
    request = request.model_copy(update={"verifier_source_sha256": file_sha256(verifier)})
    request_path = attempt / "request.json"
    request_path.chmod(0o644)
    request_path.write_text(request.model_dump_json())
    source = str(Path(screening.__file__).resolve())
    parent_program = (
        "import os,subprocess,sys,time;"
        "child=subprocess.Popen([sys.executable,'-B',sys.argv[1],'--compare',sys.argv[2],"
        "'--parent-pid',str(os.getpid())]);print(child.pid,flush=True);time.sleep(30)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", parent_program, source, str(attempt)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline().strip())
    try:
        deadline = time.monotonic() + 5
        while not (attempt / "comparison-started").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (attempt / "comparison-started").exists()
        parent.kill()  # Only the test's directly owned coordinator process.
        parent.wait(timeout=5)
        time.sleep(2.2)
        assert not (attempt / "comparison-orphaned").exists()
        assert not (attempt / "report.json").exists()
        assert (
            not psutil.pid_exists(child_pid)
            or psutil.Process(child_pid).status() == psutil.STATUS_ZOMBIE
        )
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=5)
        parent.communicate(timeout=5)


def test_old_policy_receipts_are_not_reinterpreted_as_word_pass(
    setup_screen: tuple[StubAsrScreening, str],
) -> None:
    service, run_id = setup_screen
    _status, attempt = finish(service, run_id)
    result_path = attempt / "result.json"
    result = screening._read_json(result_path)
    result["policy_id"] = "legacy-threshold-policy"
    result_path.chmod(0o644)
    result_path.write_text(json.dumps(result))
    status = service.status(run_id)
    assert status["word_status"] == "unverified" and status["current"] is False
    with pytest.raises(screening.SpeechScreenError, match="required word-screen policy"):
        service.report(run_id, attempt.name)


def test_unavailable_platform_never_starts_asr(
    setup_screen: tuple[StubAsrScreening, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, run_id = setup_screen
    monkeypatch.setattr(screening, "_supported_host", lambda: False)
    status = service.status(run_id)
    assert status["available"] is False and status["word_status"] == "unverified"
    assert "macOS Apple Silicon" in status["availability_reason"]
    with pytest.raises(screening.SpeechScreenError, match="macOS Apple Silicon"):
        service.start(run_id)
    assert service.commands == []
