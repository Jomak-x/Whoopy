"""Independent, on-demand word screening for completed local narration.

The coordinator is an isolated child, not a web-server thread. Cancellation is
an attempt-scoped marker; no web request signals a PID recovered from disk.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

import psutil
from pydantic import BaseModel, ConfigDict, Field

from whoopy.adapters.llm.llama_cpp import _stop_process_tree
from whoopy.evaluation.audio_verification import file_sha256
from whoopy.locations import runtime_worker_script
from whoopy.model_packs.operations import HeavyweightModelSlot, _atomic_json, safe_managed_path
from whoopy.pipeline import RunStore
from whoopy.pipeline.locks import RunLock, RunLockUnavailable
from whoopy.webui.audio_inspection import current_audio_artifacts

GIB = 1024**3
POLICY: Literal["normalized-word-mismatch-review-v3"] = "normalized-word-mismatch-review-v3"
TIMEOUT_SECONDS = 900
QUEUED_GRACE_SECONDS = 30
HASH_PATTERN = r"^[0-9a-f]{64}$"


def _supported_host() -> bool:
    return sys.platform == "darwin" and platform.machine() == "arm64"


class SpeechScreenError(ValueError):
    """The optional screen cannot run or cannot establish current word evidence."""


class _Stopped(SpeechScreenError):
    def __init__(self, reason: str, status: str = "failed") -> None:
        super().__init__(reason)
        self.status = status


class Installation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1]
    engine: Literal["mlx-whisper"]
    python_executable: Path
    python_sha256: str = Field(pattern=HASH_PATTERN)
    model_directory: Path
    weights_sha256: str = Field(pattern=HASH_PATTERN)
    config_sha256: str = Field(pattern=HASH_PATTERN)


class InputIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    audio_sha256: str = Field(pattern=HASH_PATTERN)
    timeline_sha256: str = Field(pattern=HASH_PATTERN)
    expected_text_sha256: str = Field(pattern=HASH_PATTERN)


class ScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal[1] = 1
    run_id: UUID
    attempt_id: UUID
    created_at: str
    identity: InputIdentity
    installation: Installation
    installation_record_sha256: str = Field(pattern=HASH_PATTERN)
    worker_source_sha256: str = Field(pattern=HASH_PATTERN)
    verifier_source_sha256: str = Field(pattern=HASH_PATTERN)
    coordinator_source_sha256: str = Field(pattern=HASH_PATTERN)
    policy_id: Literal["normalized-word-mismatch-review-v3"] = POLICY


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or path.stat().st_size > 32 * 1024**2:
        raise SpeechScreenError("Unsafe or oversized screening receipt.")
    raw = json.loads(
        path.read_text(encoding="utf-8"),
        parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Nonfinite JSON value")),
    )
    if not isinstance(raw, dict):
        raise SpeechScreenError("Screening receipt must be a JSON object.")
    return raw


def _exclusive_json(path: Path, value: dict[str, Any]) -> None:
    encoded = json.dumps(value, indent=2, allow_nan=False) + "\n"
    with path.open("x", encoding="utf-8") as destination:
        destination.write(encoded)
        destination.flush()
        os.fsync(destination.fileno())
    path.chmod(0o444)


def _owner_alive(state: dict[str, Any]) -> bool:
    try:
        return bool(
            state["host"] == socket.gethostname()
            and psutil.Process(state["pid"]).create_time() == state["process_created_at"]
        )
    except (KeyError, TypeError, ValueError, psutil.Error):
        return False


def _canonical_inputs(store: RunStore, run_id: str) -> tuple[InputIdentity, str, bytes, Path]:
    record = store.load(run_id)
    audio, _manifest, _quality = current_audio_artifacts(store, record)
    timeline = store.load_timeline(run_id)
    spoken = [segment for segment in timeline.segments if segment.type == "SPEECH"]
    if not spoken or any(
        segment.language.lower() not in {"english", "en", "en-us", "en-gb"} for segment in spoken
    ):
        raise SpeechScreenError("This local speech screen supports English spoken segments only.")
    expected = "\n".join(segment.text for segment in spoken)
    timeline_bytes = store.timeline_path(run_id).read_bytes()
    identity = InputIdentity(
        audio_sha256=file_sha256(audio),
        timeline_sha256=hashlib.sha256(timeline_bytes).hexdigest(),
        expected_text_sha256=hashlib.sha256(expected.encode("utf-8")).hexdigest(),
    )
    return identity, expected, timeline_bytes, audio


class SpeechScreening:
    """Task-specific durable receipts and isolated launch for one optional screen."""

    def __init__(
        self, runs_root: Path, models_root: Path, *, launcher: Any = subprocess.Popen
    ) -> None:
        self.runs_root = runs_root.resolve()
        self.models_root = models_root.resolve()
        self.store = RunStore(self.runs_root)
        self.launcher = launcher
        self._children: dict[str, Any] = {}

    @property
    def installation_path(self) -> Path:
        return self.models_root / "verification/speech-screening.json"

    def _attempt(self, run_id: str, attempt_id: str) -> Path:
        return safe_managed_path(
            self.runs_root,
            self.runs_root / str(UUID(run_id)) / "speech-screening" / str(UUID(attempt_id)),
            label="speech-screen attempt",
        )

    def _installation(self, *, full: bool) -> Installation:
        if not _supported_host():
            raise SpeechScreenError("Local MLX Whisper screening requires macOS Apple Silicon.")
        path = safe_managed_path(self.models_root, self.installation_path)
        if not path.is_file():
            raise SpeechScreenError(
                "Local speech screening is not configured: "
                "add models/verification/speech-screening.json."
            )
        installation = Installation.model_validate(_read_json(path))
        python, model = installation.python_executable, installation.model_directory
        if not python.is_absolute() or not model.is_absolute():
            raise SpeechScreenError(
                "Speech-screen installation paths must be absolute local paths."
            )
        if not python.is_file() or not os.access(python, os.X_OK):
            raise SpeechScreenError("The configured local ASR interpreter is unavailable.")
        if not (model / "weights.npz").is_file() or not (model / "config.json").is_file():
            raise SpeechScreenError("The configured local Whisper snapshot is incomplete.")
        installation = installation.model_copy(update={"model_directory": model.resolve()})
        if full and (
            file_sha256(python) != installation.python_sha256
            or file_sha256(model / "weights.npz") != installation.weights_sha256
            or file_sha256(model / "config.json") != installation.config_sha256
        ):
            raise SpeechScreenError("Local ASR installation differs from its pinned file hashes.")
        return installation

    def availability(self) -> tuple[bool, str | None]:
        try:
            self._installation(full=False)
            return True, None
        except (OSError, ValueError) as error:
            return False, str(error)

    def _latest(self, run_id: str) -> Path | None:
        root = safe_managed_path(
            self.runs_root, self.runs_root / str(UUID(run_id)) / "speech-screening"
        )
        if not root.is_dir():
            return None
        pointer = _read_json(root / "latest.json")
        return self._attempt(run_id, str(pointer["attempt_id"]))

    def _request(self, run_id: str, attempt: Path) -> ScreenRequest:
        request = ScreenRequest.model_validate(_read_json(attempt / "request.json"))
        if str(request.run_id) != str(UUID(run_id)) or str(request.attempt_id) != attempt.name:
            raise SpeechScreenError("Speech-screen receipt belongs to another run or attempt.")
        return request

    def has_active(self) -> bool:
        if not self.runs_root.is_dir():
            return False
        for run in self.runs_root.iterdir():
            try:
                UUID(run.name)
                attempt = self._latest(run.name)
                if attempt is None or (attempt / "result.json").exists():
                    continue
                request = self._request(run.name, attempt)
                state = _read_json(attempt / "state.json")
                age = (
                    datetime.now(UTC) - datetime.fromisoformat(request.created_at)
                ).total_seconds()
                if _owner_alive(state) or (state.get("pid") is None and age < QUEUED_GRACE_SECONDS):
                    return True
            except (OSError, ValueError, KeyError):
                continue
        return False

    def _current(self, request: ScreenRequest) -> bool:
        try:
            return _canonical_inputs(self.store, str(request.run_id))[0] == request.identity
        except (OSError, ValueError, RuntimeError):
            return False

    def status(self, run_id: str) -> dict[str, Any]:
        available, reason = self.availability()
        status: dict[str, Any] = {
            "available": available,
            "availability_reason": reason,
            "operation_status": "not_started",
            "word_status": "unverified",
            "current": False,
            "attempt_id": None,
            "stage": None,
            "elapsed_seconds": None,
            "cancellable": False,
            "human_review": "not_assessed",
            "failure_reasons": [],
            "review_reasons": [],
            "message": "Spoken words have not been checked.",
            "report_url": None,
        }
        try:
            child = self._children.get(run_id)
            if child is not None:
                child.poll()  # Reap a locally launched coordinator without owning its lifecycle.
            attempt = self._latest(run_id)
            if attempt is None:
                return status
            request = self._request(run_id, attempt)
            state = _read_json(attempt / "state.json")
            status.update(
                attempt_id=attempt.name,
                stage=state.get("stage"),
                operation_status=state.get("operation_status", "interrupted"),
                message=state.get("message", "Speech screening did not complete."),
                elapsed_seconds=state.get("elapsed_seconds"),
                created_at=request.created_at,
                checked_at=None,
                model_revision=request.installation.model_directory.name,
            )
            if (attempt / "result.json").is_file():
                result = _read_json(attempt / "result.json")
                if result.get("attempt_id") != attempt.name or result.get("run_id") != run_id:
                    raise SpeechScreenError("Mismatched speech-screen result identity.")
                if file_sha256(attempt / "request.json") != result.get("request_sha256"):
                    raise SpeechScreenError("Speech-screen request changed since completion.")
                if (
                    result.get("word_status") != "unverified"
                    and result.get("report_sha256") is None
                ):
                    raise SpeechScreenError(
                        "A word verdict requires a completed independent report."
                    )
                if (
                    result.get("word_status") != "unverified"
                    and result.get("operation_status") != "completed"
                ):
                    raise SpeechScreenError("A word verdict requires a completed operation.")
                status.update(
                    operation_status=result["operation_status"],
                    word_status=result["word_status"],
                    message=result["message"],
                    elapsed_seconds=result["elapsed_seconds"],
                    checked_at=result["finished_at"],
                )
                if result.get("report_sha256") is not None:
                    report = self._validated_report(request, attempt, result)
                    status.update(
                        current=self._current(request),
                        review_reasons=report["review_reasons"],
                        failure_reasons=report["failure_reasons"],
                        report_url=f"/api/v1/runs/{run_id}/speech-screen/{attempt.name}/report",
                    )
                    if not status["current"]:
                        status.update(
                            word_status="stale",
                            message="Audio or timeline changed after this word check.",
                        )
                return status
            age = (datetime.now(UTC) - datetime.fromisoformat(request.created_at)).total_seconds()
            active = (
                _owner_alive(state) if state.get("pid") is not None else age < QUEUED_GRACE_SECONDS
            )
            if not active:
                status.update(
                    operation_status="interrupted",
                    message="Speech screening was interrupted; start a fresh check.",
                )
            status["cancellable"] = active
            return status
        except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
            status.update(
                operation_status="failed",
                word_status="unverified",
                current=False,
                message=f"Speech-screen evidence is unavailable: {error}",
            )
            return status

    def _validated_report(
        self, request: ScreenRequest, attempt: Path, result: dict[str, Any]
    ) -> dict[str, Any]:
        if result.get("run_id") != str(request.run_id) or result.get("attempt_id") != attempt.name:
            raise SpeechScreenError("Mismatched speech-screen result identity.")
        if result.get("operation_status") != "completed":
            raise SpeechScreenError("Word evidence requires a completed comparison operation.")
        for filename in (
            "request.json",
            "report.json",
            "asr.json",
            "expected.txt",
            "timeline.json",
            "input.wav",
            "whisper-worker.py",
            "verifier-source.py",
            "coordinator-source.py",
        ):
            safe_managed_path(self.runs_root, attempt / filename)
        if result.get("policy_id") != POLICY or request.policy_id != POLICY:
            raise SpeechScreenError("This attempt does not use the required word-screen policy.")
        for filename, key in (("report.json", "report_sha256"), ("asr.json", "asr_sha256")):
            if file_sha256(attempt / filename) != result.get(key):
                raise SpeechScreenError("Speech-screen evidence changed since completion.")
        if file_sha256(attempt / "expected.txt") != request.identity.expected_text_sha256:
            raise SpeechScreenError("Expected transcript snapshot changed.")
        if file_sha256(attempt / "timeline.json") != request.identity.timeline_sha256:
            raise SpeechScreenError("Timeline snapshot changed.")
        for name, digest in (
            ("input.wav", request.identity.audio_sha256),
            ("whisper-worker.py", request.worker_source_sha256),
            ("verifier-source.py", request.verifier_source_sha256),
            ("coordinator-source.py", request.coordinator_source_sha256),
        ):
            if file_sha256(attempt / name) != digest:
                raise SpeechScreenError("Speech-screen input or source snapshot changed.")
        report, asr = _read_json(attempt / "report.json"), _read_json(attempt / "asr.json")
        if (
            report.get("policy_id") != POLICY
            or report.get("status") != result.get("word_status")
            or report.get("audio_sha256") != request.identity.audio_sha256
            or report.get("expected_text_sha256") != request.identity.expected_text_sha256
            or report.get("verifier_source_sha256") != request.verifier_source_sha256
            or asr.get("audio_sha256") != request.identity.audio_sha256
            or asr.get("engine") != "mlx-whisper"
            or asr.get("python_sha256") != request.installation.python_sha256
            or asr.get("model_revision") != request.installation.model_directory.name
            or asr.get("model_weights_sha256") != request.installation.weights_sha256
            or asr.get("model_config_sha256") != request.installation.config_sha256
            or asr.get("worker_source_sha256") != request.worker_source_sha256
            or asr.get("decoding")
            != {
                "language": "en",
                "temperature": 0.0,
                "condition_on_previous_text": False,
                "expected_text_prompted": False,
            }
            or report.get("asr") != asr
            or report.get("status") not in {"pass", "review", "fail"}
        ):
            raise SpeechScreenError("Word-screen provenance is inconsistent.")
        return report

    def report(self, run_id: str, attempt_id: str) -> dict[str, Any]:
        attempt = self._attempt(run_id, attempt_id)
        request = self._request(run_id, attempt)
        result = _read_json(attempt / "result.json")
        if file_sha256(attempt / "request.json") != result.get("request_sha256"):
            raise SpeechScreenError("Speech-screen request changed since completion.")
        report = self._validated_report(request, attempt, result)
        return {
            "run_id": run_id,
            "attempt_id": attempt_id,
            "current": self._current(request),
            "report": report,
        }

    def start(self, run_id: str) -> dict[str, Any]:
        run_id = str(UUID(run_id))
        installation = self._installation(full=False)
        with RunLock(self.runs_root / run_id):
            if self.status(run_id)["operation_status"] in {"queued", "running"}:
                raise SpeechScreenError("A speech screen is already active for this run.")
            identity, expected, timeline, _audio = _canonical_inputs(self.store, run_id)
            attempt_id = str(uuid4())
            attempt = self._attempt(run_id, attempt_id)
            attempt.mkdir(parents=True, exist_ok=False)
            worker = runtime_worker_script("whisper_speech_worker.py")
            verifier = Path(__file__).with_name("audio_verification.py")
            coordinator = Path(__file__)
            request = ScreenRequest(
                run_id=UUID(run_id),
                attempt_id=UUID(attempt_id),
                created_at=_now(),
                identity=identity,
                installation=installation,
                installation_record_sha256=file_sha256(self.installation_path),
                worker_source_sha256=file_sha256(worker),
                verifier_source_sha256=file_sha256(verifier),
                coordinator_source_sha256=file_sha256(coordinator),
            )
            for source, name in (
                (worker, "whisper-worker.py"),
                (verifier, "verifier-source.py"),
                (coordinator, "coordinator-source.py"),
            ):
                with (attempt / name).open("xb") as destination:
                    destination.write(source.read_bytes())
                (attempt / name).chmod(0o444)
            (attempt / "expected.txt").write_bytes(expected.encode("utf-8"))
            (attempt / "timeline.json").write_bytes(timeline)
            for name in ("expected.txt", "timeline.json"):
                (attempt / name).chmod(0o444)
            _exclusive_json(attempt / "request.json", request.model_dump(mode="json"))
            _atomic_json(
                attempt / "state.json",
                {
                    "operation_status": "queued",
                    "stage": "queued",
                    "message": "Waiting for the local speech screen.",
                    "pid": None,
                },
            )
            _atomic_json(attempt.parent / "latest.json", {"attempt_id": attempt_id})
            environment = {
                key: value for key, value in os.environ.items() if not key.startswith("WHOOPY_")
            }
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
            environment.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
            command = [
                sys.executable,
                "-B",
                str(attempt / "coordinator-source.py"),
                "--execute",
                run_id,
                "--attempt-id",
                attempt_id,
                "--runs-root",
                str(self.runs_root),
                "--models-root",
                str(self.models_root),
            ]
            _exclusive_json(attempt / "launch.json", {"command": command, "created_at": _now()})
            try:
                with (
                    (attempt / "stdout.log").open("x") as stdout,
                    (attempt / "stderr.log").open("x") as stderr,
                ):
                    self._children[run_id] = self.launcher(
                        command,
                        stdout=stdout,
                        stderr=stderr,
                        env=environment,
                        start_new_session=os.name == "posix",
                    )
            except OSError as error:
                self._finish(
                    request,
                    attempt,
                    "failed",
                    "unverified",
                    f"Could not launch speech screen: {error}",
                    0,
                )
        return self.status(run_id)

    def cancel(self, run_id: str, attempt_id: str) -> dict[str, Any]:
        attempt = self._attempt(run_id, attempt_id)
        self._request(run_id, attempt)
        if not (attempt / "result.json").exists() and not (attempt / "cancel.json").exists():
            with suppress(FileExistsError):
                _exclusive_json(
                    attempt / "cancel.json", {"attempt_id": attempt_id, "requested_at": _now()}
                )
        return self.status(run_id)

    def _finish(
        self,
        request: ScreenRequest,
        attempt: Path,
        operation: str,
        words: str,
        message: str,
        elapsed: float,
        *,
        report: bool = False,
    ) -> None:
        result = {
            "schema_version": 1,
            "run_id": str(request.run_id),
            "attempt_id": str(request.attempt_id),
            "operation_status": operation,
            "word_status": words,
            "message": message,
            "elapsed_seconds": elapsed,
            "finished_at": _now(),
            "policy_id": POLICY,
            "request_sha256": file_sha256(attempt / "request.json"),
            "report_sha256": file_sha256(attempt / "report.json") if report else None,
            "asr_sha256": file_sha256(attempt / "asr.json") if report else None,
        }
        if report:
            self._validated_report(request, attempt, result)
        _exclusive_json(attempt / "result.json", result)
        _atomic_json(attempt / "state.json", {**result, "stage": "finished", "pid": None})
        (attempt / "state.json").chmod(0o444)

    def _run_phase(
        self,
        command: list[str],
        attempt: Path,
        stage: str,
        started: float,
        baseline_swap: int,
        state: dict[str, Any],
    ) -> int:
        process: subprocess.Popen[str] | None = None
        low_since: float | None = None
        phase_started = _now()
        phase_clock = time.monotonic()
        failure: str | None = None
        try:
            if time.monotonic() - started >= TIMEOUT_SECONDS:
                raise _Stopped("Speech screen exceeded its deadline, including GPU-lock wait.")
            if psutil.virtual_memory().available < 4 * GIB:
                raise _Stopped("Speech screening requires at least 4 GiB available RAM.")
            with (
                (attempt / f"{stage}.stdout.log").open("x") as stdout,
                (attempt / f"{stage}.stderr.log").open("x") as stderr,
            ):
                process = subprocess.Popen(
                    command,
                    stdout=stdout,
                    stderr=stderr,
                    text=True,
                    start_new_session=os.name == "posix",
                )
                with (attempt / f"{stage}.resources.jsonl").open("x", buffering=1) as samples:
                    while process.poll() is None:
                        now = time.monotonic()
                        available, swap = (
                            psutil.virtual_memory().available,
                            psutil.swap_memory().used,
                        )
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
                        _atomic_json(
                            attempt / "state.json",
                            {
                                **state,
                                "operation_status": "running",
                                "stage": stage,
                                "message": "Checking spoken words locally.",
                                "elapsed_seconds": now - started,
                                "heartbeat_at": _now(),
                            },
                        )
                        low_since = (
                            (now if low_since is None else low_since)
                            if available < 2 * GIB
                            else None
                        )
                        if (attempt / "cancel.json").exists():
                            raise _Stopped(
                                "Speech screen cancelled; no word verdict was committed.",
                                "cancelled",
                            )
                        if now - started >= TIMEOUT_SECONDS:
                            raise _Stopped(
                                "Speech screen exceeded its deadline, including GPU-lock wait."
                            )
                        if swap - baseline_swap > 4 * GIB or (
                            low_since is not None and now - low_since >= 2
                        ):
                            raise _Stopped(
                                "Speech screen stopped because system memory safety "
                                "limits were reached."
                            )
                        time.sleep(0.5)
                process.wait()
            if (attempt / "cancel.json").exists():
                raise _Stopped(
                    "Speech screen cancelled; no word verdict was committed.", "cancelled"
                )
            if (
                time.monotonic() - started >= TIMEOUT_SECONDS
                or psutil.swap_memory().used - baseline_swap > 4 * GIB
            ):
                raise _Stopped("Speech screen stopped at its deadline or swap-growth safety limit.")
            return process.returncode
        except BaseException as error:
            failure = str(error)
            raise
        finally:
            if process is not None and process.poll() is None:
                _stop_process_tree(process)
            _exclusive_json(
                attempt / f"{stage}.process.json",
                {
                    "command": command,
                    "started_at": phase_started,
                    "finished_at": _now(),
                    "elapsed_seconds": max(0.0, time.monotonic() - phase_clock),
                    "returncode": process.returncode if process is not None else None,
                    "failure": failure,
                },
            )

    def execute(self, run_id: str, attempt_id: str) -> int:
        attempt = self._attempt(run_id, attempt_id)
        request = self._request(run_id, attempt)
        started = time.monotonic()
        lock = RunLock(self.runs_root / run_id)
        slot = HeavyweightModelSlot(self.models_root, "speech-screen-mlx-whisper")
        lock_owned = False
        try:
            while True:
                try:
                    lock.acquire()
                    lock_owned = True
                    break
                except RunLockUnavailable as error:
                    if time.monotonic() - started > 5:
                        raise SpeechScreenError(
                            "The narration is busy; retry after its active operation."
                        ) from error
                    time.sleep(0.05)
            if (attempt / "result.json").exists():
                return 1
            slot.acquire()
            state = {
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "process_created_at": psutil.Process().create_time(),
            }
            _atomic_json(
                attempt / "state.json",
                {
                    **state,
                    "operation_status": "running",
                    "stage": "preflight",
                    "message": "Verifying local speech-screen inputs.",
                },
            )
            if (attempt / "cancel.json").exists():
                raise _Stopped("Speech screen cancelled before inference.", "cancelled")
            if (
                self._installation(full=True) != request.installation
                or file_sha256(self.installation_path) != request.installation_record_sha256
            ):
                raise SpeechScreenError("Speech-screen installation changed before launch.")
            identity, _expected, _timeline, audio = _canonical_inputs(self.store, run_id)
            if identity != request.identity:
                raise SpeechScreenError("Narration or timeline changed before screening.")
            for name, digest in (
                ("whisper-worker.py", request.worker_source_sha256),
                ("verifier-source.py", request.verifier_source_sha256),
                ("coordinator-source.py", request.coordinator_source_sha256),
            ):
                if file_sha256(attempt / name) != digest:
                    raise SpeechScreenError("Speech-screen source snapshot changed before launch.")
            with audio.open("rb") as source, (attempt / "input.wav").open("xb") as destination:
                shutil.copyfileobj(source, destination)
            (attempt / "input.wav").chmod(0o444)
            if file_sha256(attempt / "input.wav") != identity.audio_sha256:
                raise SpeechScreenError("Narration changed while freezing its screening input.")
            baseline = psutil.swap_memory().used
            asr_command = [
                str(request.installation.python_executable),
                "-I",
                "-B",
                str(attempt / "whisper-worker.py"),
                "--audio",
                str(attempt / "input.wav"),
                "--model",
                str(request.installation.model_directory),
                "--output",
                str(attempt / "asr.json"),
                "--weights-sha256",
                request.installation.weights_sha256,
                "--config-sha256",
                request.installation.config_sha256,
                "--python-sha256",
                request.installation.python_sha256,
                "--parent-pid",
                str(os.getpid()),
            ]
            if self._run_phase(asr_command, attempt, "asr", started, baseline, state) != 0:
                raise SpeechScreenError(
                    "Local ASR did not complete; inspect the retained process logs."
                )
            compare_command = [
                sys.executable,
                "-B",
                str(attempt / "coordinator-source.py"),
                "--compare",
                str(attempt),
                "--parent-pid",
                str(os.getpid()),
            ]
            code = self._run_phase(compare_command, attempt, "comparison", started, baseline, state)
            if code not in {0, 1, 2} or not (attempt / "report.json").is_file():
                raise SpeechScreenError(
                    "Word comparison did not complete; inspect the retained logs."
                )
            report = _read_json(attempt / "report.json")
            if (attempt / "cancel.json").exists():
                raise _Stopped(
                    "Speech screen cancelled before committing its verdict.", "cancelled"
                )
            if not self._current(request):
                self._finish(
                    request,
                    attempt,
                    "completed",
                    report["status"],
                    "Narration changed during its word check.",
                    time.monotonic() - started,
                    report=True,
                )
                return 1
            self._finish(
                request,
                attempt,
                "completed",
                report["status"],
                "Automated word screening completed; human listening is not assessed.",
                time.monotonic() - started,
                report=True,
            )
            return code
        except _Stopped as error:
            if lock_owned:
                self._finish(
                    request,
                    attempt,
                    error.status,
                    "unverified",
                    str(error),
                    time.monotonic() - started,
                )
            return 1
        except (Exception, KeyboardInterrupt) as error:
            traceback.print_exc()
            if lock_owned:
                self._finish(
                    request,
                    attempt,
                    "interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                    "unverified",
                    str(error) or type(error).__name__,
                    time.monotonic() - started,
                )
            return 1
        finally:
            slot.release()
            lock.release()


def compare_attempt(attempt: Path, *, parent_pid: int | None = None) -> int:
    if parent_pid is not None:
        parent = psutil.Process(parent_pid)
        direct = psutil.Process(os.getppid())
        if direct.pid != parent.pid and not (
            os.name == "nt"
            and direct.ppid() == parent.pid
            and os.path.samefile(direct.exe(), sys.executable)
            and direct.create_time() <= psutil.Process().create_time()
        ):
            raise SpeechScreenError("Speech-screen coordinator is no longer the parent.")

        def watch_parent() -> None:
            while True:
                time.sleep(0.1)
                if not parent.is_running() or not direct.is_running():
                    os._exit(75)

        threading.Thread(target=watch_parent, daemon=True).start()
    request = ScreenRequest.model_validate(_read_json(attempt / "request.json"))
    for filename, expected in (
        ("input.wav", request.identity.audio_sha256),
        ("expected.txt", request.identity.expected_text_sha256),
        ("verifier-source.py", request.verifier_source_sha256),
    ):
        if file_sha256(attempt / filename) != expected:
            raise SpeechScreenError("Frozen speech-screen input changed before comparison.")
    asr = _read_json(attempt / "asr.json")
    if (
        not isinstance(asr.get("text"), str)
        or asr.get("audio_sha256") != request.identity.audio_sha256
    ):
        raise SpeechScreenError("Independent ASR is unavailable or belongs to different audio.")
    spec = importlib.util.spec_from_file_location(
        "whoopy_screen_verifier", attempt / "verifier-source.py"
    )
    assert spec is not None and spec.loader is not None
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    report = verifier.verify_audio(
        attempt / "input.wav",
        (attempt / "expected.txt").read_text(encoding="utf-8"),
        asr_result=asr,
        mode="narration",
    )
    report["verifier_source_sha256"] = request.verifier_source_sha256
    _exclusive_json(attempt / "report.json", report)
    return {"pass": 0, "review": 2, "fail": 1}[report["status"]]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute")
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--attempt-id")
    parser.add_argument("--runs-root", type=Path)
    parser.add_argument("--models-root", type=Path)
    parser.add_argument("--parent-pid", type=int)
    args = parser.parse_args()
    if args.compare is not None:
        return compare_attempt(args.compare, parent_pid=args.parent_pid)
    if (
        args.execute is None
        or args.attempt_id is None
        or args.runs_root is None
        or args.models_root is None
    ):
        parser.error("An internal execution requires run, attempt and storage roots.")
    if hasattr(signal, "SIGTERM"):

        def interrupted(_signum: int, _frame: Any) -> None:
            raise KeyboardInterrupt("Speech-screen coordinator interrupted.")

        signal.signal(signal.SIGTERM, interrupted)
    return SpeechScreening(args.runs_root, args.models_root).execute(args.execute, args.attempt_id)


if __name__ == "__main__":
    raise SystemExit(main())
