"""Parent-crash fixtures are ordinary Python processes, never model runtimes."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from whoopy.adapters.llm.llama_cpp import CapturingProcessRunner, LlamaProcessResult, _run_process
from whoopy.adapters.tts._json_process import BoundedDiagnostics, JsonLineProcessController

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/worker_parent_guard.py"
WORKERS = (
    "fish_speech_1_4_worker.py",
    "qwen3_tts_worker.py",
    "moss_tts_worker.py",
    "mlx_qwen3_tts_worker.py",
)


def namespace() -> dict[str, Any]:
    return runpy.run_path(str(HELPER))


def wait_file(path: Path, parent: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 10
    while not path.exists() and parent.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert path.exists(), "Owned stub did not finish its launch handshake"


def live(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


def wait_exited(pids: list[int]) -> None:
    deadline = time.monotonic() + 4
    while any(live(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not [pid for pid in pids if live(pid)]


def stop_owned(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def test_launch_identity_and_helper_hash_are_current() -> None:
    updates = namespace()["launch_environment"]()
    assert updates["WHOOPY_PARENT_PID"] == str(os.getpid())
    assert updates["WHOOPY_PARENT_GUARD_SHA256"] == hashlib.sha256(HELPER.read_bytes()).hexdigest()
    assert bool(updates["WHOOPY_PARENT_CREATION_TOKEN"]) == (os.name == "nt")


def test_windows_creation_identity_mismatch_closes_the_owned_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = namespace()
    closed: list[int] = []
    kernel = SimpleNamespace(CloseHandle=closed.append)
    function = values["_validated_windows_parent"]
    monkeypatch.setitem(function.__globals__, "_windows_parent", lambda pid: (kernel, 41, "actual"))
    with pytest.raises(RuntimeError, match="creation identity"):
        function(999, "different")
    assert closed == [41]
    assert function(999, "actual") == (kernel, 41)


def test_windows_watch_uses_handle_state_instead_of_relooking_up_pid() -> None:
    values = namespace()
    watch = values["ParentWatch"].__new__(values["ParentWatch"])
    states = iter([258, 0, 0xFFFFFFFF])
    watch._handle = 41
    watch._kernel = SimpleNamespace(WaitForSingleObject=lambda handle, delay: next(states))
    assert watch.alive() is True
    assert watch.alive() is False
    with pytest.raises(OSError, match="observation failed"):
        watch.alive()


@pytest.mark.parametrize("stage", ["loading", "synthesizing"])
def test_worker_self_exits_when_its_actual_parent_is_killed(
    tmp_path: Path,
    stage: str,
) -> None:
    marker = tmp_path / "ready.json"
    output = tmp_path / "stdout.txt"
    errors = tmp_path / "stderr.txt"
    exit_code = tmp_path / "exit-code.txt"
    child = tmp_path / "stub.py"
    child.write_text(
        "import json, os, pathlib, runpy, time\n"
        "original_exit = os._exit\n"
        "def record_exit(code):\n"
        f"    pathlib.Path({str(exit_code)!r}).write_text(str(code))\n"
        "    original_exit(code)\n"
        "os._exit = record_exit\n"
        f"guard = runpy.run_path({str(HELPER)!r})['start_parent_watch']()\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps(os.getpid()))\n"
        + (
            "print(json.dumps({'status': 'ready'}), flush=True)\n"
            if stage == "synthesizing"
            else ""
        )
        + "time.sleep(8)\n"
    )
    launcher = (
        "import os, pathlib, runpy, subprocess, sys, time\n"
        f"updates = runpy.run_path({str(HELPER)!r})['launch_environment']()\n"
        f"with pathlib.Path({str(output)!r}).open('w') as out, "
        f"pathlib.Path({str(errors)!r}).open('w') as err:\n"
        f"    child = subprocess.Popen([sys.executable, '-I', {str(child)!r}], "
        "env={**os.environ, **updates}, stdout=out, stderr=err)\n"
        "    time.sleep(8)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", launcher], text=True)
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"], text=True)
    try:
        wait_file(marker, parent)
        pid = json.loads(marker.read_text())
        parent.kill()  # Only the live parent Popen owned by this fixture is killed.
        parent.wait(timeout=3)
        wait_exited([pid])
        assert sentinel.poll() is None
        assert "launch parent exited" in errors.read_text()
        assert exit_code.read_text() == "75"
        expected = '{"status": "ready"}\n' if stage == "synthesizing" else ""
        assert output.read_text() == expected
    finally:
        stop_owned(parent)
        stop_owned(sentinel)


@pytest.mark.parametrize("worker", WORKERS)
@pytest.mark.parametrize("failure", ["missing", "tampered"])
def test_worker_bootstrap_rejects_missing_or_tampered_helper_before_runtime_imports(
    tmp_path: Path,
    worker: str,
    failure: str,
) -> None:
    target = tmp_path / worker
    shutil.copyfile(ROOT / "scripts" / worker, target)
    if failure == "tampered":
        shutil.copyfile(HELPER, tmp_path / HELPER.name)
        with (tmp_path / HELPER.name).open("a") as stream:
            stream.write("\n# modified fixture helper\n")
    result = subprocess.run(
        [sys.executable, "-I", str(target)],
        capture_output=True,
        text=True,
        env={**os.environ, **namespace()["launch_environment"]()},
        timeout=5,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert ("hash changed" if failure == "tampered" else "worker_parent_guard.py") in result.stderr
    assert "No module named 'torch'" not in result.stderr


def test_controller_passes_actual_identity_without_changing_json_protocol() -> None:
    source = (
        "import json, os, sys\n"
        "print(json.dumps({'status':'ready','parent':os.environ['WHOOPY_PARENT_PID'],"
        "'token':os.environ['WHOOPY_PARENT_CREATION_TOKEN']}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    break\n"
    )
    controller = JsonLineProcessController(
        command=[sys.executable, "-c", source],
        label="guard fixture",
        startup_timeout_seconds=3,
        request_timeout_seconds=3,
        shutdown_timeout_seconds=1,
        diagnostics=BoundedDiagnostics(),
    )
    try:
        ready = controller.start()
        assert ready["parent"] == str(os.getpid())
        assert bool(ready["token"]) == (os.name == "nt")
    finally:
        controller.close()


def test_guardian_preserves_native_streams_exit_code_and_recorded_argv(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; print('native out'); print('native err', file=sys.stderr)",
    ]
    capture = CapturingProcessRunner(tmp_path / "capture", _run_process)
    result = capture(command, 3)
    assert isinstance(result, LlamaProcessResult)
    assert result.completed.returncode == 0
    assert result.completed.stdout == "native out\n"
    assert result.completed.stderr == "native err\n"
    receipt = json.loads((tmp_path / "capture/001/process.json").read_text())
    assert json.loads((tmp_path / "capture/001/command.json").read_text()) == command
    assert receipt["execution_guard"]["command"][-len(command) :] == command
    assert (
        receipt["execution_guard"]["helper_sha256"]
        == hashlib.sha256(HELPER.read_bytes()).hexdigest()
    )


@pytest.mark.skipif(os.name != "posix", reason="Asserts POSIX process-group descendant cleanup")
@pytest.mark.parametrize("ignore_term", [False, True])
def test_guardian_stops_only_owned_native_group_after_launcher_sigkill(
    tmp_path: Path,
    ignore_term: bool,
) -> None:
    marker = tmp_path / "native.json"
    child_source = (
        "import json, os, pathlib, signal, subprocess, sys, time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "descendant = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(8)'])\n"
        f"pathlib.Path({str(marker)!r}).write_text(\n"
        "json.dumps([os.getpid(), descendant.pid, os.getppid()]))\n"
        "time.sleep(8)\n"
    )
    launcher = (
        "import sys\nfrom whoopy.adapters.llm.llama_cpp import _run_process\n"
        f"_run_process([sys.executable, '-c', {child_source!r}], 20)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    parent = subprocess.Popen([sys.executable, "-c", launcher], text=True, env=env)
    sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(8)"], text=True)
    try:
        wait_file(marker, parent)
        pids = json.loads(marker.read_text())
        parent.kill()
        parent.wait(timeout=3)
        wait_exited(pids)
        assert sentinel.poll() is None
    finally:
        stop_owned(parent)
        stop_owned(sentinel)


@pytest.mark.parametrize("worker", WORKERS)
def test_packaged_sibling_helper_loads_under_isolated_python(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    worker: str,
) -> None:
    from whoopy import locations

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    packaged = project["tool"]["setuptools"]["data-files"]["share/whoopy/scripts"]
    assert "scripts/worker_parent_guard.py" in packaged
    data = tmp_path / "share/whoopy"
    scripts = data / "scripts"
    scripts.mkdir(parents=True)
    for name in (worker, HELPER.name):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    monkeypatch.setattr(locations, "SOURCE_PROJECT_ROOT", tmp_path / "absent-checkout")
    monkeypatch.setattr(locations, "installed_data_root", lambda: data)
    installed_worker = locations.runtime_worker_script(worker)
    assert installed_worker == scripts / worker
    result = subprocess.run(
        [sys.executable, "-I", str(installed_worker), "--help"],
        env={**os.environ, **namespace()["launch_environment"]()},
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("usage:")
    assert "Whoopy parent guard" not in result.stdout
    assert result.stderr == ""
