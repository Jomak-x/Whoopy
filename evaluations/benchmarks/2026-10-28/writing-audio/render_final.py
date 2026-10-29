"""Render final writing benchmark scripts through the production portable TTS path."""

import fcntl
import hashlib
import json
import shutil
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OUT = Path(__file__).resolve().parent
SOURCE = OUT.parent / "writing-v5-high"


def render(case: str, method: str, source_root: Path = SOURCE) -> None:
    source = source_root / case / method
    script = source / "artifacts/script.md"
    spoken = source / "spoken.txt"
    destination = OUT / case / method
    destination.mkdir(parents=True, exist_ok=True)
    if (destination / "result.json").exists():
        prior = json.loads((destination / "result.json").read_text())
        if prior.get("returncode") == 0:
            print(f"Already rendered: {case}/{method}", flush=True)
            return
    shutil.copyfile(script, destination / "script.md")
    shutil.copyfile(spoken, destination / "spoken.txt")
    run_id = str(uuid.uuid4())
    command = [
        str(ROOT / ".venv/bin/python"),
        "-m",
        "whoopy",
        "generate",
        "--script-file",
        str(destination / "script.md"),
        "--profile",
        "basic",
        "--tts-model",
        "kokoro",
        "--voice",
        "am_michael",
        "--speed",
        "0.8",
        "--runs-dir",
        str(destination / "production-runs"),
        "--run-id",
        run_id,
        "--json",
    ]
    result = {
        "case": case,
        "method": method,
        "source_script": str(script),
        "source_spoken": str(spoken),
        "run_id": run_id,
        "command": command,
        "script_sha256": hashlib.sha256(script.read_bytes()).hexdigest(),
        "spoken_sha256": hashlib.sha256(spoken.read_bytes()).hexdigest(),
        "state": "waiting_for_gpu_lock",
    }
    result_path = destination / "result.json"
    result_path.write_text(json.dumps(result, indent=2) + "\n")
    waiting = time.monotonic()
    print(f"Waiting for lock: {case}/{method}", flush=True)
    with Path("/tmp/whoopy-benchmark-gpu.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        result["gpu_lock_wait_seconds"] = time.monotonic() - waiting
        result["state"] = "rendering"
        result_path.write_text(json.dumps(result, indent=2) + "\n")
        started = time.monotonic()
        print(f"Rendering: {case}/{method}", flush=True)
        with (
            (destination / "stdout.json").open("w") as stdout,
            (destination / "stderr.log").open("w") as stderr,
        ):
            process = subprocess.run(command, cwd=ROOT, stdout=stdout, stderr=stderr, check=False)
        result["render_wall_seconds"] = time.monotonic() - started
        result["returncode"] = process.returncode
        result["state"] = "completed" if process.returncode == 0 else "failed"
        result["source_script_unchanged"] = (
            hashlib.sha256(script.read_bytes()).hexdigest() == result["script_sha256"]
        )
        result_path.write_text(json.dumps(result, indent=2) + "\n")
        print(f"Finished: {case}/{method}: rc={process.returncode}", flush=True)


if __name__ == "__main__":
    render("grounding-3m", "plan_first")
    render("grounding-3m", "recipe_whole_session")
    sleep_root = OUT.parent / "writing-v5-high-sleep-retry"
    sleep = sleep_root / "sleep-reflection-5m/recipe_whole_session"
    if (sleep / "artifacts/script.md").exists() and (sleep / "spoken.txt").exists():
        render("sleep-reflection-5m", "recipe_whole_session", sleep_root)
    else:
        print("Sleep recipe script is not ready yet.", flush=True)
