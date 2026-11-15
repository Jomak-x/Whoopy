#!/usr/bin/env python3
"""Freeze and plan local CLI comparisons; inference requires an explicit --execute."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

GIB = 1024**3
PROFILE_MINIMUM_GIB = {"high": 16, "standard": 8, "lite": 4, "basic": 2}
Profile = Literal["high", "standard", "lite", "basic"]
Method = Literal[
    "plan_first",
    "blueprint_first_sparse",
    "three_candidate_structural_selection",
    "recipe_whole_session",
]
Voice = Literal["af_heart", "af_bella", "af_nicole", "am_michael"]
Identifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Case(StrictModel):
    id: Identifier
    prompt: str = Field(min_length=1)
    minutes: float = Field(ge=1, le=30)
    meditation_kind: Literal[
        "grounding",
        "focused_attention",
        "breath_awareness",
        "body_scan",
        "noting",
        "reflection",
        "loving_kindness",
        "self_compassion",
        "visualization",
        "open_awareness",
        "sleep",
    ]
    script: Annotated[str, Field(min_length=1)] | None = None
    seed: int = Field(default=42, ge=0)
    pace: Literal["natural", "unhurried", "slow", "very_slow"] = "slow"
    pacing_profile: str | None = None
    delivery: (
        Literal[
            "neutral",
            "warm",
            "calm",
            "soothing",
            "grounded",
            "compassionate",
            "sleepy",
            "encouraging",
        ]
        | None
    ) = None
    intensity: Literal["subtle", "moderate"] | None = None
    speed: float | None = Field(default=None, ge=0.5, le=1.2)


class Campaign(StrictModel):
    schema_version: Literal[1]
    cases: list[Case] = Field(min_length=1)
    default_cases: list[str] = Field(min_length=1)
    default_profiles: list[Profile] = Field(min_length=1)
    default_methods: list[Method] = Field(min_length=1)
    default_voices: list[Voice] = Field(min_length=1)
    timeout_seconds: float = Field(default=1800, gt=0, le=7200)
    total_seconds: float = Field(default=7200, gt=0, le=43200)
    max_cases: int = Field(default=6, ge=1, le=100)

    @model_validator(mode="after")
    def unique_cases(self) -> Campaign:
        identifiers = [case.id for case in self.cases]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("case IDs must be unique")
        if not set(self.default_cases) <= set(identifiers):
            raise ValueError("default_cases contains an unknown case")
        return self


def save(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def inventory(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc" or path.name == "local.yaml":
            continue
        if path.is_symlink():
            raise ValueError(f"Snapshot input must not be a symlink: {path}")
        if path.is_file():
            result[str(path.relative_to(root))] = digest(path)
    return result


def freeze(source: Path, output: Path, artifact_lock: Path | None) -> dict[str, str]:
    snapshot = output / "source-snapshot"
    before = {tree: inventory(source / tree) for tree in ("src", "scripts", "config")}
    snapshot.mkdir()
    for tree in before:
        shutil.copytree(
            source / tree,
            snapshot / tree,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "local.yaml"),
        )
        if inventory(snapshot / tree) != before[tree] or inventory(source / tree) != before[tree]:
            raise ValueError(f"Source {tree} changed while freezing; use a fresh output directory")
    if artifact_lock is not None:
        if artifact_lock.is_symlink() or not artifact_lock.is_file():
            raise ValueError("--artifact-lock must name a regular local file")
        original_digest = digest(artifact_lock)
        shutil.copyfile(artifact_lock, snapshot / "config/artifacts.yaml")
        if digest(snapshot / "config/artifacts.yaml") != original_digest:
            raise ValueError("Artifact lock changed while being frozen")
    return inventory(snapshot)


def git_provenance(source: Path) -> dict[str, str | bool]:
    """Supplement frozen content hashes with read-only tracked Git provenance."""
    try:
        top_level = subprocess.run(
            ["git", "--no-optional-locks", "rev-parse", "--show-toplevel"],
            cwd=source,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return {"available": False, "reason": "Source has no readable Git repository root"}
    if Path(os.fsdecode(top_level.stdout).rstrip("\r\n")).resolve() != source.resolve():
        return {
            "available": False,
            "reason": (
                "Source is not the Git repository root; frozen content hashes are authoritative"
            ),
        }
    result: dict[str, str | bool] = {}
    commands = {
        "head": ["rev-parse", "HEAD"],
        "tracked_diff_sha256": [
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--binary",
            "HEAD",
            "--",
            "src",
            "scripts",
            "config",
        ],
        "status_sha256": [
            "status",
            "--porcelain",
            "--untracked-files=normal",
            "--",
            "src",
            "scripts",
            "config",
        ],
    }
    for name, arguments in commands.items():
        try:
            completed = subprocess.run(
                ["git", "--no-optional-locks", *arguments],
                cwd=source,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return {
                "available": False,
                "reason": "Source Git metadata could not be read completely",
            }
        result[name] = (
            completed.stdout.decode().strip()
            if name == "head"
            else hashlib.sha256(completed.stdout).hexdigest()
        )
        if name == "status_sha256":
            result["dirty"] = bool(completed.stdout)
    result["available"] = True
    return result


def offline_environment(snapshot: Path) -> dict[str, str]:
    # An allowlist avoids retaining tokens/provider settings in subprocesses or receipts.
    keys = ("PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR", "COMSPEC", "LANG", "LC_ALL")
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update(
        PYTHONPATH=str(snapshot / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_DATASETS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        OMNIGENT_DISABLE_KEYRING="1",
        PIP_NO_INDEX="1",
        TOKENIZERS_PARALLELISM="false",
    )
    return env


def build_queue(
    campaign: Campaign,
    args: argparse.Namespace,
    output: Path,
) -> list[dict[str, Any]]:
    selected = args.case or campaign.default_cases
    if args.all_cases:
        selected = [case.id for case in campaign.cases]
    unknown = set(selected) - {case.id for case in campaign.cases}
    if unknown:
        raise ValueError(f"Unknown case IDs: {sorted(unknown)}")
    profiles = args.profile or campaign.default_profiles
    methods = args.method or campaign.default_methods
    voices: list[str]
    is_mlx = args.tts_model.startswith("mlx-")
    is_native_qwen = args.tts_model in {
        "qwen3-custom-voice-1.7b",
        "qwen3-voice-design-1.7b",
    }
    custom_voice = args.tts_model in {
        "mlx-qwen3-custom-voice-1.7b-8bit",
        "qwen3-custom-voice-1.7b",
    }
    if is_mlx or is_native_qwen:
        if args.voice:
            raise ValueError("--voice selects Kokoro voices; use the matching Qwen voice control")
        if any(case.speed is not None for case in campaign.cases if case.id in selected):
            raise ValueError("Qwen speech does not support numeric --speed")
        if custom_voice:
            if args.qwen_voice_design_preset is not None:
                raise ValueError("CustomVoice requires --qwen-speaker, not a design preset")
            voices = [args.qwen_speaker or "Serena"]
            voice_flag, voice_mode = "--qwen-speaker", "preset"
        else:
            if args.qwen_speaker is not None:
                raise ValueError("VoiceDesign requires --qwen-voice-design-preset, not a speaker")
            voices = [args.qwen_voice_design_preset or "warm_meditation"]
            voice_flag, voice_mode = "--qwen-voice-design-preset", "designed"
    else:
        if args.qwen_speaker is not None or args.qwen_voice_design_preset is not None:
            raise ValueError("Kokoro requires --voice, not Qwen voice controls")
        voices = list(args.voice or campaign.default_voices)
        voice_flag, voice_mode = "--voice", "preset"
    count = len(set(selected)) * len(set(profiles)) * len(set(methods)) * len(set(voices))
    budget = args.max_cases if args.max_cases is not None else campaign.max_cases
    if count > budget:
        raise ValueError(f"Matrix has {count} cases; raise --max-cases above {budget} explicitly")
    queue: list[dict[str, Any]] = []
    for case in campaign.cases:
        if case.id not in selected:
            continue
        for profile in dict.fromkeys(profiles):
            if profile == "basic" and not case.script:
                raise ValueError("Basic requires an authored script case; it cannot draft prompts")
            for method in dict.fromkeys(methods):
                for voice in dict.fromkeys(voices):
                    run_id = str(uuid4())
                    folder = output / "attempts" / f"{len(queue) + 1:03d}-{case.id}-{run_id}"
                    command = [
                        str(args.python.absolute()),
                        "-m",
                        "whoopy",
                        "generate",
                        *(
                            ["--script-file", str(folder / "script.txt")]
                            if case.script
                            else [case.prompt]
                        ),
                        "--minutes",
                        str(case.minutes),
                        "--meditation-kind",
                        case.meditation_kind,
                        "--profile",
                        profile,
                        "--writing-method",
                        method,
                        voice_flag,
                        voice,
                        "--tts-model",
                        args.tts_model,
                        "--parallel-sections",
                        "1",
                        "--pace",
                        case.pace,
                        "--seed",
                        str(case.seed),
                        "--run-id",
                        run_id,
                        "--audience",
                        "lab",
                        "--json",
                        "--config-dir",
                        str(output / "source-snapshot/config"),
                        "--models-dir",
                        str(args.models_dir.resolve()),
                        "--runs-dir",
                        str(folder / "runs"),
                    ]
                    for name in ("pacing_profile", "delivery", "intensity", "speed"):
                        value = getattr(case, name)
                        if value is not None:
                            command.extend(["--" + name.replace("_", "-"), str(value)])
                    minimum = max(
                        PROFILE_MINIMUM_GIB[profile],
                        16 if is_mlx else 12 if is_native_qwen else 0,
                    )
                    queue.append(
                        {
                            "case": case.model_dump(),
                            "profile": profile,
                            "method": method,
                            "voice": voice,
                            "voice_mode": voice_mode,
                            "tts_model": args.tts_model,
                            "voice_control": voice_flag,
                            "run_id": run_id,
                            "folder": str(folder),
                            "command": command,
                            "minimum_available_gib": minimum,
                        }
                    )
    return queue


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    if os.name != "posix":
        raise RuntimeError("Campaign execution currently requires POSIX advisory locking")
    import fcntl

    if path.is_symlink():
        raise ValueError(f"Lock must not be a symlink: {path}")
    with path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def check_idle(psutil: Any, models_dir: Path) -> None:
    from whoopy.model_packs.resolution import models_root_from_artifact_store

    runtime = models_root_from_artifact_store(models_dir) / "runtime-slot/heavyweight.lock"
    if runtime.exists():
        with exclusive_lock(runtime):
            pass
    own = psutil.Process(os.getpid())
    excluded = {own.pid, *[parent.pid for parent in own.parents()]}
    names = {
        "llama-cli",
        "llama-server",
        "qwen3_tts_worker.py",
        "mlx_qwen3_tts_worker.py",
        "benchmark_voice_candidates.py",
        "benchmark_writing_methods.py",
        "verify_benchmark_audio.py",
        "raw_qwen_meditation.py",
        "dots_tts_full_meditation.py",
    }
    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            words = process.info["cmdline"] or []
            if process.pid not in excluded and (
                ("whoopy" in words and "generate" in words)
                or any(Path(word).name in names for word in words)
            ):
                raise RuntimeError(f"External inference process detected: pid {process.pid}")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def terminate_tree(process: subprocess.Popen[bytes], psutil: Any) -> None:
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied, PermissionError):
        children = []
    for child in reversed(children):
        with suppress(psutil.NoSuchProcess):
            child.send_signal(signal.SIGINT)
    if process.poll() is None:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGINT)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=15)
    for child in reversed(children):
        with suppress(psutil.NoSuchProcess):
            if child.is_running():
                child.kill()
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def bounded(
    command: list[str],
    folder: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    deadline: float,
    minimum_gib: float,
    psutil: Any,
    poll_seconds: float = 0.5,
    script: str | None = None,
) -> dict[str, Any]:
    """Retain every attempt, including refused admission and interrupted children."""
    folder.mkdir(parents=True, exist_ok=False)
    if script is not None:
        (folder / "script.txt").write_text(script)
    started = time.monotonic()
    baseline = psutil.swap_memory().used
    result: dict[str, Any] = {
        "command": command,
        "cwd": str(cwd),
        "environment": env,
        "started_at": datetime.now(UTC).isoformat(),
        "status": "preflight",
        "swap_baseline_bytes": baseline,
        "timeout_seconds": timeout,
        "minimum_available_gib": minimum_gib,
    }
    process: subprocess.Popen[bytes] | None = None
    low_since = None
    try:
        save(folder / "receipt.json", result)
        with (folder / "stdout.log").open("x") as out, (folder / "stderr.log").open("x") as err:
            if started >= deadline or psutil.virtual_memory().available < minimum_gib * GIB:
                result.update(status="not_started", reason="deadline or memory preflight refused")
                return result
            process = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
            result.update(status="running", pid=process.pid)
            save(folder / "receipt.json", result)
            with (folder / "resources.jsonl").open("x", buffering=1) as samples:
                while process.poll() is None:
                    now = time.monotonic()
                    available = psutil.virtual_memory().available
                    swap = psutil.swap_memory().used
                    samples.write(
                        json.dumps(
                            {
                                "elapsed_seconds": now - started,
                                "available_bytes": available,
                                "swap_used_bytes": swap,
                            }
                        )
                        + "\n"
                    )
                    low_since = (
                        (now if low_since is None else low_since) if available < 2 * GIB else None
                    )
                    reason = (
                        "swap growth exceeded 4 GiB"
                        if swap - baseline > 4 * GIB
                        else "available RAM below 2 GiB for two seconds"
                        if low_since is not None and now - low_since >= 2
                        else "case timeout"
                        if now - started >= timeout
                        else "campaign deadline"
                        if now >= deadline
                        else None
                    )
                    if reason:
                        result.update(status="stopped", reason=reason)
                        terminate_tree(process, psutil)
                        break
                    time.sleep(poll_seconds)
            if result["status"] == "running":
                result["status"] = "completed" if process.returncode == 0 else "nonzero"
            result["returncode"] = process.returncode
            available = psutil.virtual_memory().available
            swap = psutil.swap_memory().used
            result["final_system"] = {"available_bytes": available, "swap_used_bytes": swap}
            if result["status"] != "stopped" and (available < 2 * GIB or swap - baseline > 4 * GIB):
                result.update(status="stopped", reason="system pressure at case exit")
            if "MLX resource watchdog:" in (folder / "stderr.log").read_text(errors="replace"):
                result.update(status="stopped", reason="native worker resource watchdog stopped")
    except BaseException as error:
        result.update(status="exception", reason=f"{type(error).__name__}: {error}")
        if process is not None:
            terminate_tree(process, psutil)
            result["returncode"] = process.returncode
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        result["wall_seconds"] = time.monotonic() - started
        result["ended_at"] = datetime.now(UTC).isoformat()
        save(folder / "receipt.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=Path("config/evaluation/generation_quality_2026_11.json")
    )
    parser.add_argument("--source", type=Path, default=Path.cwd())
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--models-dir", type=Path, default=Path("models/managed"))
    parser.add_argument("--artifact-lock", type=Path)
    parser.add_argument(
        "--output", type=Path, required=True, help="Fresh, exclusively created root"
    )
    parser.add_argument("--case", action="append")
    parser.add_argument("--all-cases", action="store_true")
    parser.add_argument("--profile", action="append", choices=tuple(PROFILE_MINIMUM_GIB))
    parser.add_argument("--method", action="append", choices=get_args(Method))
    voice_controls = parser.add_mutually_exclusive_group()
    voice_controls.add_argument("--voice", action="append", choices=get_args(Voice))
    voice_controls.add_argument("--qwen-speaker", choices=("Ryan", "Aiden", "Serena"))
    voice_controls.add_argument(
        "--qwen-voice-design-preset",
        choices=("warm_meditation", "grounded_meditation", "sleep_meditation"),
    )
    parser.add_argument(
        "--tts-model",
        choices=(
            "kokoro",
            "qwen3-custom-voice-1.7b",
            "qwen3-voice-design-1.7b",
            "mlx-qwen3-custom-voice-1.7b-8bit",
            "mlx-qwen3-voice-design-1.7b-8bit",
        ),
        default="kokoro",
    )
    parser.add_argument("--max-cases", type=int)
    parser.add_argument("--timeout-seconds", type=float)
    parser.add_argument("--total-seconds", type=float)
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=Path("/tmp/whoopy-benchmark-gpu.lock"),
        help="Shared GPU lock; MLX workers acquire it themselves",
    )
    parser.add_argument(
        "--owner-lock-file",
        type=Path,
        default=Path("/tmp/whoopy-generation-campaign-owner.lock"),
        help="Separate serial campaign ownership lock",
    )
    parser.add_argument(
        "--execute", action="store_true", help="Actually run serial local inference"
    )
    args = parser.parse_args(argv)
    manifest: dict[str, Any] | None = None
    try:
        campaign = Campaign.model_validate_json(args.config.read_text())
        if args.case and args.all_cases:
            raise ValueError("Use --case or --all-cases, not both")
        for name in ("timeout_seconds", "total_seconds", "max_cases"):
            value = getattr(args, name)
            if value is not None:
                campaign = Campaign.model_validate({**campaign.model_dump(), name: value})
        if args.owner_lock_file.resolve() == args.lock_file.resolve():
            raise ValueError("Campaign ownership and GPU locks must be different files")
        source = args.source.resolve(strict=True)
        output = args.output.absolute()
        if output.resolve() != output:
            raise ValueError("Output must not pass through symlinks; use its canonical path")
        for tree in (
            source / "src",
            source / "scripts",
            source / "config",
            args.models_dir.resolve(),
        ):
            if output == tree or tree in output.parents:
                raise ValueError("Output must be outside frozen source trees and managed models")
        output.mkdir(parents=True, exist_ok=False)
        (output / ".gitignore").write_text("*\n")
        manifest = {
            "schema_version": 1,
            "status": "freezing",
            "campaign_id": str(uuid4()),
            "created_at": datetime.now(UTC).isoformat(),
            "source": str(source),
            "git_provenance": git_provenance(source),
            "output": str(output),
            "execute_requested": args.execute,
            "configuration": campaign.model_dump(),
            "queue": [],
            "records": [],
            "verification": "not_run; use external ASR/listening verification",
            "artifact_lock_original": str(args.artifact_lock) if args.artifact_lock else None,
            "coordination": {
                "owner_lock": str(args.owner_lock_file.absolute()),
                "gpu_lock": str(args.lock_file.absolute()),
            },
        }
        save(output / "manifest.json", manifest)
        manifest["source_sha256"] = freeze(source, output, args.artifact_lock)
        queue = build_queue(campaign, args, output)
        manifest["queue"] = queue
        env = offline_environment(output / "source-snapshot")
        manifest.update(status="planned", environment=env)
        save(output / "manifest.json", manifest)
        if not args.execute:
            print(
                json.dumps(
                    {
                        "status": "planned",
                        "cases": len(queue),
                        "manifest": str(output / "manifest.json"),
                    },
                    indent=2,
                )
            )
            return 0
        import psutil

        if not args.models_dir.is_dir():
            raise ValueError("Execution requires an existing installed --models-dir")
        started = time.monotonic()
        deadline = started + campaign.total_seconds
        with ExitStack() as scope:
            previous_term = signal.getsignal(signal.SIGTERM)

            def interrupt(_signal: int, _frame: Any) -> None:
                raise KeyboardInterrupt("termination requested")

            signal.signal(signal.SIGTERM, interrupt)
            scope.callback(signal.signal, signal.SIGTERM, previous_term)
            scope.enter_context(exclusive_lock(args.owner_lock_file))
            manifest.update(status="running", started_at=datetime.now(UTC).isoformat())
            save(output / "manifest.json", manifest)
            for item in queue:
                with ExitStack() as case_scope:
                    if item["tts_model"].startswith("mlx-"):
                        # Native MLX workers hold this lock. Retaining it across their CLI
                        # would deadlock the worker against its own campaign parent.
                        with exclusive_lock(args.lock_file):
                            pass
                    else:
                        case_scope.enter_context(exclusive_lock(args.lock_file))
                    check_idle(psutil, args.models_dir.resolve())
                    # A campaign always executes the same bytes it planned.
                    if inventory(output / "source-snapshot") != manifest["source_sha256"]:
                        raise ValueError("Frozen source changed before execution")
                    folder = Path(item["folder"])
                    receipt = bounded(
                        item["command"],
                        folder,
                        cwd=output / "source-snapshot",
                        env=env,
                        timeout=campaign.timeout_seconds,
                        deadline=deadline,
                        minimum_gib=item["minimum_available_gib"],
                        psutil=psutil,
                        script=item["case"]["script"],
                    )
                    manifest["records"].append({"run_id": item["run_id"], **receipt})
                    save(output / "manifest.json", manifest)
                    if receipt["status"] in {"stopped", "not_started", "exception"}:
                        break
        manifest["status"] = (
            "completed"
            if len(manifest["records"]) == len(queue)
            and all(record["status"] == "completed" for record in manifest["records"])
            else "incomplete"
        )
        manifest["wall_seconds"] = time.monotonic() - started
        save(output / "manifest.json", manifest)
        return 0 if manifest["status"] == "completed" else 1
    except (Exception, KeyboardInterrupt) as error:
        if manifest is not None:
            manifest.update(
                status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                reason=f"{type(error).__name__}: {error}",
            )
            save(args.output.absolute() / "manifest.json", manifest)
        print(f"Campaign refused/stopped: {error}", file=sys.stderr)
        return 130 if isinstance(error, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
