"""Dependency-free local HTTP server for Whoopy's browser tester.

The web layer deliberately starts the existing CLI in a child process. That
keeps the CLI as the single generation entry point and prevents the tester from
growing a second orchestration path with different validation or recovery rules.
"""

from __future__ import annotations

import json
import mimetypes
import subprocess
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from uuid import UUID, uuid4

from whoopy.artifacts import (
    ArtifactError,
    ArtifactState,
    ArtifactStore,
    TargetPlatform,
    load_artifact_lock,
)
from whoopy.hardware import diagnose, inspect_hardware, load_runtime_profiles
from whoopy.pipeline import RunRecord
from whoopy.voices import KOKORO_ENGLISH_VOICES

MAX_REQUEST_BYTES = 64_000
MAX_RECENT_RUNS = 24
ALLOWED_ARTIFACTS = {
    "run": "run.json",
    "script": "script.md",
    "plan": "plan.json",
    "timeline": "timeline.json",
    "quality": "quality.json",
    "manifest": "audio-manifest.json",
    "models": "model-metadata.json",
}


def _utc_now_text() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class GenerationTask:
    """In-memory status for one process launched from the browser."""

    task_id: str
    mode: str
    title: str
    status: str = "queued"
    created_at: str = field(default_factory=_utc_now_text)
    updated_at: str = field(default_factory=_utc_now_text)
    run_id: str | None = None
    error: str | None = None
    process: subprocess.Popen[str] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        """Return only values intended for the JSON API."""

        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "title": self.title,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "run_id": self.run_id,
            "error": self.error,
        }


class LocalWebApplication:
    """Own local paths, task processes, and inspectable run history."""

    def __init__(
        self,
        *,
        project_root: Path,
        config_directory: Path,
        models_directory: Path,
        runs_directory: Path,
    ) -> None:
        self.project_root = project_root.resolve()
        self.config_directory = self._resolve(config_directory)
        self.models_directory = self._resolve(models_directory)
        self.runs_directory = self._resolve(runs_directory)
        self._tasks: dict[str, GenerationTask] = {}
        self._task_lock = threading.Lock()

    def _resolve(self, path: Path) -> Path:
        return path.resolve() if path.is_absolute() else (self.project_root / path).resolve()

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
            return {
                "ok": True,
                "system": f"{target.operating_system} {target.architecture}",
                "profiles": profile_reports,
                "privacy": "Models and generation stay on this laptop.",
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
        return {
            "run_id": str(record.run_id),
            "status": record.status.value,
            "source_kind": record.source_kind,
            "title": record.prompt,
            "created_at": record.created_at.isoformat(),
            "updated_at": record.updated_at.isoformat(),
            "duration_seconds": duration_seconds,
            "quality_passed": quality_passed,
            "has_audio": record.audio_artifact is not None
            and (directory / "narration.wav").is_file(),
            "error": record.error,
            "recovery": (
                record.recovery.model_dump(mode="json") if record.recovery is not None else None
            ),
        }

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
        if voice not in KOKORO_ENGLISH_VOICES:
            raise ValueError("Choose one of Whoopy's reviewed voices.")
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

        task_id = str(uuid4())
        title = text if len(text) <= 90 else f"{text[:87]}..."
        task = GenerationTask(task_id=task_id, mode=mode, title=title)
        with self._task_lock:
            self._tasks[task_id] = task
        thread = threading.Thread(
            target=self._run_generation,
            args=(task, text, voice, speed, minutes),
            name=f"whoopy-{task_id[:8]}",
            daemon=True,
        )
        thread.start()
        return task.public()

    def task(self, task_id: str) -> dict[str, Any] | None:
        try:
            UUID(task_id)
        except ValueError:
            return None
        with self._task_lock:
            task = self._tasks.get(task_id)
            return None if task is None else task.public()

    def cancel_task(self, task_id: str) -> dict[str, Any] | None:
        """Stop only the selected child process; durable checkpoints remain."""

        try:
            UUID(task_id)
        except ValueError:
            return None
        with self._task_lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task.status in ("completed", "failed", "cancelled"):
                return task.public()
            task.status = "cancelled"
            task.updated_at = _utc_now_text()
            process = task.process
        if process is not None and process.poll() is None:
            process.terminate()
        return task.public()

    def _run_generation(
        self,
        task: GenerationTask,
        text: str,
        voice: str,
        speed: float,
        minutes: float,
    ) -> None:
        input_path: Path | None = None
        with self._task_lock:
            if task.status == "cancelled":
                return
            task.status = "running"
            task.updated_at = _utc_now_text()

        command = [
            sys.executable,
            "-m",
            "whoopy",
            "generate",
            "--json",
            "--voice",
            voice,
            "--speed",
            str(speed),
            "--config-dir",
            str(self.config_directory),
            "--models-dir",
            str(self.models_directory),
            "--runs-dir",
            str(self.runs_directory),
        ]
        if task.mode == "prompt":
            command.extend(
                [
                    text,
                    "--minutes",
                    str(minutes),
                    "--profile",
                    "standard",
                    "--draft-id",
                    task.task_id,
                ]
            )
        else:
            input_directory = self.runs_directory / ".web-inputs"
            input_directory.mkdir(parents=True, exist_ok=True)
            input_path = input_directory / f"{task.task_id}.md"
            input_path.write_text(text + "\n", encoding="utf-8")
            command.extend(["--script-file", str(input_path), "--profile", "basic"])

        try:
            process = subprocess.Popen(
                command,
                cwd=self.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            with self._task_lock:
                task.process = process
                cancelled = task.status == "cancelled"
            if cancelled:
                process.terminate()
            stdout, stderr = process.communicate()
            with self._task_lock:
                if task.status == "cancelled":
                    return
                if process.returncode == 0:
                    result = json.loads(stdout)
                    task.run_id = str(result["run_id"])
                    task.status = "completed"
                else:
                    task.status = "failed"
                    task.error = self._friendly_process_error(stderr)
                task.updated_at = _utc_now_text()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            with self._task_lock:
                if task.status != "cancelled":
                    task.status = "failed"
                    task.error = str(error)
                    task.updated_at = _utc_now_text()
        finally:
            with self._task_lock:
                task.process = None
            if input_path is not None:
                input_path.unlink(missing_ok=True)

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
            if parsed.path.startswith("/api/tasks/") and parsed.path.endswith("/cancel"):
                task_id = parsed.path.removeprefix("/api/tasks/").removesuffix("/cancel")
                cancelled_task = application.cancel_task(task_id)
                if cancelled_task is None:
                    self._send_error(HTTPStatus.NOT_FOUND, "Task not found.")
                else:
                    self._send_json(cancelled_task)
                return
            self._send_error(HTTPStatus.NOT_FOUND, "Endpoint not found.")

        def _serve_run_path(self, path: str) -> None:
            parts = path.strip("/").split("/")
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
                return True
            hostname = urlparse(origin).hostname
            return hostname in ("127.0.0.1", "localhost")

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
