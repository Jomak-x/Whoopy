"""Create a CI-only fresh-wheel launcher receipt without claiming model acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import sysconfig
import tempfile
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-target", required=True)
    parser.add_argument("--wheel-dir", type=Path, default=Path("dist"))
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _offline_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "UV_OFFLINE": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
        }
    )
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        environment.pop(name, None)
    return environment


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run(command: list[str], *, environment: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=environment,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"Command failed ({completed.returncode}): {' '.join(command)}\n"
            f"{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
        )
    return completed.stdout


def _launcher_path() -> Path:
    scripts = Path(sysconfig.get_path("scripts"))
    candidate = scripts / ("whoopy.exe" if os.name == "nt" else "whoopy")
    if not candidate.is_file():
        raise RuntimeError(f"Installed whoopy launcher was not found: {candidate}")
    return candidate


def _smoke_app(
    launcher: Path, *, config_directory: Path, temporary_root: Path, environment: dict[str, str]
) -> tuple[bool, bool]:
    port = _free_loopback_port()
    process = subprocess.Popen(
        [
            str(launcher),
            "app",
            "--no-open",
            "--port",
            str(port),
            "--config-dir",
            str(config_directory),
            "--models-dir",
            str(temporary_root / "models" / "managed"),
            "--runs-dir",
            str(temporary_root / "runs"),
        ],
        cwd=temporary_root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    api_ok = False
    frontend_ok = False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/v1/status", timeout=1) as response:
                    api_ok = response.status == 200
                with opener.open(f"http://127.0.0.1:{port}/", timeout=1) as response:
                    frontend_ok = (
                        response.status == 200 and b"Whoopy Local Studio" in response.read()
                    )
                if api_ok and frontend_ok:
                    break
            except OSError:
                time.sleep(0.1)
        if not api_ok or not frontend_ok:
            stdout, stderr = (
                process.communicate(timeout=1) if process.poll() is not None else ("", "")
            )
            raise RuntimeError(
                "Fresh-wheel local app did not become ready. "
                f"stdout={stdout[-1000:]!r} stderr={stderr[-1000:]!r}"
            )
        return api_ok, frontend_ok
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    output = args.output.resolve()
    wheels = sorted(args.wheel_dir.resolve().glob("whoopy-*.whl"))
    if len(wheels) != 1:
        raise SystemExit(
            f"Expected exactly one Whoopy wheel in {args.wheel_dir}; found {len(wheels)}"
        )
    wheel = wheels[0]
    environment = _offline_environment()
    report: dict[str, Any] = {
        "schema_version": 1,
        "evidence_scope": "ci_portability_not_release_acceptance",
        "expected_target": args.expected_target,
        "generated_at": datetime.now(UTC).isoformat(),
        "wheel_sha256": _sha256(wheel),
        "offline_process_environment": True,
        "os_network_isolation_proven": False,
        "passed": False,
    }
    try:
        # Dependency acquisition already happened in `uv sync`. Reinstall only the built
        # wheel, with no dependency or index access, before testing from an unrelated cwd.
        _run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                sys.executable,
                "--reinstall",
                "--no-deps",
                str(wheel),
            ],
            environment=environment,
        )
        launcher = _launcher_path()
        _run([str(launcher), "--help"], environment=environment)
        doctor = json.loads(_run([str(launcher), "doctor", "--json"], environment=environment))
        snapshot = doctor["snapshot"]
        detected_target = f"{snapshot['operating_system']}-{snapshot['architecture']}"
        if detected_target != args.expected_target:
            raise RuntimeError(
                f"Runner target was {detected_target!r}; matrix expected {args.expected_target!r}."
            )
        with tempfile.TemporaryDirectory(prefix="whoopy-ci-portability-") as temporary:
            api_ok, frontend_ok = _smoke_app(
                launcher,
                config_directory=args.config_dir.resolve(),
                temporary_root=Path(temporary),
                environment=environment,
            )
        report.update(
            {
                "detected_target": detected_target,
                "accelerators": snapshot["accelerators"],
                "launcher_installed": True,
                "api_started": api_ok,
                "bundled_frontend_served": frontend_ok,
                "passed": True,
            }
        )
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"[:4000]
        _write_report(output, report)
        print(output)
        return 1
    _write_report(output, report)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
