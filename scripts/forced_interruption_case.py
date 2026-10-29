"""Launch or verify one exact production-stage interruption acceptance case."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import wave
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from whoopy.audio import AudioManifest, AudioQualityReport
from whoopy.pipeline import RunStatus, RunStore

_HOOK_ENVIRONMENT = (
    "LOCAL_V1_ACCEPTANCE_STAGE_READY_FILE",
    "LOCAL_V1_ACCEPTANCE_PAUSE_STAGE",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("interrupt", "recover"))
    parser.add_argument(
        "--stage",
        choices=("planning", "drafting", "synthesis", "assembly", "encoding", "qc"),
        required=True,
    )
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--runs-dir", type=Path, required=True)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _plan_semantic_digest(path: Path) -> str:
    """Hash reviewed plan content while allowing derived post-draft timing."""

    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise RuntimeError(f"Plan checkpoint is not an object: {path}")
    # Draft completion refines this aggregate from the final timeline. The
    # technique, beats, section budgets, and authored structure stay immutable.
    document.pop("silence_budget_seconds", None)
    sections = document.get("sections")
    if isinstance(sections, list):
        for section in sections:
            if isinstance(section, dict):
                # Validated prose deterministically reallocates section-ending
                # quiet after the drafting checkpoint; it is derived timing,
                # not a mutation of the reviewed technique or structure.
                section.pop("pause_after_ms", None)
    payload = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as output:
            output.write(json.dumps(value, indent=2).encode("utf-8") + b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _state_path(state_dir: Path, stage: str) -> Path:
    return state_dir / f"{stage}.json"


def _interrupt(args: argparse.Namespace) -> int:
    run_id = uuid4()
    state_path = _state_path(args.state_dir, args.stage)
    state_dir = state_path.parent.resolve()
    state_dir.mkdir(parents=True, exist_ok=True)
    script_path: Path | None = None
    command: list[str] = [sys.executable, "-m", "whoopy", "generate"]
    if args.stage in ("planning", "drafting"):
        command.extend(
            [
                "Create a calm one-minute grounding meditation with sparse guidance.",
                "--minutes",
                "1",
                "--meditation-kind",
                "grounding",
                "--profile",
                "standard",
            ]
        )
    else:
        script_path = state_dir / f"{args.stage}-script.md"
        script_path.write_text(
            "[delivery: calm]\nNotice the support beneath you.\n\n"
            "[pause: 3s]\n\nLet your attention return gently to the room.\n",
            encoding="utf-8",
        )
        command.extend(["--script-file", str(script_path), "--profile", "basic"])
    command.extend(
        [
            "--tts-model",
            "kokoro",
            "--run-id",
            str(run_id),
            "--runs-dir",
            str(args.runs_dir.resolve()),
            "--audience",
            "system",
            "--json",
        ]
    )
    _write_json(
        state_path,
        {
            "schema_version": 1,
            "stage": args.stage,
            "run_id": str(run_id),
            "runs_dir": str(args.runs_dir.resolve()),
            "script_path": str(script_path.resolve()) if script_path is not None else None,
            "interrupt_command": command,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    os.execv(sys.executable, command)


def _healthy_artifacts(store: RunStore, run_id: UUID) -> dict[str, str]:
    directory = store.run_directory(run_id)
    healthy: dict[str, str] = {}
    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in {
            "run.json",
            "events.jsonl",
            "worker.log",
            "model-metadata.json",
            "narration.wav",
            "audio-manifest.json",
            "quality.json",
            "worker.lock",
        }:
            continue
        if any(part.startswith(".") for part in path.relative_to(directory).parts):
            continue
        healthy[relative] = _sha256(path)
    workspace = store.root / ".generation-workspaces" / str(run_id)
    if workspace.is_dir():
        for path in sorted(workspace.rglob("*")):
            if path.is_file() and not any(
                part.startswith(".") for part in path.relative_to(workspace).parts
            ):
                relative = path.relative_to(workspace).as_posix()
                digest = _plan_semantic_digest(path) if relative == "plan.json" else _sha256(path)
                healthy[f"generation-workspace/{relative}"] = digest
    return healthy


def _recover(args: argparse.Namespace) -> int:
    state_path = _state_path(args.state_dir, args.stage)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("stage") != args.stage:
        raise RuntimeError("Interruption state does not match the requested stage")
    run_id = UUID(str(state["run_id"]))
    store = RunStore(args.runs_dir)
    interrupted = store.load(run_id)
    if interrupted.status is RunStatus.RUNNING:
        interrupted = store.reconcile_stale_run(
            run_id,
            now=datetime.now(UTC) + timedelta(minutes=1),
        )
    if interrupted.status is not RunStatus.INTERRUPTED:
        raise RuntimeError(
            f"Expected an interrupted durable run before recovery, got {interrupted.status.value}"
        )
    if interrupted.execution is None:
        raise RuntimeError("Interrupted run has no execution evidence")
    expected_stage = {
        "synthesis": "synthesizing",
        "assembly": "assembling",
        "qc": "quality_check",
    }.get(args.stage, args.stage)
    if interrupted.execution.stage.value != expected_stage:
        raise RuntimeError(
            f"Interrupted record is at {interrupted.execution.stage.value}, "
            f"expected {expected_stage}"
        )
    healthy_before = _healthy_artifacts(store, run_id)
    checkpoint_count_before = sum(name.endswith("/checkpoint.json") for name in healthy_before)
    environment = dict(os.environ)
    for name in _HOOK_ENVIRONMENT:
        environment.pop(name, None)
    command = (
        sys.executable,
        "-m",
        "whoopy",
        "run",
        "resume",
        str(run_id),
        "--runs-dir",
        str(args.runs_dir.resolve()),
        "--json",
    )
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=1_800,
    )
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr or completed.stdout)
        return completed.returncode

    record = store.load(run_id)
    if record.status is not RunStatus.COMPLETED:
        raise RuntimeError(f"Recovery ended in {record.status.value}, not completed")
    healthy_after = _healthy_artifacts(store, run_id)
    changed = {
        name: (digest, healthy_after.get(name))
        for name, digest in healthy_before.items()
        if healthy_after.get(name) != digest
    }
    if changed:
        raise RuntimeError(f"Recovery changed healthy durable work: {changed}")
    if args.stage in ("synthesis", "assembly", "encoding", "qc") and checkpoint_count_before < 1:
        raise RuntimeError("Audio-stage interruption preserved no completed speech checkpoint")
    if record.recovery is None or record.recovery.resume_count < 1:
        raise RuntimeError("Completed run did not record a recovery attempt")
    if checkpoint_count_before and record.recovery.checkpoint_reuses < checkpoint_count_before:
        raise RuntimeError(
            "Recovery did not reuse every healthy speech checkpoint: "
            f"before={checkpoint_count_before}, reused={record.recovery.checkpoint_reuses}"
        )

    run_directory = store.run_directory(run_id)
    audio_path = run_directory / "narration.wav"
    manifest = AudioManifest.model_validate_json(
        (run_directory / "audio-manifest.json").read_text(encoding="utf-8")
    )
    quality = AudioQualityReport.model_validate_json(
        (run_directory / "quality.json").read_text(encoding="utf-8")
    )
    with wave.open(str(audio_path), "rb") as reader:
        playable = reader.getframerate() == 24_000 and reader.getnframes() > 0
    if not playable or not quality.passed or manifest.total_frames <= 0:
        raise RuntimeError("Recovered audio did not pass playback and quality verification")

    receipt = {
        "schema_version": 1,
        "verified_at": datetime.now(UTC).isoformat(),
        "stage": args.stage,
        "run_id": str(run_id),
        "interrupted_stage": expected_stage,
        "healthy_artifacts_before": healthy_before,
        "healthy_artifacts_preserved": True,
        "checkpoint_count_before": checkpoint_count_before,
        "checkpoint_reuses": record.recovery.checkpoint_reuses,
        "resume_count": record.recovery.resume_count,
        "completed_audio_sha256": _sha256(audio_path),
        "quality_passed": quality.passed,
    }
    receipt_path = args.state_dir / f"{args.stage}-recovery-evidence.json"
    _write_json(receipt_path, receipt)
    print(receipt_path.resolve())
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return _interrupt(args) if args.mode == "interrupt" else _recover(args)


if __name__ == "__main__":
    raise SystemExit(main())
