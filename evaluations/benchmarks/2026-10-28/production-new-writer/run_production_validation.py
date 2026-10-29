"""One evidence-only production CLI run, serialized with all local model work."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

import psutil

# The evidence driver lives below the project root; share the existing guard.
ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

FOLDER = Path(__file__).resolve().parent
RUNS = FOLDER / "runs"
PROMPT = (
    "A seated grounding meditation after an overstimulating workday. Use contact and "
    "ordinary sounds, develop one simple practice, and return gently to the rest of the day."
)
COMMAND = [
    str(ROOT / ".venv/bin/python"),
    "-m",
    "whoopy",
    "generate",
    PROMPT,
    "--minutes",
    "3",
    "--voice",
    "am_michael",
    "--tts-model",
    "kokoro",
    "--writing-method",
    "recipe_whole_session",
    "--profile",
    "high",
    "--json",
    "--runs-dir",
    str(RUNS),
]


def save(value: dict[str, object]) -> None:
    (FOLDER / "result.json").write_text(json.dumps(value, indent=2, default=str) + "\n")


def cleanup_group(process: subprocess.Popen[bytes]) -> None:
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        process.wait(timeout=10)
    with suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=10)


def main() -> int:
    from scripts.benchmark_writing_methods import ResourceGuard

    result: dict[str, object] = {
        "created_at": datetime.now(UTC).isoformat(),
        "command": COMMAND,
        "scope": "production_prompt_to_script_to_kokoro_end_to_end",
        "requested_seconds": 180,
        "human_listening_review": "pending",
        "status": "waiting_for_shared_gpu_lock",
        "source_edits": False,
    }
    save(result)
    print("Waiting for shared GPU lock.", flush=True)
    start = time.monotonic()
    with Path("/tmp/whoopy-benchmark-gpu.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result["gpu_lock_wait_seconds"] = time.monotonic() - start
        result["available_memory_before_bytes"] = psutil.virtual_memory().available
        try:
            guard = ResourceGuard()
        except OSError as error:
            result.update(
                status="failed_resource_monitor_before_cli",
                error=f"{type(error).__name__} from ResourceGuard memory monitoring: {error}",
                cli_launched=False,
            )
            save(result)
            return 2
        result["swap_before_bytes"] = guard.initial_swap
        if psutil.virtual_memory().available < 16 * 1024**3:
            result.update(
                status="refused_memory_preflight", error="Need at least 16 GiB available."
            )
            save(result)
            print("Memory preflight refused; no CLI inference launched.", flush=True)
            return 2
        result["status"] = "running_production_cli"
        save(result)
        print("GPU lock acquired; memory preflight passed; launching production CLI.", flush=True)
        process = None
        began = time.monotonic()
        guard.thread.start()
        try:
            with (
                (FOLDER / "stdout.json").open("wb") as stdout,
                (FOLDER / "stderr.log").open("wb") as stderr,
            ):
                process = subprocess.Popen(
                    COMMAND, cwd=ROOT, stdout=stdout, stderr=stderr, start_new_session=True
                )
                result["cli_pid"] = process.pid
                save(result)
                code = process.wait(timeout=900)
                result.update(status="completed" if code == 0 else "failed", returncode=code)
        except KeyboardInterrupt:
            result.update(
                status="stopped_by_resource_guard" if guard.reason else "interrupted",
                error=guard.reason or "Driver interrupted.",
            )
            if process is not None:
                cleanup_group(process)
        except subprocess.TimeoutExpired:
            result.update(
                status="stopped_by_timeout", error="Production CLI exceeded 900-second limit."
            )
            if process is not None:
                cleanup_group(process)
        except BaseException as error:
            result.update(status="failed_driver", error=f"{type(error).__name__}: {error}")
            if process is not None:
                cleanup_group(process)
        finally:
            guard.close()
            result["elapsed_seconds"] = time.monotonic() - began
            result["available_memory_after_bytes"] = psutil.virtual_memory().available
            result["swap_after_bytes"] = psutil.swap_memory().used
            result["resource_guard_reason"] = guard.reason
            save(result)
            print(json.dumps(result, default=str), flush=True)
    return 0 if result["status"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
