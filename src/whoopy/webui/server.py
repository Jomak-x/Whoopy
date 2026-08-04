"""Dependency-free local HTTP server for Whoopy's browser tester.

The web layer deliberately starts the existing CLI in a child process. That
keeps the CLI as the single generation entry point and prevents the tester from
growing a second orchestration path with different validation or recovery rules.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import signal
import socket
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import import_module
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

from whoopy.adapters.tts import MOSS_LANGUAGES
from whoopy.artifacts import (
    ArtifactError,
    ArtifactState,
    ArtifactStore,
    TargetPlatform,
    load_artifact_lock,
)
from whoopy.hardware import diagnose, inspect_hardware, load_runtime_profiles
from whoopy.model_packs.resolution import (
    OptionalTTSBackend,
    models_root_from_artifact_store,
    resolve_tts_model_pack,
)
from whoopy.pipeline import RunExecution, RunRecord, RunStage, RunStatus, RunStore
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.pipeline.runs import RunNotFoundError, RunStoreError
from whoopy.timeline import SpeechSegment
from whoopy.voices import KOKORO_ENGLISH_VOICES

MAX_REQUEST_BYTES = 64_000
MAX_RECENT_RUNS = 24
PROCESS_STOP_TIMEOUT_SECONDS = 2.0
SEGMENT_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MODEL_PACK_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{0,127}$")
ALLOWED_ARTIFACTS = {
    "run": "run.json",
    "script": "script.md",
    "plan": "plan.json",
    "timeline": "timeline.json",
    "quality": "quality.json",
    "manifest": "audio-manifest.json",
    "models": "model-metadata.json",
}


def _model_pack_service(registry_path: Path, models_root: Path) -> Any:
    """Build the optional-pack facade only when a pack endpoint is used."""

    manager_type = import_module("whoopy.model_packs").ManagedModelPacks
    return manager_type.from_paths(
        registry_path=registry_path,
        models_root=models_root,
    )


def _model_pack_payload(value: Any) -> dict[str, Any]:
    """Normalize a typed pack report for the dependency-free HTTP server."""

    model_dump = getattr(value, "model_dump", None)
    payload = model_dump(mode="json") if callable(model_dump) else value
    if not isinstance(payload, dict):
        raise ValueError("Model-pack operations must return a JSON object.")
    return cast(dict[str, Any], payload)


@dataclass
class GenerationTask:
    """Ephemeral process handle for a durable browser-initiated run.

    This deliberately does *not* own status or progress.  Those values live in
    ``run.json`` so a restarted web server can still show the real run state.
    """

    task_id: str
    run_id: str
    mode: str | None = None
    process: Any | None = None
    launching: bool = True
    cancel_requested: bool = False

    @property
    def active(self) -> bool:
        """Include the pre-Popen window in duplicate/cancel decisions."""

        return self.launching or (self.process is not None and self.process.poll() is None)


class LocalWebApplication:
    """Own local paths, task processes, and inspectable run history."""

    def __init__(
        self,
        *,
        project_root: Path,
        config_directory: Path,
        models_directory: Path,
        runs_directory: Path,
        command_launcher: Callable[..., Any] | None = None,
        model_pack_service_factory: Callable[[Path, Path], Any] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config_directory = self._resolve(config_directory)
        self.models_directory = self._resolve(models_directory)
        self.model_pack_models_root = models_root_from_artifact_store(self.models_directory)
        self.runs_directory = self._resolve(runs_directory)
        self._tasks: dict[str, GenerationTask] = {}
        self._task_lock = threading.Lock()
        # Injection keeps web tests independent from a shell child process.
        self._command_launcher: Callable[..., Any] = command_launcher or subprocess.Popen
        # The web layer deliberately receives an already-composed service. It
        # never imports a TTS runtime or reconstructs pack file paths itself.
        self._model_pack_service_factory = model_pack_service_factory or _model_pack_service

    def _resolve(self, path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (self.project_root / path).resolve()

    def list_model_packs(self) -> dict[str, Any]:
        """Return declarative pack readiness without loading a voice model."""

        return _model_pack_payload(
            self._model_pack_service_factory(
                self.config_directory / "model_packs.yaml", self.model_pack_models_root
            ).list()
        )

    def model_pack_action(
        self,
        action: str,
        pack_id: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Run one allow-listed pack operation from the loopback Studio.

        Pack IDs are declarations, not paths. This is the same safety boundary
        used by the terminal: the browser cannot point removal or installation
        at an arbitrary location on the laptop.
        """

        service = self._model_pack_service_factory(
            self.config_directory / "model_packs.yaml", self.model_pack_models_root
        )
        if action in {"install", "verify", "smoke-test", "remove"} and (
            pack_id is None or not MODEL_PACK_ID_PATTERN.fullmatch(pack_id)
        ):
            raise ValueError("Choose a declared model pack.")
        if action == "install":
            allow_network = payload.get("allow_network", False)
            if not isinstance(allow_network, bool):
                raise ValueError("allow_network must be a boolean.")
            if set(payload) - {"allow_network"}:
                raise ValueError("Install accepts only allow_network.")
            result = service.install(pack_id, offline_directory=None, allow_network=allow_network)
        elif action == "verify":
            if payload:
                raise ValueError("Verify does not accept request fields.")
            result = service.verify(pack_id)
        elif action == "smoke-test":
            if payload:
                raise ValueError("Smoke test does not accept request fields.")
            result = service.smoke_test(pack_id)
        elif action == "unload":
            if payload:
                raise ValueError("Unload does not accept request fields.")
            result = service.unload()
        elif action == "remove":
            if set(payload) != {"confirm"} or payload["confirm"] is not True:
                raise ValueError('Remove requires exactly {"confirm": true}.')
            result = service.remove(pack_id, confirmed=True)
        elif action == "restore":
            receipt_id = payload.get("receipt_id")
            if (
                not isinstance(receipt_id, str)
                or not receipt_id.strip()
                or set(payload) != {"receipt_id"}
            ):
                raise ValueError("Restore requires exactly one non-empty receipt_id.")
            result = service.restore(receipt_id)
        else:
            raise ValueError("Unknown model-pack action.")
        return _model_pack_payload(result)

    def environment_status(self) -> dict[str, Any]:
        """Report compatibility and downloads without loading any model."""

        try:
            inspection_path = self.models_directory
            while not inspection_path.exists() and inspection_path != inspection_path.parent:
                inspection_path = inspection_path.parent
            snapshot = inspect_hardware(inspection_path)
            target = TargetPlatform(
                operating_system=snapshot.operating_system,
                architecture=snapshot.architecture,
            )
            profiles = load_runtime_profiles(self.config_directory / "runtime_profiles.yaml")
            lock = load_artifact_lock(self.config_directory / "artifacts.yaml")
            store = ArtifactStore(self.models_directory)
            profile_reports: dict[str, Any] = {}
            for profile_name in ("basic", "standard"):
                diagnosis = diagnose(snapshot, profiles, profile_name)
                artifacts = lock.resolve(profile_name, target)
                states = [store.inspect(artifact).state for artifact in artifacts]
                profile_reports[profile_name] = {
                    "compatible": diagnosis.supported,
                    "installed": bool(states)
                    and all(state is ArtifactState.INSTALLED for state in states),
                    "messages": diagnosis.messages,
                    "artifact_count": len(states),
                }
            pack_report = self.list_model_packs()
            pack_statuses = {
                item["pack_id"]: item
                for item in pack_report.get("packs", [])
                if isinstance(item, dict) and isinstance(item.get("pack_id"), str)
            }

            def optional_status(pack_id: str) -> dict[str, Any]:
                status = pack_statuses.get(pack_id, {})
                state = status.get("state", "missing")
                failed_checks = [
                    check.get("message")
                    for check in status.get("checks", [])
                    if isinstance(check, dict) and check.get("passed") is False
                ]
                return {
                    "ready": state == "ready",
                    "error": (
                        None if state == "ready" else (failed_checks[0] if failed_checks else state)
                    ),
                }

            return {
                "ok": True,
                "system": f"{target.operating_system} {target.architecture}",
                "profiles": profile_reports,
                "privacy": "Models and generation stay on this laptop.",
                "speech_models": {
                    "kokoro": {
                        **optional_status("kokoro"),
                        "license": "Apache-2.0",
                        "expression": "voice preset",
                    },
                    "fish-1.4": {
                        **optional_status("fish-speech-1.4"),
                        "license": "CC-BY-NC-SA-4.0",
                        "expression": "reference voice; no bracket tags",
                    },
                    "fish-s2": {
                        "ready": False,
                        "license": "Fish Audio Research License",
                        "expression": "[emotion] tags",
                        "error": "requires a much larger Linux/WSL GPU setup",
                    },
                    **{
                        model: {
                            **optional_status(pack_id),
                            "license": "Apache-2.0",
                            "expression": "language + free-form delivery instruction",
                        }
                        for model, pack_id in {
                            "moss-local-v1.5": "moss-local-5b",
                            "moss-v1.5": "moss-8b",
                        }.items()
                    },
                },
            }
        except (ArtifactError, OSError, ValueError) as error:
            return {"ok": False, "error": str(error), "profiles": {}}

    def list_runs(self) -> list[dict[str, Any]]:
        """Load the newest valid records while ignoring caches and partial files."""

        if not self.runs_directory.is_dir():
            return []
        records: list[tuple[RunRecord, Path]] = []
        for directory in self.runs_directory.iterdir():
            if not directory.is_dir():
                continue
            try:
                UUID(directory.name)
                record = RunRecord.model_validate_json(
                    (directory / "run.json").read_text(encoding="utf-8")
                )
            except (FileNotFoundError, OSError, ValueError):
                continue
            records.append((record, directory))
        records.sort(key=lambda item: item[0].updated_at, reverse=True)
        return [
            self._run_summary(record, directory) for record, directory in records[:MAX_RECENT_RUNS]
        ]

    @staticmethod
    def _run_summary(record: RunRecord, directory: Path) -> dict[str, Any]:
        duration_seconds: float | None = None
        quality_passed: bool | None = None
        tts_model: str | None = None
        voice: str | None = None
        execution = record.execution
        try:
            manifest = json.loads((directory / "audio-manifest.json").read_text(encoding="utf-8"))
            duration_seconds = round(float(manifest["duration_ms"]) / 1_000, 2)
        except (FileNotFoundError, OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            pass
        try:
            quality = json.loads((directory / "quality.json").read_text(encoding="utf-8"))
            quality_passed = bool(quality["passed"])
        except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError):
            pass
        try:
            resolved = json.loads((directory / "resolved-config.json").read_text(encoding="utf-8"))
            tts_model = str(resolved["tts"].get("backend", "kokoro"))
            voice = str(resolved["tts"]["voice_name"])
        except (FileNotFoundError, OSError, KeyError, TypeError, json.JSONDecodeError):
            pass
        return {
            "run_id": str(record.run_id),
            "status": record.status.value,
            "source_kind": record.source_kind,
            "title": record.prompt,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "duration_seconds": duration_seconds,
            "quality_passed": quality_passed,
            "tts_model": tts_model,
            "voice": voice,
            "has_audio": record.audio_artifact is not None
            and (directory / "narration.wav").is_file(),
            "error": record.error,
            "recovery": (
                record.recovery.model_dump(mode="json") if record.recovery is not None else None
            ),
            # Lifecycle stage, heartbeat, lease, and segment position are all
            # persisted by the worker. Returning them verbatim avoids a second,
            # lossy web-only progress model.
            "execution": (execution.model_dump(mode="json") if execution is not None else None),
        }

    def run(self, run_id: str) -> dict[str, Any] | None:
        """Read one durable record; this works after a web-server restart."""

        try:
            record = RunStore(self.runs_directory).load(run_id)
        except (ValueError, RunNotFoundError, RunStoreError):
            return None
        return self._run_summary(record, self.runs_directory / str(record.run_id))

    def _record(self, run_id: str) -> RunRecord | None:
        try:
            return RunStore(self.runs_directory).load(run_id)
        except (ValueError, RunNotFoundError, RunStoreError):
            return None

    def start_generation(self, payload: Any) -> dict[str, Any]:
        """Validate a browser request and start it without blocking HTTP."""

        if not isinstance(payload, dict):
            raise ValueError("The request body must be a JSON object.")
        mode = payload.get("mode")
        if mode not in ("prompt", "script"):
            raise ValueError("mode must be either 'prompt' or 'script'.")
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Please enter a meditation prompt or script.")
        text = text.strip()
        if len(text) > 20_000:
            raise ValueError("Meditation input must contain at most 20,000 characters.")
        voice = payload.get("voice", "af_heart")
        tts_model = payload.get("tts_model", "kokoro")
        if tts_model not in (
            "kokoro",
            "fish-1.4",
            "moss-local-v1.5",
            "moss-v1.5",
        ):
            raise ValueError("Choose an available local speech model.")
        if tts_model == "kokoro" and voice not in KOKORO_ENGLISH_VOICES:
            raise ValueError("Choose one of Whoopy's reviewed voices.")
        if tts_model == "fish-1.4":
            resolve_tts_model_pack(
                "fish-1.4",
                registry_path=self.config_directory / "model_packs.yaml",
                references_path=self.config_directory / "voice_references.yaml",
                models_root=self.model_pack_models_root,
            )
        moss_language = payload.get("moss_language", "English")
        if moss_language not in MOSS_LANGUAGES:
            raise ValueError("Choose one of MOSS-TTS v1.5's 31 supported languages.")
        moss_instruction = payload.get(
            "moss_instruction",
            "Speak slowly, softly, and warmly, with a meditative delivery.",
        )
        if not isinstance(moss_instruction, str) or len(moss_instruction) > 300:
            raise ValueError("MOSS delivery instructions must contain at most 300 characters.")
        moss_use_reference = payload.get("moss_use_reference", True)
        if not isinstance(moss_use_reference, bool):
            raise ValueError("MOSS voice source must be a boolean.")
        if tts_model.startswith("moss-"):
            resolve_tts_model_pack(
                cast(OptionalTTSBackend, tts_model),
                registry_path=self.config_directory / "model_packs.yaml",
                references_path=self.config_directory / "voice_references.yaml",
                models_root=self.model_pack_models_root,
            )
        try:
            speed = float(payload.get("speed", 0.6))
        except (TypeError, ValueError) as error:
            raise ValueError("Speech speed must be a number.") from error
        if not 0.5 <= speed <= 1.2:
            raise ValueError("Speech speed must be between 0.5 and 1.2.")
        try:
            minutes = float(payload.get("minutes", 3))
        except (TypeError, ValueError) as error:
            raise ValueError("Duration must be a number of minutes.") from error
        if mode == "prompt" and not 1 <= minutes <= 30:
            raise ValueError("Prompt duration must be between 1 and 30 minutes.")

        # Allocating the UUID and record before the child begins means the UI
        # can follow this run through planning and even after a web restart.
        record = RunStore(self.runs_directory).create(text)
        run_id = str(record.run_id)
        task = GenerationTask(task_id=run_id, run_id=run_id, mode=mode)
        with self._task_lock:
            self._tasks[run_id] = task
        thread = threading.Thread(
            target=self._run_generation,
            args=(
                task,
                text,
                tts_model,
                voice,
                speed,
                minutes,
                moss_language,
                moss_instruction,
                moss_use_reference,
            ),
            name=f"whoopy-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self._task_public(task)

    def task(self, task_id: str) -> dict[str, Any] | None:
        try:
            UUID(task_id)
        except ValueError:
            return None
        with self._task_lock:
            task = self._tasks.get(task_id)
        if task is not None:
            return self._task_public(task)
        # The historical /api/tasks endpoint remains a compatibility alias,
        # but it now recovers its answer from the durable record.
        run = self.run(task_id)
        return None if run is None else {"task_id": task_id, **run}

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        """Request cancellation without losing the pre-launch race window."""

        try:
            UUID(task_id)
        except ValueError:
            return None
        with self._task_lock:
            task = self._tasks.get(task_id)
            if task is not None and task.active:
                task.cancel_requested = True
                process = task.process
            else:
                process = None
        if task is None or not task.active:
            return self.cancel_run(task_id)
        if process is not None:
            self._stop_process(process)
        return self._task_public(task)

    def cancel_run(self, run_id: str) -> dict[str, Any] | None:
        """Cancel through the CLI so it also works after a web restart."""

        record = self._record(run_id)
        if record is None:
            return None
        if record.status is RunStatus.QUEUED:
            self._persist_prelaunch_cancel(run_id)
            run = self.run(run_id)
            return None if run is None else {"task_id": run_id, **run}
        if record.status is not RunStatus.RUNNING:
            raise ValueError(
                f"Run {run_id} is {record.status.value}; only queued or running runs can cancel."
            )
        return self._launch_recovery_command(run_id, "cancel", prevalidated=True)

    def resume_run(self, run_id: str) -> dict[str, Any] | None:
        """Launch the CLI's durable resume path without inventing web state."""

        try:
            record = RunStore(self.runs_directory).reconcile_stale_run(run_id)
        except (ValueError, RunNotFoundError, RunStoreError):
            return None
        if record.status not in (RunStatus.FAILED, RunStatus.INTERRUPTED):
            raise ValueError(
                f"Run {run_id} is {record.status.value}; only failed or interrupted runs resume."
            )
        return self._launch_recovery_command(run_id, "resume", prevalidated=True)

    def regenerate_segment(self, run_id: str, segment_id: str) -> dict[str, Any] | None:
        """Ask the CLI to invalidate and rebuild one named speech segment."""

        record = self._record(run_id)
        if record is None:
            return None
        if record.status not in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.INTERRUPTED,
        ):
            raise ValueError(
                f"Run {run_id} is {record.status.value}; only stopped runs regenerate."
            )
        try:
            timeline = RunStore(self.runs_directory).load_timeline(run_id)
        except RunStoreError as error:
            raise ValueError(f"Run {run_id} has no valid timeline to regenerate.") from error
        matching = [segment for segment in timeline.segments if segment.id == segment_id]
        if not matching:
            raise ValueError(f"Segment {segment_id} is not present in run {run_id}.")
        if not isinstance(matching[0], SpeechSegment):
            raise ValueError(f"Segment {segment_id} is not a speech segment.")
        return self._launch_recovery_command(
            run_id,
            "regenerate-segment",
            segment_id,
            prevalidated=True,
        )

    def _task_public(self, task: GenerationTask) -> dict[str, Any]:
        run = self.run(task.run_id)
        if run is None:
            # The record was successfully created before the task was stored;
            # this branch is only defensive against external manual deletion.
            return {"task_id": task.task_id, "run_id": task.run_id, "status": "missing"}
        return {"task_id": task.task_id, **run}

    def _launch_recovery_command(
        self,
        run_id: str,
        command_name: str,
        segment_id: str | None = None,
        *,
        prevalidated: bool = False,
    ) -> dict[str, Any] | None:
        if not prevalidated and self.run(run_id) is None:
            return None
        task = GenerationTask(task_id=run_id, run_id=run_id)
        with self._task_lock:
            existing = self._tasks.get(run_id)
            if existing is not None and existing.active:
                raise ValueError("This run already has an active local worker.")
            self._tasks[run_id] = task
        thread = threading.Thread(
            target=self._run_recovery_command,
            args=(task, command_name, segment_id),
            name=f"whoopy-{command_name}-{run_id[:8]}",
            daemon=True,
        )
        thread.start()
        return self._task_public(task)

    def _run_generation(
        self,
        task: GenerationTask,
        text: str,
        tts_model: str,
        voice: str,
        speed: float,
        minutes: float,
        moss_language: str,
        moss_instruction: str,
        moss_use_reference: bool,
    ) -> None:
        input_path: Path | None = None
        try:
            with self._task_lock:
                cancelled_before_launch = task.cancel_requested
            if cancelled_before_launch:
                self._persist_prelaunch_cancel(task.run_id)
                return
            command = [
                sys.executable,
                "-m",
                "whoopy",
                "generate",
                "--json",
                "--run-id",
                task.run_id,
                "--tts-model",
                tts_model,
                "--voice",
                voice,
                "--speed",
                str(speed),
                "--moss-language",
                moss_language,
                "--moss-instruction",
                moss_instruction,
                "--config-dir",
                str(self.config_directory),
                "--models-dir",
                str(self.models_directory),
                "--runs-dir",
                str(self.runs_directory),
            ]
            if not moss_use_reference:
                command.append("--moss-direct-voice")
            if task.mode == "prompt":
                command.extend(
                    [
                        text,
                        "--minutes",
                        str(minutes),
                        "--profile",
                        "standard",
                    ]
                )
            else:
                input_directory = self.runs_directory / ".web-inputs"
                input_directory.mkdir(parents=True, exist_ok=True)
                input_path = input_directory / f"{task.task_id}.md"
                input_path.write_text(text + "\n", encoding="utf-8")
                command.extend(["--script-file", str(input_path), "--profile", "basic"])
            process = self._command_launcher(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self._process_group_options(),
            )
            with self._task_lock:
                task.process = process
                task.launching = False
                cancel_after_launch = task.cancel_requested
            if cancel_after_launch:
                self._stop_process(process)
            stdout, stderr = process.communicate()
            # The CLI owns all durable transitions and errors.  Do not write a
            # competing status from the web process merely because its child
            # exited; the record remains the source of truth.
            with self._task_lock:
                cancelled = task.cancel_requested
            if cancelled:
                self._persist_prelaunch_cancel(task.run_id)
            elif process.returncode not in (0, 130, 143):
                self._persist_process_failure(task.run_id, self._friendly_process_error(stderr))
            _ = stdout
        except OSError as error:
            self._persist_process_failure(task.run_id, f"Could not launch generation: {error}")
        finally:
            with self._task_lock:
                task.launching = False
                task.process = None
            if input_path is not None:
                with suppress(OSError):
                    input_path.unlink(missing_ok=True)

    def _run_recovery_command(
        self,
        task: GenerationTask,
        command_name: str,
        segment_id: str | None,
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "whoopy",
            "run",
            command_name,
            task.run_id,
        ]
        if segment_id is not None:
            command.append(segment_id)
        command.extend(
            [
                "--json",
                "--config-dir",
                str(self.config_directory),
                "--models-dir",
                str(self.models_directory),
                "--runs-dir",
                str(self.runs_directory),
            ]
        )
        try:
            process = self._command_launcher(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                **self._process_group_options(),
            )
            with self._task_lock:
                task.process = process
                task.launching = False
                cancel_after_launch = task.cancel_requested
            if cancel_after_launch and command_name != "cancel":
                self._stop_process(process)
            _stdout, stderr = process.communicate()
            with self._task_lock:
                cancelled = task.cancel_requested
            if cancelled:
                self._persist_prelaunch_cancel(task.run_id)
            elif process.returncode not in (0, 130, 143):
                self._persist_process_failure(
                    task.run_id,
                    self._friendly_process_error(stderr),
                )
        except OSError as error:
            self._persist_process_failure(
                task.run_id,
                f"Could not launch {command_name}: {error}",
            )
        finally:
            with self._task_lock:
                task.launching = False
                task.process = None

    @staticmethod
    def _process_group_options() -> dict[str, object]:
        if os.name == "posix":
            return {"start_new_session": True}
        creation_flag = int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        return {"creationflags": creation_flag} if creation_flag else {}

    @staticmethod
    def _stop_process(process: Any) -> None:
        """Bound cancellation so the HTTP request never waits indefinitely."""

        if process.poll() is not None:
            return
        try:
            if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
                cast(Any, os).killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
            return
        except (OSError, ProcessLookupError):
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix" and isinstance(getattr(process, "pid", None), int):
                cast(Any, os).killpg(
                    process.pid,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
            else:
                process.kill()
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            return

    def _persist_process_failure(self, run_id: str, message: str) -> None:
        """Keep launcher/CLI failures from becoming permanently queued runs."""

        store = RunStore(self.runs_directory)
        try:
            with RunLock(store.run_directory(run_id)):
                record = store.load(run_id)
                if record.recovery is None or record.status not in (
                    RunStatus.QUEUED,
                    RunStatus.FAILED,
                    RunStatus.INTERRUPTED,
                ):
                    return
                now = datetime.now(UTC)
                attempt_id = uuid4()
                resumed = record.status in (RunStatus.FAILED, RunStatus.INTERRUPTED)
                stage = (
                    RunStage.PLANNING
                    if record.status is RunStatus.QUEUED and record.script_artifact is None
                    else RunStage.COMPILING
                )
                stopped_execution = RunExecution(
                    stage=stage,
                    attempt_id=attempt_id,
                    owner_id=f"web:{socket.gethostname()}:{os.getpid()}",
                    pid=os.getpid(),
                    started_at=now,
                    heartbeat_at=now,
                    lease_expires_at=now + timedelta(seconds=1),
                    message="The local command could not start.",
                ).finish(message=message or "Local command failed without an error message.")
                failed = record.transition(
                    RunStatus.FAILED,
                    updated_at=now,
                    recovery=record.recovery.model_copy(
                        update={
                            "process_attempts": record.recovery.process_attempts + 1,
                            "resume_count": record.recovery.resume_count + int(resumed),
                            "failed_segment_id": None,
                        }
                    ),
                    execution=stopped_execution,
                    error=message or "Local command failed without an error message.",
                )
                # One atomic record replacement prevents observers from seeing
                # an artificial RUNNING state for a command that never started.
                store.save(failed)
        except (OSError, RunLockUnavailable, RunStoreError, ValueError):
            return

    def _persist_prelaunch_cancel(self, run_id: str) -> None:
        """Turn a queued web request into a durable, resumable interruption."""

        store = RunStore(self.runs_directory)
        try:
            with RunLock(store.run_directory(run_id)):
                record = store.load(run_id)
                if record.status is not RunStatus.QUEUED or record.recovery is None:
                    return
                now = datetime.now(UTC)
                interrupted = record.transition(
                    RunStatus.INTERRUPTED,
                    updated_at=now,
                    recovery=record.recovery.model_copy(
                        update={"process_attempts": record.recovery.process_attempts + 1}
                    ),
                    execution=RunExecution(
                        stage=(
                            RunStage.PLANNING
                            if record.script_artifact is None
                            else RunStage.COMPILING
                        ),
                        interruption_kind="user_cancelled",
                        message=(
                            "Generation was cancelled before the local command started; "
                            "the durable request can be resumed."
                        ),
                    ),
                )
                store.save(interrupted)
        except (OSError, RunLockUnavailable, RunStoreError, ValueError):
            return

    @staticmethod
    def _friendly_process_error(stderr: str) -> str:
        lines = [line.strip() for line in stderr.splitlines() if line.strip()]
        if not lines:
            return "Whoopy stopped without an error message."
        message = lines[-1]
        prefix = "whoopy: error: "
        return message[len(prefix) :] if message.startswith(prefix) else message

    def artifact_path(self, run_id: str, artifact: str) -> Path | None:
        """Resolve an allow-listed run artifact without accepting path input."""

        try:
            parsed = UUID(run_id)
        except ValueError:
            return None
        filename = ALLOWED_ARTIFACTS.get(artifact)
        if filename is None:
            return None
        path = self.runs_directory / str(parsed) / filename
        return path if path.is_file() else None

    def audio_path(self, run_id: str) -> Path | None:
        try:
            parsed = UUID(run_id)
        except ValueError:
            return None
        path = self.runs_directory / str(parsed) / "narration.wav"
        return path if path.is_file() else None


def _handler_factory(application: LocalWebApplication) -> type[BaseHTTPRequestHandler]:
    asset_root = files("whoopy.webui").joinpath("static")

    class WhoopyRequestHandler(BaseHTTPRequestHandler):
        server_version = "WhoopyLocal/1"

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = unquote(parsed.path)
            if path == "/api/health":
                self._send_json({"ok": True, "service": "whoopy-local"})
                return
            if path == "/api/status":
                self._send_json(application.environment_status())
                return
            if path == "/api/model-packs":
                try:
                    self._send_json(application.list_model_packs())
                except (OSError, ValueError) as error:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                return
            if path == "/api/runs":
                self._send_json({"runs": application.list_runs()})
                return
            if path.startswith("/api/tasks/"):
                task = application.task(path.removeprefix("/api/tasks/"))
                if task is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Task not found.")
                else:
                    self._send_json(task)
                return
            if path.startswith("/api/runs/"):
                self._serve_run_path(path)
                return
            if path == "/favicon.ico":
                # Browsers request this automatically. An empty success keeps
                # the local log focused on actual application problems.
                self._send_bytes(b"", "image/x-icon", status=HTTPStatus.NO_CONTENT)
                return
            static_name = {"/": "index.html", "/styles.css": "styles.css", "/app.js": "app.js"}.get(
                path
            )
            if static_name is None:
                self._send_error(HTTPStatus.NOT_FOUND, "Page not found.")
                return
            asset = asset_root.joinpath(static_name)
            payload = asset.read_bytes()
            content_type = mimetypes.guess_type(static_name)[0] or "application/octet-stream"
            self._send_bytes(payload, content_type)

        def do_POST(self) -> None:
            if not self._origin_is_local():
                self._send_error(HTTPStatus.FORBIDDEN, "Only the local Whoopy page may post.")
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/generate":
                try:
                    payload = self._read_json()
                    task = application.start_generation(payload)
                except ValueError as error:
                    self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                    return
                self._send_json(task, status=HTTPStatus.ACCEPTED)
                return
            if parsed.path.startswith("/api/model-packs"):
                self._post_model_pack_action(parsed.path)
                return
            if parsed.path.startswith("/api/runs/"):
                self._post_run_action(parsed.path)
                return
            if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/cancel"):
                # Compatibility with the temporary Phase 3 tester. New code
                # uses the durable run endpoint below.
                task_id = parsed.path.removeprefix("/api/tasks/").removesuffix("/cancel")
                cancelled_task = application.cancel_task(task_id)
                if cancelled_task is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Task not found.")
                else:
                    self._send_json(cancelled_task)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Endpoint not found.")

        def _post_model_pack_action(self, path: str) -> None:
            """Dispatch only the explicit, local model-pack action routes."""

            parts = path.strip("/").split("/")
            action: str
            pack_id: str | None
            if parts == ["api", "model-packs", "unload"]:
                action, pack_id = "unload", None
            elif parts == ["api", "model-packs", "restore"]:
                action, pack_id = "restore", None
            elif len(parts) == 4 and parts[:2] == ["api", "model-packs"]:
                pack_id, action = parts[2], parts[3]
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "Model-pack action not found.")
                return
            if action not in {"install", "verify", "smoke-test", "unload", "remove", "restore"}:
                self._send_error(HTTPStatus.NOT_FOUND, "Model-pack action not found.")
                return
            try:
                payload = self._read_json()
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object.")
                report = application.model_pack_action(action, pack_id, payload)
            except (OSError, ValueError) as error:
                self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                return
            self._send_json(report)

        def _post_run_action(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) < 4 or parts[:2] != ["api", "runs"]:
                self._send_error(HTTPStatus.NOT_FOUND, "Run action not found.")
                return
            run_id = parts[2]
            try:
                UUID(run_id)
            except ValueError:
                self._send_error(HTTPStatus.NOT_FOUND, "Run not found.")
                return
            try:
                payload = self._read_json()
                if not isinstance(payload, dict):
                    raise ValueError("Request body must be a JSON object.")
                if len(parts) == 4 and parts[3] == "cancel":
                    if payload:
                        raise ValueError("Cancel does not accept request fields.")
                    task = application.cancel_task(run_id)
                elif len(parts) == 4 and parts[3] == "resume":
                    if payload:
                        raise ValueError("Resume does not accept request fields.")
                    task = application.resume_run(run_id)
                elif len(parts) == 6 and parts[3] == "segments" and parts[5] == "regenerate":
                    segment_id = parts[4]
                    if not SEGMENT_ID_PATTERN.fullmatch(segment_id):
                        raise ValueError(
                            "Segment ID must contain only lowercase letters, digits, _ or -."
                        )
                    if payload:
                        raise ValueError("Regenerate does not accept request fields.")
                    task = application.regenerate_segment(run_id, segment_id)
                else:
                    self._send_error(HTTPStatus.NOT_FOUND, "Run action not found.")
                    return
            except ValueError as error:
                self._send_error(HTTPStatus.BAD_REQUEST, str(error))
                return
            if task is None:
                self._send_error(HTTPStatus.NOT_FOUND, "Run not found.")
            else:
                self._send_json(task, status=HTTPStatus.ACCEPTED)

        def _serve_run_path(self, path: str) -> None:
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                run = application.run(parts[2])
                if run is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Run not found.")
                else:
                    self._send_json(run)
                return
            if len(parts) == 4 and parts[:2] == ["api", "runs"] and parts[3] == "audio":
                audio = application.audio_path(parts[2])
                if audio is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Audio not found.")
                else:
                    self._send_audio(audio)
                return
            if len(parts) == 5 and parts[:2] == ["api", "runs"] and parts[3] == "artifact":
                artifact = application.artifact_path(parts[2], parts[4])
                if artifact is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Artifact not found.")
                else:
                    content_type = mimetypes.guess_type(artifact.name)[0] or "application/json"
                    self._send_bytes(artifact.read_bytes(), content_type)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Run resource not found.")

        def _send_audio(self, path: Path) -> None:
            """Stream a WAV with byte-range support for browser seek/cancel behavior."""

            size = path.stat().st_size
            start = 0
            end = size - 1
            status = HTTPStatus.OK
            range_header = self.headers.get("Range")
            if range_header:
                try:
                    unit, requested = range_header.split("=", 1)
                    first, last = requested.split("-", 1)
                    if unit.strip().lower() != "bytes" or "," in requested:
                        raise ValueError
                    if first:
                        start = int(first)
                        end = int(last) if last else end
                    elif last:
                        suffix_length = int(last)
                        if suffix_length <= 0:
                            raise ValueError
                        start = max(0, size - suffix_length)
                    if start < 0 or start >= size or end < start:
                        raise ValueError
                    end = min(end, size - 1)
                except ValueError:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status = HTTPStatus.PARTIAL_CONTENT

            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()

            try:
                with path.open("rb") as stream:
                    stream.seek(start)
                    remaining = length
                    while remaining:
                        chunk = stream.read(min(64 * 1_024, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                # Seeking or closing the browser player legitimately cancels a
                # request. It is not a failed Whoopy generation.
                return

        def _read_json(self) -> Any:
            content_type = self.headers.get_content_type()
            if content_type != "application/json":
                raise ValueError("Content-Type must be application/json.")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("Invalid Content-Length.") from error
            if not 0 < length <= MAX_REQUEST_BYTES:
                raise ValueError(f"Request must contain at most {MAX_REQUEST_BYTES} bytes.")
            try:
                return json.loads(self.rfile.read(length))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Request body must contain valid UTF-8 JSON.") from error

        def _origin_is_local(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                # Native clients do not send Origin. The server is loopback-only,
                # so they retain access without pretending to be a browser page.
                return True
            parsed = urlparse(origin)
            host = self.headers.get("Host", "").lower()
            return (
                parsed.scheme == "http"
                and parsed.username is None
                and parsed.password is None
                and parsed.hostname in ("127.0.0.1", "localhost")
                and parsed.netloc.lower() == host
            )

        def _send_json(
            self,
            payload: Any,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self._send_bytes(
                (json.dumps(payload, separators=(",", ":")) + "\n").encode(),
                "application/json; charset=utf-8",
                status=status,
            )

        def _send_error(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"error": message}, status=status)

        def _send_bytes(
            self,
            payload: bytes,
            content_type: str,
            *,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; media-src 'self'")
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                return

        def log_message(self, format: str, *args: object) -> None:
            print(f"[whoopy web] {self.address_string()} {format % args}", file=sys.stderr)

    return WhoopyRequestHandler


def serve(
    *,
    host: str,
    port: int,
    project_root: Path,
    config_directory: Path,
    models_directory: Path,
    runs_directory: Path,
    open_browser: bool = False,
) -> None:
    """Serve until interrupted, binding only to the local loopback interface."""

    application = LocalWebApplication(
        project_root=project_root,
        config_directory=config_directory,
        models_directory=models_directory,
        runs_directory=runs_directory,
    )
    server = ThreadingHTTPServer((host, port), _handler_factory(application))
    url = f"http://{host}:{server.server_port}"
    print("Whoopy local studio")
    print(f"  Open: {url}")
    print("  Privacy: bound to this laptop only; press Ctrl+C to stop.")
    if open_browser:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Whoopy local studio.")
    finally:
        server.server_close()
