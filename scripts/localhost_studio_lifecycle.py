"""Exercise a real running Studio through localhost and write durable lifecycle evidence."""

from __future__ import annotations

import argparse
import io
import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def validated_base_url(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("Studio lifecycle acceptance requires a plain localhost HTTP base URL.")
    return value.rstrip("/")


class LocalStudioClient:
    def __init__(self, base_url: str) -> None:
        self.base_url = validated_base_url(base_url)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> tuple[int, bytes, str]:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json", "Origin": self.base_url}
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.status, response.read(), response.headers.get_content_type()
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(
                f"{method} {path} failed with HTTP {error.code}: {detail}"
            ) from error

    def json(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        status, body, _content_type = self.request(method, path, payload)
        if not 200 <= status < 300:
            raise RuntimeError(f"{method} {path} returned HTTP {status}.")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise RuntimeError(f"{method} {path} did not return a JSON object.")
        return value


def _wait_for_completion(
    client: LocalStudioClient,
    task_id: str,
    *,
    timeout_seconds: float,
    allow_editor_revision_resume: bool = False,
    allow_recovery_command_handoff: bool = False,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.json("GET", f"/api/v1/tasks/{task_id}")
        status = last.get("status")
        if status == "completed":
            return last
        interruption_kind = (last.get("execution") or {}).get("interruption_kind")
        if status == "interrupted" and (
            (allow_editor_revision_resume and interruption_kind == "editor_revision")
            or allow_recovery_command_handoff
        ):
            # Revision activation is intentionally durable before the native
            # worker is launched. The task can therefore be observed in this
            # short hand-off state even though automatic selective rendering
            # is already scheduled.
            time.sleep(0.5)
            continue
        if status in {"failed", "cancelled", "interrupted", "missing"}:
            raise RuntimeError(f"Studio task ended as {status}: {last}")
        time.sleep(0.5)
    raise RuntimeError(f"Studio task did not complete within {timeout_seconds:g}s: {last}")


def _wait_for_interruption(
    client: LocalStudioClient,
    task_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait until a browser cancellation has become durably resumable."""

    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.json("GET", f"/api/v1/tasks/{task_id}")
        status = last.get("status")
        if status == "interrupted":
            return last
        if status in {"completed", "failed", "cancelled", "missing"}:
            raise RuntimeError(
                f"Studio cancellation did not leave resumable work; task ended as {status}: {last}"
            )
        time.sleep(0.25)
    raise RuntimeError(
        f"Studio cancellation did not become resumable within {timeout_seconds:g}s: {last}"
    )


def _wait_for_resumable_script_progress(
    client: LocalStudioClient,
    task_id: str,
    *,
    timeout_seconds: float,
) -> dict[str, Any]:
    """Wait until immutable authored inputs exist before forcing cancellation."""

    deadline = time.monotonic() + timeout_seconds
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = client.json("GET", f"/api/v1/tasks/{task_id}")
        if last.get("status") == "running" and last.get("source_kind") == "script_file":
            return last
        if last.get("status") in {"completed", "failed", "cancelled", "missing"}:
            raise RuntimeError(
                f"Studio run stopped before immutable authored input became resumable: {last}"
            )
        time.sleep(0.1)
    raise RuntimeError(
        f"Studio run did not reach resumable authored work within {timeout_seconds:g}s: {last}"
    )


def _audio_evidence(payload: bytes) -> dict[str, int]:
    with wave.open(io.BytesIO(payload), "rb") as source:
        frames = source.getnframes()
        sample_rate = source.getframerate()
        channels = source.getnchannels()
    if frames <= 0 or sample_rate <= 0 or channels != 1:
        raise RuntimeError("Studio playback WAV is empty or not valid mono PCM.")
    return {"bytes": len(payload), "frames": frames, "sample_rate": sample_rate}


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=900)
    parser.add_argument("--voice", default="af_heart")
    args = parser.parse_args(argv)
    output = args.output.resolve()
    report: dict[str, Any] = {
        "schema_version": 1,
        "evidence_scope": "localhost_studio_lifecycle_not_cross_platform_release",
        "generated_at": datetime.now(UTC).isoformat(),
        "base_url": args.base_url,
        "status": "failed",
        "local_v1_release_ready": False,
        "steps": [],
    }
    run_id: str | None = None
    resume_run_id: str | None = None
    try:
        client = LocalStudioClient(args.base_url)
        health = client.json("GET", "/api/v1/health")
        report["steps"].append({"step": "health", "passed": health.get("ok") is True})
        models = client.json("GET", "/api/v1/speech-models")
        kokoro = models.get("speech_models", {}).get("kokoro", {})
        if not isinstance(kokoro, dict) or kokoro.get("ready") is not True:
            report.update(
                {
                    "status": "not_run",
                    "blocker": "Kokoro is not ready on this running Studio.",
                }
            )
            _write(output, report)
            print(output)
            return 2
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            report.update(
                {
                    "status": "not_run",
                    "blocker": "FFmpeg and FFprobe are required for the export lifecycle.",
                }
            )
            _write(output, report)
            print(output)
            return 2

        started = client.json(
            "POST",
            "/api/v1/generate",
            {
                "mode": "script",
                "surface": "studio",
                "profile": "basic",
                "tts_model": "kokoro",
                "voice": args.voice,
                "speed": 0.8,
                "text": (
                    "[delivery: calm]\nTake a comfortable breath and notice the support "
                    "beneath you.\n\n[pause: 2s]\n\nGently return your attention to the room."
                ),
            },
        )
        run_id = str(started["run_id"])
        task_id = str(started["task_id"])
        completed = _wait_for_completion(client, task_id, timeout_seconds=args.timeout_seconds)
        report["steps"].append(
            {"step": "generate", "passed": True, "run_id": run_id, "status": completed["status"]}
        )

        _status, audio, _content_type = client.request("GET", f"/api/v1/runs/{run_id}/audio")
        report["steps"].append({"step": "playback", "passed": True, **_audio_evidence(audio)})

        timeline = client.json("GET", f"/api/v1/runs/{run_id}/artifact/timeline")
        silence = next(
            (segment for segment in timeline["segments"] if segment.get("type") == "SILENCE"),
            None,
        )
        if not isinstance(silence, dict):
            raise RuntimeError("Generated lifecycle timeline contains no editable silence event.")
        edited_duration = min(int(silence["duration_ms"]) + 500, 600_000)
        revision = client.json(
            "POST",
            f"/api/v1/runs/{run_id}/revisions",
            {
                "timeline": {"silence_duration_ms": {str(silence["id"]): edited_duration}},
                "note": "Local V1 automated lifecycle silence edit.",
            },
        )
        revision_task = revision.get("task")
        if not isinstance(revision_task, dict):
            raise RuntimeError("Studio edit did not return its assembly task.")
        _wait_for_completion(
            client,
            str(revision_task["task_id"]),
            timeout_seconds=args.timeout_seconds,
            allow_editor_revision_resume=True,
        )
        report["steps"].append(
            {
                "step": "edit",
                "passed": True,
                "revision_id": revision["revision"]["revision_id"],
                "silence_duration_ms": edited_duration,
            }
        )

        exports = client.json("POST", f"/api/v1/runs/{run_id}/exports", {})
        exported: dict[str, int] = {}
        for kind in ("wav", "flac", "aac", "opus"):
            _status, payload, _content_type = client.request(
                "GET", f"/api/v1/runs/{run_id}/exports/{kind}"
            )
            if not payload:
                raise RuntimeError(f"Studio {kind} export is empty.")
            exported[kind] = len(payload)
        report["steps"].append(
            {
                "step": "export",
                "passed": True,
                "files": exported,
                "manifest_schema_version": exports.get("schema_version"),
            }
        )

        # Exercise the same point-and-click cancellation/resume API exposed by
        # Studio. This second run is intentionally interrupted immediately so
        # the receipt proves a fresh user can continue durable work without a
        # terminal command or private filesystem mutation.
        resume_script = "\n\n".join(
            (
                f"Notice breath {index + 1} with patience, then soften the shoulders "
                "and return gently to the support beneath you."
            )
            for index in range(36)
        )
        resumable = client.json(
            "POST",
            "/api/v1/generate",
            {
                "mode": "script",
                "surface": "studio",
                "profile": "basic",
                "tts_model": "kokoro",
                "voice": args.voice,
                "speed": 0.8,
                "text": resume_script,
            },
        )
        resume_run_id = str(resumable["run_id"])
        resume_task_id = str(resumable["task_id"])
        _wait_for_resumable_script_progress(
            client,
            resume_task_id,
            timeout_seconds=min(args.timeout_seconds, 120),
        )
        client.json("POST", f"/api/v1/runs/{resume_run_id}/cancel", {})
        interrupted = _wait_for_interruption(
            client,
            resume_task_id,
            timeout_seconds=min(args.timeout_seconds, 120),
        )
        resumed = client.json("POST", f"/api/v1/runs/{resume_run_id}/resume", {})
        resumed_task = _wait_for_completion(
            client,
            str(resumed["task_id"]),
            timeout_seconds=args.timeout_seconds,
            allow_recovery_command_handoff=True,
        )
        recovery = resumed_task.get("recovery")
        if not isinstance(recovery, dict) or int(recovery.get("resume_count", 0)) < 1:
            raise RuntimeError("Studio resume did not record a durable resume attempt.")
        _status, resumed_audio, _content_type = client.request(
            "GET", f"/api/v1/runs/{resume_run_id}/audio"
        )
        report["steps"].append(
            {
                "step": "resume",
                "passed": True,
                "run_id": resume_run_id,
                "interruption_kind": (interrupted.get("execution") or {}).get("interruption_kind"),
                "resume_count": recovery["resume_count"],
                **_audio_evidence(resumed_audio),
            }
        )
        resumed_deletion = client.json("DELETE", f"/api/v1/runs/{resume_run_id}")
        if resumed_deletion.get("recoverable") is not True:
            raise RuntimeError("Resumed lifecycle run was not recoverably deleted.")

        deletion = client.json("DELETE", f"/api/v1/runs/{run_id}")
        if deletion.get("recoverable") is not True:
            raise RuntimeError("Studio deletion did not produce a recoverable trash receipt.")
        report["steps"].append(
            {
                "step": "delete",
                "passed": True,
                "recoverable": True,
                "receipt_id": deletion.get("receipt_id"),
            }
        )
        report["status"] = "passed"
        report["run_id"] = run_id
        _write(output, report)
        print(output)
        return 0
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"[:4000]
        if run_id is not None:
            report["preserved_failed_run_id"] = run_id
        if resume_run_id is not None:
            report["preserved_resume_run_id"] = resume_run_id
        _write(output, report)
        print(output)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
