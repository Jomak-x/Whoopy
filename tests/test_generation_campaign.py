"""No weights, network, ASR, or speech: real small subprocesses exercise orchestration."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest
from pydantic import ValidationError

from scripts import generation_campaign as runner

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/evaluation/generation_quality_2026_11.json"
SCRIPT = ROOT / "scripts/generation_campaign.py"
POSIX_EXECUTION = pytest.mark.skipif(
    os.name != "posix", reason="Campaign execution uses POSIX flock and process groups"
)


def observer(
    available: Callable[[], SimpleNamespace] | None = None,
    swap: Callable[[], SimpleNamespace] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        virtual_memory=available or (lambda: SimpleNamespace(available=32 * runner.GIB)),
        swap_memory=swap or (lambda: SimpleNamespace(used=0)),
        Process=psutil.Process,
        NoSuchProcess=psutil.NoSuchProcess,
        AccessDenied=psutil.AccessDenied,
    )


def run_child(tmp_path: Path, code: str, **overrides: Any) -> dict[str, Any]:
    options: dict[str, Any] = dict(
        cwd=tmp_path,
        env=runner.offline_environment(tmp_path),
        timeout=5,
        deadline=time.monotonic() + 10,
        minimum_gib=8,
        psutil=observer(),
        poll_seconds=0.01,
    )
    options.update(overrides)
    return runner.bounded([sys.executable, "-c", code], tmp_path / "attempt", **options)


def args(**changes: Any) -> argparse.Namespace:
    values: dict[str, Any] = dict(
        case=None,
        all_cases=False,
        profile=None,
        method=None,
        voice=None,
        max_cases=None,
        python=Path(sys.executable),
        models_dir=Path("/installed-models"),
        tts_model="kokoro",
        qwen_speaker=None,
        qwen_voice_design_preset=None,
    )
    values.update(changes)
    return argparse.Namespace(**values)


def test_typed_suite_and_cli_arguments(tmp_path: Path) -> None:
    campaign = runner.Campaign.model_validate_json(CONFIG.read_text())
    assert len(campaign.cases) == 6
    assert len(runner.build_queue(campaign, args(), tmp_path)) == 1
    from whoopy.cli import _build_parser

    queue = runner.build_queue(campaign, args(all_cases=True), tmp_path)
    for item in queue:
        parsed = _build_parser().parse_args(item["command"][3:])
        assert parsed.run_id == item["run_id"]
        assert parsed.parallel_sections == 1
        assert parsed.runs_dir.parent == Path(item["folder"])
    assert len({item["run_id"] for item in queue}) == 6
    bad = campaign.model_dump()
    bad["cases"][0]["extra_cli_args"] = ["--download"]
    with pytest.raises(ValidationError):
        runner.Campaign.model_validate(bad)
    bad = campaign.model_dump()
    bad["cases"][0]["minutes"] = "3"
    with pytest.raises(ValidationError):
        runner.Campaign.model_validate(bad)


def test_matrix_budget_profiles_and_mlx_floor(tmp_path: Path) -> None:
    campaign = runner.Campaign.model_validate_json(CONFIG.read_text())
    with pytest.raises(ValueError, match="Matrix has 12"):
        runner.build_queue(campaign, args(all_cases=True, profile=["high", "standard"]), tmp_path)
    queue = runner.build_queue(campaign, args(profile=["high", "standard", "lite"]), tmp_path)
    assert [item["minimum_available_gib"] for item in queue] == [16, 8, 4]
    mlx = runner.build_queue(
        campaign, args(profile=["lite"], tts_model="mlx-qwen3-custom-voice-1.7b-8bit"), tmp_path
    )
    assert mlx[0]["minimum_available_gib"] == 16
    with pytest.raises(ValueError, match="Basic requires"):
        runner.build_queue(campaign, args(profile=["basic"]), tmp_path)
    campaign.cases[0].script = "Notice the chair. [pause: 3s]"
    basic = runner.build_queue(campaign, args(profile=["basic"]), tmp_path)
    assert basic[0]["minimum_available_gib"] == 2
    assert "--script-file" in basic[0]["command"]


@POSIX_EXECUTION
def test_child_success_and_nonzero_keep_evidence(tmp_path: Path) -> None:
    code = "import sys; print('retained out'); print('retained err', file=sys.stderr); sys.exit(7)"
    receipt = run_child(tmp_path, code)
    assert receipt["status"] == "nonzero"
    assert receipt["returncode"] == 7
    assert receipt["wall_seconds"] > 0
    assert receipt["started_at"] < receipt["ended_at"]
    assert (tmp_path / "attempt/stdout.log").read_text().strip() == "retained out"
    assert (tmp_path / "attempt/stderr.log").read_text().strip() == "retained err"
    assert json.loads((tmp_path / "attempt/receipt.json").read_text()) == receipt
    with pytest.raises(FileExistsError):
        # Receipts are atomic, but logs must never overwrite an attempted child.
        run_child(tmp_path, "print('must not overwrite')")
    receipt = run_child(tmp_path / "success", "print('ok')")
    assert receipt["status"] == "completed"
    assert receipt["returncode"] == 0


@pytest.mark.parametrize("cause", ["memory", "deadline"])
def test_refused_preflight_never_starts_child(tmp_path: Path, cause: str) -> None:
    marker = tmp_path / "child-started"
    options = (
        {"psutil": observer(available=lambda: SimpleNamespace(available=7 * runner.GIB))}
        if cause == "memory"
        else {"deadline": time.monotonic() - 1}
    )
    receipt = run_child(tmp_path, f"open({str(marker)!r}, 'w').write('started')", **options)
    assert receipt["status"] == "not_started"
    assert not marker.exists()
    assert (tmp_path / "attempt/stdout.log").exists()
    assert receipt["wall_seconds"] >= 0


@pytest.mark.parametrize("limit", ["timeout", "deadline", "swap", "ram"])
@POSIX_EXECUTION
def test_runtime_stops_real_child(tmp_path: Path, limit: str) -> None:
    options: dict[str, Any] = {}
    if limit == "timeout":
        options["timeout"] = 0.08
    elif limit == "deadline":
        options["deadline"] = time.monotonic() + 0.08
    elif limit == "swap":
        swaps = iter([0, 5 * runner.GIB])
        options["psutil"] = observer(swap=lambda: SimpleNamespace(used=next(swaps, 5 * runner.GIB)))
    else:
        memory = iter([32 * runner.GIB])
        options["psutil"] = observer(
            available=lambda: SimpleNamespace(available=next(memory, runner.GIB))
        )
    receipt = run_child(tmp_path, "import time; time.sleep(30)", **options)
    assert receipt["status"] == "stopped"
    assert receipt["returncode"] is not None
    assert receipt["wall_seconds"] < 6
    assert (
        receipt["reason"]
        == {
            "timeout": "case timeout",
            "deadline": "campaign deadline",
            "swap": "swap growth exceeded 4 GiB",
            "ram": "available RAM below 2 GiB for two seconds",
        }[limit]
    )
    assert not psutil.pid_exists(receipt["pid"])


@POSIX_EXECUTION
def test_timeout_stops_detached_descendant(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    code = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        "start_new_session=True); "
        f"open({str(pid_file)!r}, 'w').write(str(p.pid)); "
        "time.sleep(30)"
    )
    receipt = run_child(tmp_path, code, timeout=0.2)
    assert receipt["status"] == "stopped"
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
            break
        time.sleep(0.01)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE


def test_environment_omits_tokens_and_sets_offline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "never-retain")
    monkeypatch.setenv("HF_TOKEN", "never-retain")
    monkeypatch.setenv("WHOOPY_FAKE_PROVIDER", "never-retain")
    env = runner.offline_environment(tmp_path)
    assert not {"OPENAI_API_KEY", "HF_TOKEN", "WHOOPY_FAKE_PROVIDER"} & env.keys()
    assert env["HF_HUB_OFFLINE"] == env["OMNIGENT_DISABLE_KEYRING"] == "1"
    assert env["PYTHONPATH"] == str(tmp_path / "src")


def tiny_source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    for tree in ("src", "scripts", "config"):
        (source / tree).mkdir(parents=True)
        (source / tree / "frozen.txt").write_text("reviewed source\n")
    (source / "config/local.yaml").write_text("private machine settings")
    return source


def test_real_plan_freezes_hashes_without_running(tmp_path: Path) -> None:
    source = tiny_source(tmp_path)
    output = tmp_path / "campaign"
    command = [
        sys.executable,
        str(SCRIPT),
        "--source",
        str(source),
        "--config",
        str(CONFIG),
        "--output",
        str(output),
        "--models-dir",
        str(tmp_path / "missing-models"),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "planned"
    assert manifest["records"] == []
    assert not (output / "attempts").exists()
    assert (output / ".gitignore").read_text() == "*\n"
    assert "config/local.yaml" not in manifest["source_sha256"]
    assert manifest["source_sha256"] == runner.inventory(output / "source-snapshot")
    (source / "src/frozen.txt").write_text("edited later")
    assert (output / "source-snapshot/src/frozen.txt").read_text() == "reviewed source\n"
    before = (output / "manifest.json").read_bytes()
    again = subprocess.run(command, capture_output=True, text=True, timeout=20)
    assert again.returncode == 1
    assert (output / "manifest.json").read_bytes() == before


@POSIX_EXECUTION
def test_lock_refuses_second_owner(tmp_path: Path) -> None:
    lock = tmp_path / "campaign.lock"
    with runner.exclusive_lock(lock), pytest.raises(BlockingIOError), runner.exclusive_lock(lock):
        pass


def test_freeze_artifact_override(tmp_path: Path) -> None:
    source = tiny_source(tmp_path)
    lock = tmp_path / "experiment.yaml"
    lock.write_text("schema_version: 1\n")
    output = tmp_path / "out"
    output.mkdir()
    hashes = runner.freeze(source, output, lock)
    assert hashes["config/artifacts.yaml"] == runner.digest(lock)
    assert not (source / "config/artifacts.yaml").exists()


def test_inventory_rejects_symlinks(tmp_path: Path) -> None:
    source = tiny_source(tmp_path)
    try:
        (source / "src/linked.py").symlink_to(source / "src/frozen.txt")
    except OSError as error:
        pytest.skip(f"Host does not permit creating test symlinks: {error}")
    with pytest.raises(ValueError, match="symlink"):
        runner.inventory(source / "src")


@POSIX_EXECUTION
def test_execute_serial_stub_queue_retains_failed_attempt_and_continues(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tiny_source(tmp_path)
    package = source / "src/whoopy"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import json, pathlib, sys, time\n"
        "args = sys.argv\n"
        "run_id = args[args.index('--run-id') + 1]\n"
        "folder = pathlib.Path(args[args.index('--runs-dir') + 1]) / run_id\n"
        "folder.mkdir(parents=True)\n"
        "(folder / 'stub.txt').write_text('real child evidence')\n"
        "time.sleep(0.04)\n"
        "print(json.dumps({'run_id': run_id}))\n"
        "sys.exit(7 if args[args.index('--profile') + 1] == 'high' else 0)\n"
    )
    models = tmp_path / "models"
    models.mkdir()
    output = tmp_path / "campaign"
    calls: list[str] = []
    monkeypatch.setitem(sys.modules, "psutil", observer())
    monkeypatch.setattr(runner, "check_idle", lambda *args: calls.append("admitted"))
    result = runner.main(
        [
            "--source",
            str(source),
            "--config",
            str(CONFIG),
            "--output",
            str(output),
            "--models-dir",
            str(models),
            "--profile",
            "high",
            "--profile",
            "standard",
            "--lock-file",
            str(tmp_path / "campaign.lock"),
            "--owner-lock-file",
            str(tmp_path / "owner.lock"),
            "--execute",
        ]
    )
    assert result == 1
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "incomplete"
    assert [record["status"] for record in manifest["records"]] == ["nonzero", "completed"]
    first, second = manifest["records"]
    assert first["ended_at"] <= second["started_at"]
    assert calls == ["admitted", "admitted"]
    for item in manifest["queue"]:
        folder = Path(item["folder"])
        assert (folder / "runs" / item["run_id"] / "stub.txt").read_text() == "real child evidence"
        assert json.loads((folder / "stdout.log").read_text())["run_id"] == item["run_id"]


@pytest.mark.parametrize(
    ("backend", "controls", "flag", "voice", "mode"),
    [
        (
            "mlx-qwen3-custom-voice-1.7b-8bit",
            {"qwen_speaker": "Ryan"},
            "--qwen-speaker",
            "Ryan",
            "preset",
        ),
        (
            "mlx-qwen3-voice-design-1.7b-8bit",
            {"qwen_voice_design_preset": "sleep_meditation"},
            "--qwen-voice-design-preset",
            "sleep_meditation",
            "designed",
        ),
        (
            "qwen3-custom-voice-1.7b",
            {"qwen_speaker": "Ryan"},
            "--qwen-speaker",
            "Ryan",
            "preset",
        ),
        (
            "qwen3-voice-design-1.7b",
            {"qwen_voice_design_preset": "sleep_meditation"},
            "--qwen-voice-design-preset",
            "sleep_meditation",
            "designed",
        ),
    ],
)
def test_qwen_backend_controls_and_actual_voice_identity(
    tmp_path: Path,
    backend: str,
    controls: dict[str, Any],
    flag: str,
    voice: str,
    mode: str,
) -> None:
    campaign = runner.Campaign.model_validate_json(CONFIG.read_text())
    item = runner.build_queue(campaign, args(tts_model=backend, **controls), tmp_path)[0]
    from whoopy.cli import _build_parser

    parsed = _build_parser().parse_args(item["command"][3:])
    assert getattr(parsed, flag[2:].replace("-", "_")) == voice
    assert item["voice"] == voice
    assert item["voice_mode"] == mode
    assert item["voice_control"] == flag
    assert item["tts_model"] == backend
    assert "--voice" not in item["command"]
    excluded_flag = "--qwen-speaker" if mode == "designed" else "--qwen-voice-design-preset"
    assert excluded_flag not in item["command"]


@pytest.mark.parametrize(
    "controls",
    [
        {"tts_model": "mlx-qwen3-custom-voice-1.7b-8bit", "voice": ["af_heart"]},
        {
            "tts_model": "mlx-qwen3-custom-voice-1.7b-8bit",
            "qwen_voice_design_preset": "warm_meditation",
        },
        {"tts_model": "mlx-qwen3-voice-design-1.7b-8bit", "qwen_speaker": "Serena"},
        {"tts_model": "qwen3-custom-voice-1.7b", "voice": ["af_heart"]},
        {"tts_model": "qwen3-custom-voice-1.7b", "qwen_voice_design_preset": "warm_meditation"},
        {"tts_model": "qwen3-voice-design-1.7b", "qwen_speaker": "Ryan"},
        {"qwen_speaker": "Ryan"},
        {"qwen_voice_design_preset": "sleep_meditation"},
    ],
)
def test_backend_rejects_inapplicable_voice_controls(
    tmp_path: Path,
    controls: dict[str, Any],
) -> None:
    campaign = runner.Campaign.model_validate_json(CONFIG.read_text())
    with pytest.raises(ValueError):
        runner.build_queue(campaign, args(**controls), tmp_path)


@pytest.mark.parametrize(
    "backend",
    [
        "mlx-qwen3-custom-voice-1.7b-8bit",
        "mlx-qwen3-voice-design-1.7b-8bit",
        "qwen3-custom-voice-1.7b",
        "qwen3-voice-design-1.7b",
    ],
)
def test_speed_bounds_and_qwen_speed_are_validated_before_execution(
    tmp_path: Path,
    backend: str,
) -> None:
    campaign = runner.Campaign.model_validate_json(CONFIG.read_text())
    for speed in (0.49, 1.21):
        payload = campaign.model_dump()
        payload["cases"][0]["speed"] = speed
        with pytest.raises(ValidationError):
            runner.Campaign.model_validate(payload)
    campaign.cases[0].speed = 0.7
    with pytest.raises(ValueError, match="Qwen speech does not support numeric"):
        runner.build_queue(campaign, args(tts_model=backend), tmp_path)
    item = runner.build_queue(campaign, args(), tmp_path)[0]
    assert item["command"][item["command"].index("--speed") + 1] == "0.7"


@POSIX_EXECUTION
def test_mlx_real_child_can_acquire_gpu_lock_while_campaign_owns_separate_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tiny_source(tmp_path)
    gpu_lock = tmp_path / "gpu.lock"
    owner_lock = tmp_path / "owner.lock"
    package = source / "src/whoopy"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import fcntl, pathlib\n"
        f"with pathlib.Path({str(owner_lock)!r}).open('a') as owner:\n"
        "    try:\n"
        "        fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n"
        "        pass\n"
        "    else:\n"
        "        raise RuntimeError('campaign ownership was not retained')\n"
        f"with pathlib.Path({str(gpu_lock)!r}).open('a') as gpu:\n"
        "    fcntl.flock(gpu, fcntl.LOCK_EX)\n"
        "    print('native worker acquired GPU lock', flush=True)\n"
    )
    models = tmp_path / "models"
    models.mkdir()
    output = tmp_path / "campaign"
    monkeypatch.setitem(sys.modules, "psutil", observer())
    monkeypatch.setattr(runner, "check_idle", lambda *args: None)
    result = runner.main(
        [
            "--source",
            str(source),
            "--config",
            str(CONFIG),
            "--output",
            str(output),
            "--models-dir",
            str(models),
            "--tts-model",
            "mlx-qwen3-voice-design-1.7b-8bit",
            "--qwen-voice-design-preset",
            "grounded_meditation",
            "--lock-file",
            str(gpu_lock),
            "--owner-lock-file",
            str(owner_lock),
            "--timeout-seconds",
            "2",
            "--total-seconds",
            "3",
            "--execute",
        ]
    )
    assert result == 0
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["queue"][0]["voice"] == "grounded_meditation"
    folder = Path(manifest["queue"][0]["folder"])
    assert (folder / "stdout.log").read_text().strip() == "native worker acquired GPU lock"
    assert manifest["records"][0]["wall_seconds"] < 2


@pytest.fixture
def provenance_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if shutil.which("git") is None:
        pytest.skip("Git is required for provenance fixture checks")
    # Only a disposable local fixture receives a commit; ignore user hooks/signing config.
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    repository = tmp_path / "repository"
    repository.mkdir()
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    commands = [
        ["init", "--quiet"],
        [
            "-c",
            "user.name=Campaign fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            f"core.hooksPath={hooks}",
            "commit",
            "--quiet",
            "--allow-empty",
            "-m",
            "Local provenance fixture",
        ],
    ]
    for command in commands:
        subprocess.run(
            ["git", *command], cwd=repository, check=True, capture_output=True, timeout=10
        )
    return repository


def test_git_provenance_only_attests_the_actual_repository_root(
    provenance_repository: Path,
) -> None:
    repository = provenance_repository
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout.strip()
    clean = runner.git_provenance(repository)
    assert clean["available"] is True
    assert clean["head"] == head
    assert clean["dirty"] is False
    assert len(str(clean["tracked_diff_sha256"])) == 64
    assert len(str(clean["status_sha256"])) == 64
    (repository / "src").mkdir()
    (repository / "src/new.py").write_text("# untracked source is covered by content inventory\n")
    dirty = runner.git_provenance(repository)
    assert dirty["available"] is True
    assert dirty["head"] == head
    assert dirty["dirty"] is True
    assert dirty["status_sha256"] != clean["status_sha256"]


def test_nested_snapshot_does_not_inherit_parent_git_provenance(
    provenance_repository: Path,
) -> None:
    snapshot = provenance_repository / "frozen/source-snapshot"
    (snapshot / "src").mkdir(parents=True)
    (snapshot / "src/accepted.py").write_text("# accepted frozen bytes\n")
    before = runner.inventory(snapshot)
    result = runner.git_provenance(snapshot)
    assert result["available"] is False
    assert "not the Git repository root" in str(result["reason"])
    assert not {"head", "tracked_diff_sha256", "status_sha256", "dirty"} & result.keys()
    assert runner.inventory(snapshot) == before


@pytest.mark.parametrize("store_name", ["managed", "custom-store"])
@POSIX_EXECUTION
def test_idle_probe_uses_normalized_models_root(tmp_path: Path, store_name: str) -> None:
    from whoopy.model_packs.operations import HeavyweightModelSlot

    models_dir = tmp_path / "models" / store_name
    models_dir.mkdir(parents=True)
    expected = models_dir.parent if store_name == "managed" else models_dir
    with HeavyweightModelSlot(expected, "native-tts"), pytest.raises(BlockingIOError):
        runner.check_idle(observer(), models_dir)


@pytest.mark.parametrize(
    ("backend", "expected"),
    [
        ("qwen3-custom-voice-1.7b", [12, 12, 12, 16]),
        ("qwen3-voice-design-1.7b", [12, 12, 12, 16]),
        ("mlx-qwen3-custom-voice-1.7b-8bit", [16, 16, 16, 16]),
        ("mlx-qwen3-voice-design-1.7b-8bit", [16, 16, 16, 16]),
    ],
)
def test_qwen_floor_never_lowers_the_writer_profile_admission(
    tmp_path: Path,
    backend: str,
    expected: list[int],
) -> None:
    campaign = runner.Campaign.model_validate_json(CONFIG.read_text())
    campaign.cases[0].script = "Fixed speech. [pause: 3s]"
    queue = runner.build_queue(
        campaign,
        args(
            tts_model=backend,
            profile=["basic", "lite", "standard", "high"],
        ),
        tmp_path,
    )
    assert [item["minimum_available_gib"] for item in queue] == expected


@pytest.mark.parametrize(
    ("backend", "control", "voice"),
    [
        ("qwen3-custom-voice-1.7b", "--qwen-speaker", "Ryan"),
        ("qwen3-voice-design-1.7b", "--qwen-voice-design-preset", "sleep_meditation"),
    ],
)
@POSIX_EXECUTION
def test_native_qwen_fixed_script_child_observes_parent_gpu_ownership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    control: str,
    voice: str,
) -> None:
    source = tiny_source(tmp_path)
    gpu_lock = tmp_path / "gpu.lock"
    package = source / "src/whoopy"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import fcntl, pathlib, sys\n"
        f"with pathlib.Path({str(gpu_lock)!r}).open('a') as gpu:\n"
        "    try:\n"
        "        fcntl.flock(gpu, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "    except BlockingIOError:\n"
        "        pass\n"
        "    else:\n"
        "        raise RuntimeError('native campaign did not retain GPU ownership')\n"
        "script = pathlib.Path(sys.argv[sys.argv.index('--script-file') + 1]).read_text()\n"
        "assert script == 'Same fixed script. [pause: 3s]'\n"
        f"assert sys.argv[sys.argv.index({control!r}) + 1] == {voice!r}\n"
        "assert '--voice' not in sys.argv\n"
        "print('native fixed script and parent GPU ownership verified', flush=True)\n"
    )
    campaign = runner.Campaign.model_validate_json(CONFIG.read_text())
    campaign.cases[0].script = "Same fixed script. [pause: 3s]"
    config = tmp_path / "fixed-script.json"
    config.write_text(campaign.model_dump_json())
    models = tmp_path / "models"
    models.mkdir()
    output = tmp_path / "campaign"
    monkeypatch.setitem(sys.modules, "psutil", observer())
    monkeypatch.setattr(runner, "check_idle", lambda *args: None)
    result = runner.main(
        [
            "--source",
            str(source),
            "--config",
            str(config),
            "--output",
            str(output),
            "--models-dir",
            str(models),
            "--tts-model",
            backend,
            control,
            voice,
            "--profile",
            "basic",
            "--lock-file",
            str(gpu_lock),
            "--owner-lock-file",
            str(tmp_path / "owner.lock"),
            "--execute",
        ]
    )
    assert result == 0
    manifest = json.loads((output / "manifest.json").read_text())
    item = manifest["queue"][0]
    assert item["voice"] == voice
    assert item["minimum_available_gib"] == 12
    assert item["tts_model"] == backend
    folder = Path(item["folder"])
    assert (folder / "stdout.log").read_text().strip() == (
        "native fixed script and parent GPU ownership verified"
    )
