"""Compare real local writing methods with identical requests and model settings.

This development-only Mac benchmark uses a shared GPU lock, keeps failed drafts,
and never invents a listening score. It does not synthesize audio or promote a
default. Run from the project root with the ordinary development environment.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psutil

from whoopy.adapters.llm.llama_cpp import LlamaCppScriptGenerator, LlamaCppSettings
from whoopy.artifacts import ArtifactStore, TargetPlatform, load_artifact_lock
from whoopy.meditation import GenerationWorkspace, LocalMeditationGenerator, load_prompt_bundle

CASES = {
    "grounding-3m": (
        180,
        "A seated grounding meditation after an overstimulating workday. Use contact and "
        "ordinary sounds, develop one simple practice, and return gently to the rest of the day.",
    ),
    "focus-no-breath-3m": (
        180,
        "I have an important meeting soon and feel distracted. I am seated. Give me a "
        "steadying practice with sound as the anchor. I do not want to focus on breathing. "
        "Finish ready to continue my day, without promising how I will feel.",
    ),
    "sleep-reflection-5m": (
        300,
        "A gentle good-night reflection after a busy day. I am lying down in bed. "
        "Help me settle, notice my body and reflect on one ordinary moment, with room "
        "for my own answer. End with a sleep taper without waking me up.",
    ),
}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8")


class ResourceGuard:
    """Interrupt this driver so the adapter cleans up its own model children."""

    def __init__(self) -> None:
        self.initial_swap = psutil.swap_memory().used
        self.reason: str | None = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self) -> None:
        low_samples = 0
        while not self.stop.wait(0.5):
            try:
                growth = psutil.swap_memory().used - self.initial_swap
                low_samples = (
                    low_samples + 1 if psutil.virtual_memory().available < 2 * 1024**3 else 0
                )
                if growth > 4 * 1024**3 or low_samples >= 4:
                    self.reason = (
                        f"Resource stop: swap growth {growth} bytes; "
                        f"low-memory samples {low_samples}"
                    )
                    os.kill(os.getpid(), signal.SIGINT)
                    return
            except OSError:
                self.reason = "Resource monitoring unavailable; stopping instead of running blind."
                os.kill(os.getpid(), signal.SIGINT)
                return

    def close(self) -> None:
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", choices=tuple(CASES), action="append")
    parser.add_argument("--method", choices=("plan_first", "recipe_whole_session"), action="append")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--profile", choices=("standard", "lite", "high"), default="standard")
    args = parser.parse_args()
    if args.output.exists() and any(args.output.iterdir()):
        raise SystemExit("Evidence already exists; use a new output directory.")
    args.output.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {
        "created_at": datetime.now(UTC).isoformat(),
        "scope": "same_model_writing_method_comparison_not_voice_quality",
        "human_review": "pending",
        "profile": args.profile,
        "seed": args.seed,
        "results": [],
    }
    failed = False
    for case in args.case or CASES:
        seconds, prompt = CASES[case]
        for method in args.method or ("plan_first", "recipe_whole_session"):
            folder = args.output / case / method
            folder.mkdir(parents=True, exist_ok=True)
            result: dict[str, Any] = {
                "case": case,
                "writing_method": method,
                "prompt": prompt,
                "target_seconds": seconds,
                "seed": args.seed,
                "status": "waiting_for_gpu_lock",
            }
            write_json(folder / "result.json", result)
            lock_start = time.monotonic()
            with Path("/tmp/whoopy-benchmark-gpu.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result["gpu_lock_wait_seconds"] = time.monotonic() - lock_start
                result["status"] = "generating"
                write_json(folder / "result.json", result)
                started = time.monotonic()
                guard = ResourceGuard()
                result["available_memory_before_bytes"] = psutil.virtual_memory().available
                result["swap_before_bytes"] = guard.initial_swap
                minimum = (16 if args.profile == "high" else 8) * 1024**3
                if result["available_memory_before_bytes"] < minimum:
                    result.update(status="failed", error="Memory preflight refused this model.")
                    write_json(folder / "result.json", result)
                    report["results"].append(result)
                    write_json(args.output / "report.json", report)
                    return 1
                guard.thread.start()
                try:
                    adapter = LlamaCppScriptGenerator.from_artifact_store(
                        artifact_lock=load_artifact_lock(Path("config/artifacts.yaml")),
                        store=ArtifactStore(Path("models/managed")),
                        profile_name=args.profile,
                        target=TargetPlatform.current(),
                        device="cpu,metal",
                        settings=LlamaCppSettings(context_tokens=32768, timeout_seconds=300),
                    )
                    generated = LocalMeditationGenerator(
                        adapter,
                        load_prompt_bundle(Path("config/prompts")),
                        articulation_words_per_minute=123,
                        workspace=GenerationWorkspace(folder / "artifacts"),
                    ).generate(
                        prompt=prompt,
                        duration_seconds=seconds,
                        run_id=uuid4(),
                        seed=args.seed,
                        writing_method=method,
                    )
                    result.update(
                        status="generated_pending_content_review",
                        elapsed_seconds=time.monotonic() - started,
                        estimated_duration_seconds=generated.estimated_duration_seconds,
                        sections=len(generated.sections),
                        words=sum(section.word_count for section in generated.sections),
                        attempts=len(generated.raw_attempts),
                        recipe_id=generated.plan.blueprint_id,
                        model=adapter.metadata.model_dump(),
                        script_sha256=hashlib.sha256(generated.script.encode()).hexdigest(),
                        script=str((folder / "artifacts/script.md").resolve()),
                    )
                    # This transcript is for independent ASR, never model prompting.
                    (folder / "spoken.txt").write_text(
                        "\n\n".join(section.text for section in generated.sections) + "\n"
                    )
                except KeyboardInterrupt:
                    result.update(
                        status="failed" if guard.reason else "interrupted",
                        error=guard.reason or "Benchmark interrupted; model cleanup completed.",
                    )
                    write_json(folder / "result.json", result)
                    report["results"].append(result)
                    write_json(args.output / "report.json", report)
                    return 1
                except Exception as error:
                    failed = True
                    result.update(
                        status="failed",
                        elapsed_seconds=time.monotonic() - started,
                        error=f"{type(error).__name__}: {error}",
                    )
                finally:
                    guard.close()
                write_json(folder / "result.json", result)
            report["results"].append(result)
            write_json(args.output / "report.json", report)
            print(case, method, result["status"], flush=True)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
