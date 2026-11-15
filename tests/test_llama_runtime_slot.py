"""Model-free writer admission: fake outputs and real cross-process slot probes."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from whoopy.adapters.llm.llama_cpp import LlamaCppScriptGenerator, ProcessRunner
from whoopy.artifacts import ArtifactSpec, ArtifactStore, TargetPlatform, load_artifact_lock
from whoopy.model_packs.operations import HeavyweightModelSlot
from whoopy.ports import (
    FatalAdapterError,
    InvalidAdapterOutput,
    ScriptGenerationRequest,
    TransientAdapterError,
)

ROOT = Path(__file__).resolve().parents[1]


def writer(
    root: Path,
    process_runner: ProcessRunner,
    *,
    models_root: Path | None,
) -> LlamaCppScriptGenerator:
    executable, model = root / "llama-cli", root / "fixture.gguf"
    executable.touch()
    model.touch()
    return LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="fixture@1",
        runtime_version="fixture",
        license_id="CC0-1.0",
        device="fixture",
        models_root=models_root,
        process_runner=process_runner,
    )


def output(command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
    argv = list(command)
    Path(argv[argv.index("--output-file") + 1]).write_text("A fixture sentence.")
    return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


def probe_slot(root: Path) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from whoopy.model_packs.operations import HeavyweightModelSlot, "
        "HeavyweightModelSlotUnavailable\n"
        "try:\n"
        "    with HeavyweightModelSlot(Path(sys.argv[1]), 'speech-screen-mlx-whisper'):\n"
        "        print('acquired')\n"
        "except HeavyweightModelSlotUnavailable:\n"
        "    print('blocked')\n"
        "    raise SystemExit(17)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "OMNIGENT_DISABLE_KEYRING": "1"}
    return subprocess.run(
        [sys.executable, "-c", code, str(root)],
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_writer_slot_blocks_real_cross_process_speech_admission(tmp_path: Path) -> None:
    models_root = tmp_path / "models"
    probes: list[subprocess.CompletedProcess[str]] = []

    def run(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        probes.append(probe_slot(models_root))
        return output(command, timeout)

    adapter = writer(tmp_path, run, models_root=models_root)
    assert not (models_root / "runtime-slot").exists()
    assert (
        adapter.generate(ScriptGenerationRequest(prompt="Fixture.")).text == "A fixture sentence."
    )
    assert probes[0].returncode == 17, probes[0].stderr
    assert probes[0].stdout.strip() == "blocked"
    assert probe_slot(models_root).returncode == 0


def test_foreign_speech_owner_prevents_any_writer_launch(tmp_path: Path) -> None:
    calls: list[Sequence[str]] = []

    def run(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        return output(command, timeout)

    models_root = tmp_path / "models"
    adapter = writer(tmp_path, run, models_root=models_root)
    ready = tmp_path / "foreign-owner-ready"
    code = (
        "import sys\nfrom pathlib import Path\n"
        "from whoopy.model_packs.operations import HeavyweightModelSlot\n"
        "with HeavyweightModelSlot(Path(sys.argv[1]), 'speech-screen-mlx-whisper'):\n"
        "    Path(sys.argv[2]).write_text('ready')\n"
        "    sys.stdin.readline()\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "OMNIGENT_DISABLE_KEYRING": "1"}
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(models_root), str(ready)],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "Foreign slot holder did not become ready"
        with pytest.raises(TransientAdapterError, match="slot is already in use"):
            adapter.generate(ScriptGenerationRequest(prompt="Must not launch."))
        assert calls == []
    finally:
        try:
            _stdout, stderr = child.communicate("release\n", timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate(timeout=5)
            raise
    assert child.returncode == 0, stderr


@pytest.mark.parametrize(
    "outcome", ["success", "nonzero", "timeout", "start_error", "cancel", "empty"]
)
def test_writer_releases_slot_after_every_terminal_outcome(tmp_path: Path, outcome: str) -> None:
    models_root = tmp_path / "models"

    def run(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        owner = HeavyweightModelSlot(models_root, "probe").current_owner()
        assert owner is not None and owner.pack_id == "llama-cpp-writer"
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(command, timeout)
        if outcome == "start_error":
            raise OSError("fixture launch failure")
        if outcome == "cancel":
            raise KeyboardInterrupt
        if outcome == "empty":
            argv = list(command)
            Path(argv[argv.index("--output-file") + 1]).write_text("")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        result = output(command, timeout)
        if outcome == "nonzero":
            result.returncode = 7
        return result

    adapter = writer(tmp_path, run, models_root=models_root)
    request = ScriptGenerationRequest(prompt="Fixture.")
    if outcome == "success":
        adapter.generate(request)
    else:
        failure = {
            "nonzero": FatalAdapterError,
            "timeout": TransientAdapterError,
            "start_error": FatalAdapterError,
            "cancel": KeyboardInterrupt,
            "empty": InvalidAdapterOutput,
        }[outcome]
        with pytest.raises(failure):
            adapter.generate(request)
    with HeavyweightModelSlot(models_root, "next-runtime"):
        pass
    assert not (models_root / "runtime-slot/owner.json").exists()


@pytest.mark.parametrize("store_name", ["managed", "custom-store"])
def test_artifact_factory_normalizes_models_root_without_owning_slot_at_construction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_name: str,
) -> None:
    store = ArtifactStore(tmp_path / "models" / store_name)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "llama-cli").touch()
    model = tmp_path / "fixture.gguf"
    model.touch()

    def require(artifact: ArtifactSpec) -> Path:
        return runtime if artifact.component == "llm_runtime" else model

    monkeypatch.setattr(store, "require", require)
    adapter = LlamaCppScriptGenerator.from_artifact_store(
        artifact_lock=load_artifact_lock(Path("config/artifacts.yaml")),
        store=store,
        profile_name="standard",
        target=TargetPlatform(operating_system="linux", architecture="x86_64"),
        device="fixture",
    )
    expected = store.root.parent if store_name == "managed" else store.root
    assert adapter.models_root == expected
    assert not (expected / "runtime-slot").exists()
    adapter.process_runner = output
    with (
        HeavyweightModelSlot(expected, "existing-runtime"),
        pytest.raises(TransientAdapterError, match="slot is already in use"),
    ):
        adapter.generate(ScriptGenerationRequest(prompt="Do not launch."))


def test_direct_constructor_can_remain_an_isolated_fixture(tmp_path: Path) -> None:
    models_root = tmp_path / "models"
    adapter = writer(tmp_path, output, models_root=None)
    with HeavyweightModelSlot(models_root, "other-runtime"):
        assert (
            adapter.generate(ScriptGenerationRequest(prompt="Fixture.")).text
            == "A fixture sentence."
        )


def test_shared_adapter_serializes_its_own_parallel_sections(tmp_path: Path) -> None:
    active = 0
    maximum = 0
    gate = threading.Lock()

    def run(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
        nonlocal active, maximum
        with gate:
            active += 1
            maximum = max(maximum, active)
        try:
            time.sleep(0.03)
            return output(command, timeout)
        finally:
            with gate:
                active -= 1

    adapter = writer(tmp_path, run, models_root=tmp_path / "models")
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(adapter.generate, ScriptGenerationRequest(prompt="Fixture."))
            for _ in range(2)
        ]
        assert [future.result(timeout=5).text for future in futures] == ["A fixture sentence."] * 2
    assert maximum == 1
