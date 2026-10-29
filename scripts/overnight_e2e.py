#!/usr/bin/env python3
"""Serial proposal: plan by default. --execute is the only inference entry point."""

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
from contextlib import ExitStack, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4

PLAN: dict[str, Any] = {}
GIB = 1024**3


def save(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".pending")
    temporary.write_text(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load_verification_result(report: Path, receipt: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(report.read_text()) if report.is_file() else {}
    returncodes: dict[str | None, int] = {"pass": 0, "review": 2, "fail": 1}
    expected_returncode = returncodes.get(payload.get("status"))
    if (
        expected_returncode is not None
        and receipt.get("status") in {"completed", "nonzero"}
        and receipt.get("returncode") == expected_returncode
    ):
        return payload
    return {
        "status": "fail",
        "failure_reasons": ["Verification did not finish with a terminal result"],
        "receipt": receipt,
        "verification_report": payload,
    }


def screening_status(
    final: dict[str, Any], native_reports: list[dict[str, Any]], required_native: set[str]
) -> str:
    covered = [report["segment_id"] for report in native_reports]
    if len(covered) != len(required_native) or set(covered) != required_native:
        return "fail"
    statuses = [final.get("status"), *[r["verification"].get("status") for r in native_reports]]
    if any(status not in {"pass", "review"} for status in statuses):
        return "fail"
    return "review" if "review" in statuses else "pass"


def freeze_writer_inputs(plan: dict[str, Any], config: Path) -> dict[str, Any]:
    """Freeze an explicit experimental lock without editing source configuration."""
    from whoopy.artifacts import load_artifact_lock

    effective_lock = config / "artifacts.yaml"
    requested_lock = plan.get("writer_artifact_lock")
    original_lock = None
    if requested_lock is not None:
        original_lock = Path(requested_lock).absolute()
        if original_lock.is_symlink() or not original_lock.is_file():
            raise ValueError("writer_artifact_lock must name an existing regular file")
        original_sha256 = digest(original_lock)
        temporary = config / f".writer-artifacts-{uuid4().hex}.yaml"
        try:
            shutil.copyfile(original_lock, temporary)
            if digest(temporary) != original_sha256 or digest(original_lock) != original_sha256:
                raise ValueError("writer_artifact_lock changed while being frozen")
            load_artifact_lock(temporary)
            temporary.replace(effective_lock)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        load_artifact_lock(effective_lock)
    models_dir = Path(plan.get("writer_models_dir", Path(plan["assets"]) / "models/managed"))
    if "writer_models_dir" in plan and not models_dir.is_dir():
        raise ValueError("writer_models_dir must name an existing installed artifact store")
    return {
        "writer_artifact_lock_original_path": str(original_lock) if original_lock else None,
        "writer_artifact_lock_effective_path": str(effective_lock),
        "writer_artifact_lock_sha256": digest(effective_lock),
        "writer_models_dir": str(models_dir.resolve()),
    }


def inventory(root: Path) -> dict[str, dict[str, Any]]:
    return {
        str(p.relative_to(root)): {"bytes": p.stat().st_size, "sha256": digest(p)}
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink() and p.name != "files.json" and p.suffix != ".pending"
    }


def check_idle() -> None:
    """Probe both known cooperative locks before launching a new stage."""
    import fcntl

    for value in (PLAN["locks"]["worker_and_asr_gpu"], PLAN["locks"]["runtime"]):
        path = Path(value)
        if not path.exists():
            continue
        with path.open("rb") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise RuntimeError(f"Another heavyweight process owns {path}") from error
            fcntl.flock(lock, fcntl.LOCK_UN)
    import psutil

    for process in psutil.process_iter(["pid", "cmdline"]):
        try:
            words = process.info["cmdline"] or []
            command = " ".join(words)
            if process.pid != os.getpid() and (
                ("whoopy" in words and "generate" in words)
                or any(
                    Path(word).name
                    in {
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
                    for word in words
                )
            ):
                raise RuntimeError(
                    f"External inference process detected: pid {process.pid}: {command}"
                )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def clean_env(source: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("WHOOPY_")}
    env.update(
        PYTHONPATH=str(source / "src"),
        PYTHONDONTWRITEBYTECODE="1",
        HF_HUB_OFFLINE="1",
        TRANSFORMERS_OFFLINE="1",
        HF_DATASETS_OFFLINE="1",
        HF_HUB_DISABLE_TELEMETRY="1",
        OMNIGENT_DISABLE_KEYRING="1",
    )
    return env


def validated_output(value: Path, source: Path) -> Path:
    """Only the expressly authorized fresh date root is writable inside assets."""
    chosen = value.resolve()
    assets = Path(PLAN["assets"]).resolve()
    dedicated = Path(PLAN["fresh_benchmarks_root"]).absolute()
    # A symlinked date root must not turn the exception into model/recovery writes.
    if dedicated.resolve() != dedicated:
        raise ValueError("Fresh benchmark root must not pass through symlinks")
    allowed_benchmark = dedicated in chosen.parents
    for root in (source.resolve(), Path(PLAN["publication"]).resolve()):
        if (chosen == root or root in chosen.parents) and not (
            root == assets and allowed_benchmark
        ):
            raise ValueError("Output must not be inside an active source/publication checkout")
    if (chosen == assets or assets in chosen.parents) and not allowed_benchmark:
        raise ValueError(
            "Inside original assets, only a fresh child of the authorized date root is allowed"
        )
    return chosen


def create_campaign_directory(output: Path) -> None:
    """Ignore every generated artifact before creating any run evidence."""
    output.mkdir(parents=True, exist_ok=False)
    with (output / ".gitignore").open("x", encoding="utf-8") as stream:
        stream.write("*\n")


def terminate_tree(process: subprocess.Popen[bytes], psutil: Any) -> None:
    """Adapters can create their own sessions; target observed descendants as well."""
    try:
        children = psutil.Process(process.pid).children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []
    for child in reversed(children):
        with suppress(psutil.NoSuchProcess):
            child.send_signal(signal.SIGINT)
    if process.poll() is None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGINT)
            else:
                process.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            pass
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=15)
    for child in reversed(children):
        try:
            if child.is_running():
                child.kill()
        except psutil.NoSuchProcess:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def bounded(
    command: list[str | Path],
    folder: Path,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    minimum_gib: float,
    deadline: float,
    psutil: Any,
    own_gpu: bool = False,
) -> dict[str, Any]:
    """Same conservative thresholds as benchmark_voice_candidates.ResourceWatchdog."""
    folder.mkdir(parents=True, exist_ok=False)
    baseline = psutil.swap_memory().used
    result: dict[str, Any] = {
        "command": [str(x) for x in command],
        "cwd": str(cwd),
        "started_at": datetime.now(UTC).isoformat(),
        "status": "preflight",
        "swap_baseline_bytes": baseline,
        "observations": [],
    }
    save(folder / "receipt.json", result)
    if time.monotonic() >= deadline:
        result.update(status="not_started", reason="overnight deadline reached")
        save(folder / "receipt.json", result)
        return result
    if psutil.virtual_memory().available < minimum_gib * GIB:
        result.update(status="not_started", reason="memory preflight refused")
        save(folder / "receipt.json", result)
        return result
    check_idle()
    gpu: TextIO | None = None
    gpu_scope = ExitStack()
    if own_gpu:
        import fcntl

        # Descriptor spans the subprocess lifecycle; ExitStack closes it in finally.
        gpu = gpu_scope.enter_context(Path(PLAN["locks"]["worker_and_asr_gpu"]).open("a"))  # noqa: SIM115
        fcntl.flock(gpu, fcntl.LOCK_EX | fcntl.LOCK_NB)
    started = time.monotonic()
    low_since: float | None = None
    process: subprocess.Popen[bytes] | None = None
    try:
        with (folder / "stdout.log").open("x") as out, (folder / "stderr.log").open("x") as err:
            process = subprocess.Popen(
                [str(x) for x in command],
                cwd=cwd,
                env=env,
                stdout=out,
                stderr=err,
                start_new_session=os.name == "posix",
            )
            result.update(status="running", pid=process.pid)
            save(folder / "receipt.json", result)
            with (folder / "resources.jsonl").open("x", buffering=1) as samples:
                while process.poll() is None:
                    now = time.monotonic()
                    available, swap = psutil.virtual_memory().available, psutil.swap_memory().used
                    rss: int | None = 0
                    try:
                        root = psutil.Process(process.pid)
                        for current in [root, *root.children(recursive=True)]:
                            with suppress(psutil.NoSuchProcess, psutil.ZombieProcess):
                                rss = (rss or 0) + current.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied) as error:
                        rss = None
                        result["rss_unavailable_reason"] = f"{type(error).__name__}: {error}"
                    sample = {
                        "time": time.time(),
                        "elapsed_seconds": now - started,
                        "available_bytes": available,
                        "swap_used_bytes": swap,
                        "process_tree_rss_bytes": rss,
                    }
                    samples.write(json.dumps(sample) + "\n")
                    low_since = (low_since or now) if available < 2 * GIB else None
                    reason = (
                        "swap growth exceeded 4 GiB"
                        if swap - baseline > 4 * GIB
                        else "available RAM below 2 GiB for two seconds"
                        if low_since and now - low_since >= 2
                        else "stage deadline"
                        if now - started > timeout
                        else "overnight deadline"
                        if now >= deadline
                        else None
                    )
                    if reason:
                        result.update(status="stopped", reason=reason)
                        save(folder / "receipt.json", result)
                        terminate_tree(process, psutil)
                        break
                    time.sleep(0.5)
            if result["status"] == "running":
                result["status"] = "completed" if process.returncode == 0 else "nonzero"
            result["returncode"] = process.returncode
        final_available, final_swap = psutil.virtual_memory().available, psutil.swap_memory().used
        result["final_system"] = {"available_bytes": final_available, "swap_used_bytes": final_swap}
        stderr = (folder / "stderr.log").read_text(errors="replace")
        if result["status"] != "stopped" and "MLX resource watchdog:" in stderr:
            result.update(status="stopped", reason="native worker resource watchdog stopped")
        elif result["status"] != "stopped" and (
            final_available < 2 * GIB or final_swap - baseline > 4 * GIB
        ):
            result.update(status="stopped", reason="system pressure at stage exit")
    except BaseException as error:
        result.update(status="exception", reason=f"{type(error).__name__}: {error}")
        if process is not None:
            terminate_tree(process, psutil)
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
    finally:
        result["wall_seconds"] = time.monotonic() - started
        save(folder / "receipt.json", result)
        if gpu is not None:
            import fcntl

            fcntl.flock(gpu, fcntl.LOCK_UN)
        gpu_scope.close()
    return result


def read_expected(run: Path, comparison: list[str] | None = None) -> list[str]:
    timeline = json.loads((run / "timeline.json").read_text())
    texts: list[str] = [s["text"] for s in timeline["segments"] if s["type"] == "SPEECH"]
    if not texts or any("[pause:" in t or "[delivery:" in t or "[pace:" in t for t in texts):
        raise ValueError("Final typed speech missing or contains control markup")
    if comparison is not None and texts != comparison:
        raise ValueError("Voice comparison changed final typed spoken text")
    (run / "expected-spoken.txt").write_text("\n\n".join(texts) + "\n")
    return texts


def selected_native_evidence(
    run: Path, delivered: dict[str, Any], speech: dict[str, Any]
) -> tuple[Path, Path, str]:
    """Bind each delivery to verified synthesis inputs, allowing cached repetitions."""
    from whoopy.adapters.tts.mlx_qwen3_tts import valid_cached_native_evidence
    from whoopy.audio.processing import SpeechProcessingSettings, processed_speech_metadata
    from whoopy.audio.synthesis import cache_key_for_identity
    from whoopy.ports import AdapterMetadata
    from whoopy.timeline import SpeechSegment

    native_root = run / "native-speech"
    key = delivered["cache_key"]
    # Validate before using a manifest value in a filesystem path.
    if not isinstance(key, str) or len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
        raise ValueError("Delivered native cache key is invalid")
    reference = json.loads((native_root / "cache-references" / f"{key}.json").read_text())
    typed = SpeechSegment.model_validate(speech)
    original = SpeechSegment.model_validate(reference["segment"])
    adapter = AdapterMetadata.model_validate(reference["adapter"])
    processing = SpeechProcessingSettings(**reference["processing"])
    processed = processed_speech_metadata(
        adapter, inner_cache_identity=adapter.cache_identity, settings=processing
    )
    actual_key = cache_key_for_identity(
        typed, synthesizer_identity=processed.cache_identity, sample_rate=24000
    )
    if delivered["segment_id"] != typed.id or actual_key != key:
        raise ValueError("Native reference does not match final typed synthesis inputs")
    if not valid_cached_native_evidence(native_root, key, delivered["pcm_sha256"]):
        raise ValueError("Native-to-processed evidence does not verify against delivered PCM")
    evidence = reference["native"]
    wav, sidecar = Path(evidence["wav_path"]), Path(evidence["sidecar_path"])
    metadata = json.loads(sidecar.read_text())
    # Native IDs describe the inference attempt; delivery IDs describe timeline uses.
    if (
        reference["segment_id"] != original.id
        or reference["text"] != original.text
        or metadata["segment_id"] != original.id
        or metadata["text"] != original.text
    ):
        raise ValueError("Native attempt does not match its recorded synthesis input")
    return wav, sidecar, original.id


def main() -> int:
    global PLAN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="Reviewed JSON benchmark plan.")
    parser.add_argument("--source", type=Path, help="Override the plan's source checkout.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--include-sleep", action="store_true")
    args = parser.parse_args()
    PLAN = json.loads(args.plan.read_text())
    selected_source = args.source or Path(PLAN["source"])
    if not args.execute:
        print(
            json.dumps(
                {**PLAN, "source": str(selected_source), "include_sleep": args.include_sleep},
                indent=2,
            )
        )
        return 0
    if args.output is None:
        parser.error("--execute requires a fresh --output directory")
    if sys.platform != "darwin":
        parser.error(
            "Live MLX benchmark execution requires macOS; plan/report/stub checks are portable"
        )
    # Import only for execute, so printing a plan cannot trigger optional runtime imports.
    import fcntl

    import psutil

    source = selected_source.resolve(strict=True)
    try:
        output = validated_output(args.output, source)
    except ValueError as error:
        parser.error(str(error))
    create_campaign_directory(output)
    owner_scope = ExitStack()
    # Descriptor spans the whole campaign; ExitStack closes it in finally.
    owner = owner_scope.enter_context(Path(PLAN["locks"]["runner_owner"]).open("a"))  # noqa: SIM115
    fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
    env, python = clean_env(source), Path(PLAN["python"])
    deadline = time.monotonic() + PLAN["limits"]["total_hours"] * 3600
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "preflight",
        "started_at": datetime.now(UTC).isoformat(),
        "plan": PLAN,
        "source": str(source),
        "output": str(output),
        "records": [],
    }
    save(output / "manifest.json", manifest)
    input_source = source
    snapshot = output / "source-snapshot"
    snapshot.mkdir()
    for tree in ("src", "scripts", "config"):
        shutil.copytree(
            source / tree,
            snapshot / tree,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "local.yaml"),
        )
    shutil.copy2(source / "pyproject.toml", snapshot / "pyproject.toml")
    source = snapshot
    env = clean_env(source)
    manifest["source_snapshot"] = str(snapshot)
    snapshot_files = [path for path in snapshot.rglob("*") if path.is_file()]
    manifest["source_snapshot_file_count"] = len(snapshot_files)
    manifest["source_snapshot_bytes"] = sum(path.stat().st_size for path in snapshot_files)
    config = output / "config"
    shutil.copytree(source / "config", config, ignore=shutil.ignore_patterns("local.yaml"))
    try:
        manifest["writer_inputs"] = freeze_writer_inputs(PLAN, config)
        effective_models_dir = Path(manifest["writer_inputs"]["writer_models_dir"])
        save(output / "manifest.json", manifest)
        help_result = subprocess.run(
            [str(python), "-m", "whoopy", "generate", "--help"],
            cwd=output,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        (output / "generate-help.txt").write_text(help_result.stdout + help_result.stderr)
        if help_result.returncode or any(
            PLAN[key] not in help_result.stdout
            for key in ("experimental_backend", "experimental_voice_design_backend")
        ):
            raise RuntimeError(
                "Future-only experimental backend is not integrated in selected source"
            )
        for path in (Path(PLAN["asr_python"]), Path(PLAN["asr_model"]) / "weights.npz"):
            if not path.is_file():
                raise RuntimeError(f"Required existing offline verification asset missing: {path}")
        if shutil.disk_usage(output).free < 20 * GIB:
            raise RuntimeError("Disk preflight requires 20 GiB free")
        check_idle()
        manifest["source_hashes"] = {
            str(p.relative_to(source)): digest(p)
            for tree in (source / "src", source / "scripts", source / "config")
            for p in sorted(tree.rglob("*"))
            if p.is_file() and p.suffix in {".py", ".yaml", ".md"}
        }
        for label, command in (
            ("git-head", ["git", "rev-parse", "HEAD"]),
            ("git-status", ["git", "status", "--short"]),
            ("git-diff", ["git", "diff", "--binary", "HEAD"]),
        ):
            result = subprocess.run(
                command, cwd=input_source, capture_output=True, text=True, check=True
            )
            (output / f"{label}.txt").write_text(result.stdout)
        manifest["asr_weights_sha256"] = digest(Path(PLAN["asr_model"]) / "weights.npz")
        manifest["asr_config_sha256"] = digest(Path(PLAN["asr_model"]) / "config.json")
        manifest["runner_sha256"] = digest(Path(__file__))
        manifest["status"] = "running"
        save(output / "manifest.json", manifest)

        def verify(audio: Path, expected: Path, folder: Path, mode: str) -> dict[str, Any]:
            report = folder.parent / (folder.name + ".json")
            command: list[str | Path] = [
                PLAN["asr_python"],
                source / "scripts/verify_benchmark_audio.py",
                audio,
                "--expected-file",
                expected,
                "--report",
                report,
                "--mode",
                mode,
                "--asr-python",
                PLAN["asr_python"],
                "--model",
                PLAN["asr_model"],
                "--timeout",
                "900",
            ]
            receipt = bounded(
                command,
                folder,
                cwd=output,
                env=env,
                timeout=930,
                minimum_gib=4,
                deadline=deadline,
                psutil=psutil,
            )
            if receipt.get("status") in {"stopped", "exception", "not_started"}:
                manifest["stop_queue_reason"] = receipt.get("reason", "verification resource stop")
            return load_verification_result(report, receipt)

        def generate(
            case: dict[str, Any],
            candidate: str,
            accepted_script: Path | None = None,
            original_texts: list[str] | None = None,
        ) -> dict[str, Any]:
            attempt = output / case["id"] / f"{candidate}-{uuid4()}"
            attempt.mkdir(parents=True, exist_ok=False)
            run_id = str(uuid4())
            runs = attempt / "production-runs"
            run = runs / run_id
            kokoro = candidate.startswith("kokoro")
            design = candidate.startswith("VoiceDesign-")
            speaker = "Ryan" if candidate == "writer-Ryan" else candidate
            command: list[str | Path] = [python, "-m", "whoopy", "generate"]
            command += ["--script-file", accepted_script] if accepted_script else [case["prompt"]]
            command += [
                "--minutes",
                str(case["minutes"]),
                "--meditation-kind",
                case["kind"],
                "--profile",
                "basic" if accepted_script else "high",
                "--seed",
                "42",
                "--writing-method",
                "recipe_whole_session",
                "--parallel-sections",
                "1",
                "--run-id",
                run_id,
                "--runs-dir",
                runs,
                "--config-dir",
                config,
                "--artifact-lock",
                config / "artifacts.yaml",
                "--models-dir",
                effective_models_dir,
                "--tts-model",
                "kokoro"
                if kokoro
                else PLAN["experimental_voice_design_backend"]
                if design
                else PLAN["experimental_backend"],
                "--pace",
                "slow",
                "--audience",
                "lab",
                "--json",
            ]
            command += (
                ["--voice", "am_michael", "--speed", str(PLAN["speech"]["kokoro_speed"])]
                if kokoro
                else [
                    "--qwen-voice-design-preset",
                    PLAN["voice_design_preset"],
                    "--qwen-language",
                    "English",
                ]
                if design
                else ["--qwen-speaker", speaker, "--qwen-language", "English"]
            )
            record: dict[str, Any] = {
                "case": case["id"],
                "candidate": candidate,
                "run_id": run_id,
                "attempt": str(attempt),
                "status": "starting",
                "human_review": "pending",
                "mode": "fixed_script_voice_comparison" if accepted_script else "prompt_to_audio",
                "comparison_script": str(accepted_script) if accepted_script else None,
            }
            manifest["records"].append(record)
            save(output / "manifest.json", manifest)
            receipt = bounded(
                command,
                attempt / "generation",
                cwd=output,
                env=env,
                timeout=1800,
                minimum_gib=4 if accepted_script else 16,
                deadline=deadline,
                psutil=psutil,
                own_gpu=kokoro,
            )
            record["generation"] = receipt
            if receipt.get("status") in {"stopped", "exception", "not_started"}:
                manifest["stop_queue_reason"] = receipt.get("reason", "generation resource stop")
            try:
                durable = json.loads((run / "run.json").read_text())
                record["durable_status"] = durable["status"]
                if receipt.get("returncode") != 0 or durable["status"] != "completed":
                    raise ValueError("Generation did not complete; retaining raw failed attempts")
                texts = read_expected(run, original_texts)
                expected = run / "expected-spoken.txt"
                record["script_sha256"] = digest(run / "script.md")
                record["timeline_sha256"] = digest(run / "timeline.json")
                record["resolved_config"] = json.loads((run / "resolved-config.json").read_text())
                record["model_metadata"] = json.loads((run / "model-metadata.json").read_text())
                record["quality"] = json.loads((run / "quality.json").read_text())
                record["target_seconds"] = case["minutes"] * 60
                record["actual_seconds"] = record["quality"]["duration_ms"] / 1000
                if not record["quality"]["passed"]:
                    raise ValueError("Durable audio QC failed")
                record["final_verification"] = verify(
                    run / "narration.wav", expected, attempt / "verify-final", "narration"
                )
                if manifest.get("stop_queue_reason"):
                    raise ValueError("Queue stopped during narration verification")
                native = sorted((run / "native-speech").glob("*/metadata.json"))
                if not kokoro and not native:
                    raise ValueError("Required native FLOAT WAV/EOS evidence missing")
                record["native_metadata"] = [str(p) for p in native]
                record["native_attempts"] = []
                for path in native:
                    metadata = json.loads(path.read_text())
                    record["native_attempts"].append(
                        {
                            "metadata": str(path),
                            **{
                                k: metadata.get(k)
                                for k in (
                                    "attempt_id",
                                    "segment_id",
                                    "eos_observed",
                                    "stop_evidence",
                                    "inference_seconds",
                                )
                            },
                        }
                    )
                native_reports: list[dict[str, Any]] = []
                required_native: set[str] = set()
                record["native_verification"] = native_reports
                if not kokoro:
                    timeline = json.loads((run / "timeline.json").read_text())
                    spoken = {s["id"]: s for s in timeline["segments"] if s["type"] == "SPEECH"}
                    audio_manifest = json.loads((run / "audio-manifest.json").read_text())
                    delivered = [
                        s for s in audio_manifest["segments"] if s["segment_type"] == "SPEECH"
                    ]
                    if {s["segment_id"] for s in delivered} != set(spoken):
                        raise ValueError("Audio manifest does not cover every typed speech segment")
                    required_native = set(spoken)
                    for index, segment in enumerate(delivered):
                        receipt_path = (
                            run / "native-speech/cache-references" / f"{segment['cache_key']}.json"
                        )
                        text = spoken[segment["segment_id"]]["text"]
                        wav, sidecar, native_segment_id = selected_native_evidence(
                            run, segment, spoken[segment["segment_id"]]
                        )
                        expected_native = attempt / f"expected-native-{index:03d}.txt"
                        expected_native.write_text(text + "\n")
                        report = verify(
                            wav, expected_native, attempt / f"verify-native-{index:03d}", "speech"
                        )
                        native_reports.append(
                            {
                                "segment_id": segment["segment_id"],
                                "native_segment_id": native_segment_id,
                                "selected_reference": str(receipt_path),
                                "metadata": str(sidecar),
                                "verification": report,
                                "report": str(attempt / f"verify-native-{index:03d}.json"),
                            }
                        )
                        if manifest.get("stop_queue_reason"):
                            break
                record["status"] = screening_status(
                    record["final_verification"], native_reports, required_native
                )
                record["native_evidence_scope"] = (
                    "native FLOAT with selected cache mapping"
                    if not kokoro
                    else "portable adapter has no native FLOAT export; "
                    "retained final WAV and cache PCM are processed"
                )
                record["accepted_script"] = str(run / "script.md")
                record["texts"] = texts
            except Exception as error:
                record.update(status="fail", error=f"{type(error).__name__}: {error}")
            finally:
                save(attempt / "files.json", inventory(attempt))
                save(output / "manifest.json", manifest)
            return record

        def comparisons(case: dict[str, Any], initial: dict[str, Any]) -> None:
            if "accepted_script" not in initial or manifest.get("stop_queue_reason"):
                return
            for candidate in PLAN["comparison_voices"]:
                if manifest.get("stop_queue_reason"):
                    break
                if (
                    candidate.startswith("VoiceDesign-")
                    and case["id"] not in PLAN["voice_design_comparison_cases"]
                ):
                    continue
                if time.monotonic() >= deadline:
                    manifest.setdefault("skipped", []).append(
                        {"case": case["id"], "candidate": candidate, "reason": "overnight deadline"}
                    )
                    continue
                generate(case, candidate, Path(initial["accepted_script"]), initial["texts"])

        initials: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for case in [c for c in PLAN["cases"] if not c["optional"]]:
            if manifest.get("stop_queue_reason"):
                break
            if time.monotonic() >= deadline:
                manifest.setdefault("skipped", []).append(
                    {"case": case["id"], "reason": "overnight deadline"}
                )
                continue
            initials.append((case, generate(case, "writer-Ryan")))
        for case, initial in initials:
            comparisons(case, initial)
        for case in [c for c in PLAN["cases"] if c["optional"]]:
            if (
                not args.include_sleep
                or manifest.get("stop_queue_reason")
                or len(initials) < 2
                or any(r["status"] == "fail" for r in manifest["records"])
            ):
                manifest.setdefault("skipped", []).append(
                    {"case": case["id"], "reason": "optional or earlier hard failure"}
                )
                continue
            if time.monotonic() >= deadline:
                manifest.setdefault("skipped", []).append(
                    {"case": case["id"], "reason": "overnight deadline"}
                )
                continue
            comparisons(case, generate(case, "writer-Ryan"))
        manifest["status"] = (
            "stopped"
            if manifest.get("stop_queue_reason")
            else "incomplete"
            if any(r["reason"] == "overnight deadline" for r in manifest.get("skipped", []))
            else "screened_pending_human_listening"
        )
    except BaseException as error:
        manifest.update(status="stopped", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        manifest["finished_at"] = datetime.now(UTC).isoformat()
        save(output / "manifest.json", manifest)
        save(output / "files.json", inventory(output))
        fcntl.flock(owner, fcntl.LOCK_UN)
        owner_scope.close()
    if manifest["status"] in {"stopped", "incomplete"} or any(
        r["status"] == "fail" for r in manifest["records"]
    ):
        return 1
    return 2 if any(r["status"] == "review" for r in manifest["records"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
