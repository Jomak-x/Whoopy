"""Verify a fresh bundled install and localhost launch after acquisition.

The receipt distinguishes package-index isolation, operator-attested host isolation,
and a probed macOS process sandbox. It never promotes CI execution to independent
platform or model-inference acceptance.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import io
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPOSITORY_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if REPOSITORY_IMPORT_ROOT not in sys.path:  # Direct ``python scripts/...`` execution.
    sys.path.insert(0, REPOSITORY_IMPORT_ROOT)

from scripts.install_offline_bundle import (  # noqa: E402
    _launcher,
    _offline_environment,
    _target_id,
    _venv_python,
    verify_bundle,
)
from scripts.localhost_studio_lifecycle import main as studio_lifecycle_main  # noqa: E402
from whoopy.release.network_isolation import MACOS_NETWORK_SANDBOX_PROFILE  # noqa: E402

# Keep the acceptance harness's original public name explicit.  With strict
# ``no_implicit_reexport`` typing, an imported alias is not itself an exported
# module attribute even though it exists at runtime.
MACOS_SANDBOX_PROFILE = MACOS_NETWORK_SANDBOX_PROFILE

MACOS_SANDBOX_ACTIVE = "LOCAL_V1_MACOS_SANDBOX_ACTIVE"
MACOS_SANDBOX_CONTROL = "LOCAL_V1_MACOS_SANDBOX_CONTROL"
EXTERNAL_NETWORK_PROBE_ENDPOINTS = (
    ("1.1.1.1", 443),
    ("8.8.8.8", 443),
    ("9.9.9.9", 443),
)
_MUTABLE_MODEL_PARTS = frozenset({"records", ".active", ".locks"})


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _json_identity(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _readiness_tree_sha256(models_directory: Path) -> str:
    """Hash every mutable model-state file so acceptance can prove no workspace writes."""

    digest = hashlib.sha256()
    if not models_directory.is_dir():
        raise ValueError(f"Managed models directory does not exist: {models_directory}")
    for path in sorted(models_directory.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(models_directory)
        if not any(part in _MUTABLE_MODEL_PARTS for part in relative.parts):
            continue
        digest.update(relative.as_posix().encode("utf-8") + b"\0")
        if path.is_symlink():
            digest.update(b"link\0" + os.readlink(path).encode("utf-8"))
        elif path.is_file():
            digest.update(b"file\0" + _sha256(path).encode("ascii"))
        elif path.is_dir():
            digest.update(b"directory\0")
    return digest.hexdigest()


def _clone_models_for_lifecycle(
    source: Path,
    destination: Path,
    *,
    isolation_root: Path,
) -> Path:
    """Clone Basic/Kokoro state without allowing the lifecycle to mutate its source.

    Heavy optional runtimes are outside this portable Basic-profile proof and
    commonly contain virtualenv interpreter symlinks to host locations.  They
    must not widen or escape the installed lifecycle fixture.
    """

    source = source.expanduser().resolve(strict=True)
    isolation_root = isolation_root.resolve(strict=True)
    destination = destination.resolve(strict=False)
    try:
        destination.relative_to(isolation_root)
    except ValueError as error:
        raise ValueError(
            "Isolated models destination must stay below the lifecycle root."
        ) from error
    if destination == isolation_root or destination.exists() or not source.is_dir():
        raise ValueError("Isolated models destination must be a new child directory.")
    excluded_roots = {".downloads", "quarantine", "runtime-trash", "runtimes"}

    def included(path: Path) -> bool:
        relative = path.relative_to(source)
        return not relative.parts or relative.parts[0] not in excluded_roots

    for link in (path for path in source.rglob("*") if path.is_symlink() and included(path)):
        target = Path(os.readlink(link))
        if target.is_absolute():
            raise ValueError(f"Managed model tree contains an absolute symlink: {link}")
        try:
            link.resolve(strict=True).relative_to(source)
        except (OSError, ValueError) as error:
            raise ValueError(f"Managed model symlink escapes its source tree: {link}") from error

    def copy_file(source_name: str, destination_name: str) -> str:
        source_path = Path(source_name)
        relative = source_path.relative_to(source)
        if any(part in _MUTABLE_MODEL_PARTS for part in relative.parts):
            return shutil.copy2(source_name, destination_name)
        try:
            os.link(source_name, destination_name)
        except OSError as error:
            if error.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
                raise
            return shutil.copy2(source_name, destination_name)
        return destination_name

    shutil.copytree(
        source,
        destination,
        symlinks=True,
        copy_function=copy_file,
        ignore=lambda directory, names: (
            sorted(excluded_roots.intersection(names))
            if Path(directory).resolve() == source
            else []
        ),
    )
    return destination


def _runtime_fingerprint_with_python(
    python: Path,
    models_directory: Path,
    *,
    cwd: Path,
) -> str:
    probe = (
        "from pathlib import Path; "
        "from whoopy.locations import default_config_directory; "
        "from whoopy.model_packs.registry import load_model_pack_registry; "
        "from whoopy.model_packs.resolution import models_root_from_artifact_store; "
        f"models=Path({str(models_directory)!r}); "
        "registry=load_model_pack_registry(default_config_directory()/'model_packs.yaml',"
        "models_root=models_root_from_artifact_store(models)); "
        "print(registry.runtime_fingerprint('kokoro') or '')"
    )
    completed = subprocess.run(
        [str(python), "-I", "-c", probe],
        cwd=cwd,
        env=_offline_environment(),
        check=True,
        capture_output=True,
        text=True,
    )
    fingerprint = completed.stdout.strip()
    if len(fingerprint) != 64 or any(
        character not in "0123456789abcdef" for character in fingerprint
    ):
        raise ValueError("Kokoro runtime fingerprint probe returned an invalid digest.")
    return fingerprint


def _tcp_probe(host: str, port: int, *, timeout: float = 3.0) -> dict[str, Any]:
    started = time.monotonic()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        result = connection.connect_ex((host, port))
    return {
        "host": host,
        "port": port,
        "connect_errno": result,
        "errno_name": errno.errorcode.get(result, "UNKNOWN") if result else None,
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
    }


def _loopback_round_trip_probe() -> dict[str, Any]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        accepted = threading.Event()

        def accept_once() -> None:
            connection, _address = listener.accept()
            connection.close()
            accepted.set()

        thread = threading.Thread(target=accept_once, daemon=True)
        thread.start()
        with socket.create_connection(("127.0.0.1", port), timeout=3):
            pass
        thread.join(timeout=3)
        if not accepted.is_set():
            raise RuntimeError("macOS sandbox loopback round-trip did not complete.")
    return {"host": "127.0.0.1", "port": port, "round_trip_passed": True}


def _macos_reachability_control() -> dict[str, Any]:
    probes = [_tcp_probe(host, port) for host, port in EXTERNAL_NETWORK_PROBE_ENDPOINTS]
    reachable = [probe for probe in probes if probe["connect_errno"] == 0]
    if not reachable:
        raise RuntimeError(
            "Cannot prove macOS sandbox isolation because none of the fixed external TCP "
            "control endpoints was reachable before sandboxing."
        )
    return {
        "schema_version": 1,
        "probes": probes,
        "reachable_endpoints": [
            {"host": probe["host"], "port": probe["port"]} for probe in reachable
        ],
    }


def _verify_macos_sandbox_isolation() -> dict[str, Any]:
    if platform.system() != "Darwin" or os.environ.get(MACOS_SANDBOX_ACTIVE) != "1":
        raise RuntimeError("macOS sandbox isolation verification is not running in its child.")
    raw_control = os.environ.get(MACOS_SANDBOX_CONTROL)
    if not raw_control:
        raise RuntimeError("macOS sandbox reachability control is missing.")
    control = json.loads(raw_control)
    endpoints = control.get("reachable_endpoints")
    if not isinstance(endpoints, list) or not endpoints:
        raise RuntimeError("macOS sandbox reachability control has no reachable endpoint.")
    denied: list[dict[str, Any]] = []
    for endpoint in endpoints:
        if not isinstance(endpoint, dict):
            raise RuntimeError("macOS sandbox reachability control is malformed.")
        probe = _tcp_probe(str(endpoint["host"]), int(endpoint["port"]))
        if probe["connect_errno"] not in {errno.EPERM, errno.EACCES}:
            raise RuntimeError(
                "External TCP was not denied by the macOS sandbox: "
                f"{probe['host']}:{probe['port']} returned {probe['connect_errno']}."
            )
        denied.append(probe)
    profile_sha256 = hashlib.sha256(MACOS_SANDBOX_PROFILE.encode("utf-8")).hexdigest()
    return {
        "schema_version": 1,
        "control_kind": "pre_sandbox_reachability_then_in_sandbox_denial",
        "sandbox_executable": "/usr/bin/sandbox-exec",
        "sandbox_profile_sha256": profile_sha256,
        "external_control": control,
        "external_denial_probes": denied,
        "loopback_probe": _loopback_round_trip_probe(),
        "external_denial_errno_names": sorted({str(probe["errno_name"]) for probe in denied}),
    }


def _run_in_macos_network_sandbox(argv: list[str], *, script: Path | None = None) -> int:
    if platform.system() != "Darwin":
        raise ValueError("--macos-sandbox-network-isolation is available only on macOS.")
    sandbox = Path("/usr/bin/sandbox-exec")
    if not sandbox.is_file():
        raise ValueError("This macOS installation does not provide /usr/bin/sandbox-exec.")
    control = _macos_reachability_control()
    environment = os.environ.copy()
    environment[MACOS_SANDBOX_ACTIVE] = "1"
    environment[MACOS_SANDBOX_CONTROL] = json.dumps(control, sort_keys=True, separators=(",", ":"))
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", prefix="whoopy-network-", suffix=".sb"
    ) as profile:
        profile.write(MACOS_SANDBOX_PROFILE)
        profile.flush()
        completed = subprocess.run(
            [
                str(sandbox),
                "-f",
                profile.name,
                sys.executable,
                str((script or Path(__file__)).resolve()),
                *argv,
            ],
            env=environment,
            check=False,
        )
    return completed.returncode


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _validate_install_receipt(receipt: object, manifest: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(receipt, dict):
        raise ValueError("Offline installer receipt must be a JSON object.")
    expected = {
        "schema_version": 2,
        "bundle_id": manifest["bundle_id"],
        "bundle_target": manifest["target"],
        "bundle_python": manifest["python"],
        "product_version": manifest["product_version"],
        "product_wheel_sha256": manifest["product_wheel_sha256"],
        "bundle_integrity_verified": True,
        "bundle_authenticity_verified": False,
        "package_index_isolation_enforced": True,
        "process_network_isolation_enforced": False,
        "dependency_consistency_check_passed": True,
        "launcher_smoke_passed": True,
    }
    mismatches = {
        key: {"expected": value, "actual": receipt.get(key)}
        for key, value in expected.items()
        if receipt.get(key) != value
    }
    if mismatches:
        raise ValueError(f"Offline installer receipt is inconsistent: {mismatches}")
    return receipt


def _probe_installed_interpreter(prefix: Path, manifest: dict[str, Any]) -> dict[str, str]:
    probe = (
        "import json,platform,sys; "
        "machine={'amd64':'x86_64','x86_64':'x86_64','arm64':'arm64','aarch64':'arm64'}"
        ".get(platform.machine().lower(),platform.machine().lower()); "
        "os_name={'windows':'windows','darwin':'darwin','linux':'linux'}"
        ".get(platform.system().lower(),platform.system().lower()); "
        "print(json.dumps({'python':f'{sys.version_info.major}.{sys.version_info.minor}',"
        "'implementation':platform.python_implementation(),'target':f'{os_name}-{machine}'}))"
    )
    completed = subprocess.run(
        [str(_venv_python(prefix)), "-I", "-c", probe],
        check=True,
        capture_output=True,
        text=True,
        env=_offline_environment(),
    )
    payload = json.loads(completed.stdout)
    expected = {
        "python": manifest["python"],
        "implementation": "CPython",
        "target": manifest["target"],
    }
    if payload != expected:
        raise ValueError(f"Installed interpreter identity mismatch: {payload!r} != {expected!r}")
    return expected


def _prepare_installed_kokoro(
    opener: urllib.request.OpenerDirector,
    base_url: str,
) -> dict[str, Any]:
    """Run the installed runtime's real smoke before its first Studio generation."""

    request = urllib.request.Request(
        f"{base_url}/api/v1/model-pack-tasks/kokoro/smoke-test",
        data=b"{}",
        headers={"Content-Type": "application/json", "Origin": base_url},
        method="POST",
    )
    with opener.open(request, timeout=10) as response:
        started = json.loads(response.read())
    task_id = started.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise RuntimeError("Installed Kokoro smoke did not return a task ID.")
    deadline = time.monotonic() + 180
    task: dict[str, Any] = {}
    while time.monotonic() < deadline:
        with opener.open(f"{base_url}/api/v1/model-pack-tasks/{task_id}", timeout=10) as response:
            task = json.loads(response.read())
        if task.get("status") == "completed":
            break
        if task.get("status") == "failed":
            raise RuntimeError(f"Installed Kokoro production smoke failed: {task.get('error')}")
        time.sleep(0.25)
    else:
        raise RuntimeError("Installed Kokoro production smoke timed out.")
    if (task.get("result") or {}).get("state") != "ready":
        raise RuntimeError("Installed Kokoro smoke completed without READY evidence.")
    with opener.open(f"{base_url}/api/v1/model-packs/kokoro/sample", timeout=30) as response:
        sample = response.read()
    with wave.open(io.BytesIO(sample), "rb") as source:
        frames = source.getnframes()
        sample_rate = source.getframerate()
        channels = source.getnchannels()
    if frames <= 0 or sample_rate <= 0 or channels != 1:
        raise RuntimeError("Installed Kokoro smoke sample is not playable mono PCM.")
    return {
        "kokoro_production_smoke_passed": True,
        "kokoro_smoke_task_id": task_id,
        "kokoro_sample_bytes": len(sample),
        "kokoro_sample_frames": frames,
        "kokoro_sample_rate": sample_rate,
    }


def _launch_and_probe(
    launcher: Path,
    root: Path,
    environment: dict[str, str],
    *,
    models_directory: Path | None = None,
    lifecycle_output: Path | None = None,
) -> dict[str, Any]:
    port = _free_port()
    process = subprocess.Popen(
        [
            str(launcher),
            "app",
            "--no-open",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--models-dir",
            str(models_directory or root / "product-data/models/managed"),
            "--runs-dir",
            str(root / "product-data/runs"),
        ],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    health_payload: dict[str, Any] | None = None
    status_payload: dict[str, Any] | None = None
    frontend = b""
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline and process.poll() is None:
            try:
                with opener.open(f"http://127.0.0.1:{port}/api/v1/health", timeout=1) as response:
                    health_payload = json.loads(response.read())
                if health_payload:
                    break
            except (OSError, json.JSONDecodeError):
                time.sleep(0.1)
        if health_payload is not None:
            # First-run status intentionally inspects installed model trees and
            # can take materially longer than the lightweight health probe.
            # Never issue overlapping one-second status requests: abandoned
            # server threads would repeat the same disk work and can prevent a
            # healthy installation from ever completing its first inspection.
            with opener.open(f"http://127.0.0.1:{port}/api/v1/status", timeout=120) as response:
                status_payload = json.loads(response.read())
            with opener.open(f"http://127.0.0.1:{port}/", timeout=10) as response:
                frontend = response.read()
        if (
            health_payload is None
            or status_payload is None
            or b"Whoopy Local Studio" not in frontend
        ):
            stdout, stderr = (
                process.communicate(timeout=1) if process.poll() is not None else ("", "")
            )
            raise RuntimeError(
                "Installed application did not serve its API and bundled frontend; "
                f"stdout={stdout[-1500:]!r}, stderr={stderr[-1500:]!r}"
            )
        evidence = {
            "api_started": True,
            "api_version": health_payload.get("api_version"),
            "environment_status_loaded": bool(status_payload),
            "bundled_frontend_served": True,
            "installed_defaults_resolved_without_checkout": True,
            "bound_host": "127.0.0.1",
        }
        if lifecycle_output is not None:
            evidence.update(_prepare_installed_kokoro(opener, f"http://127.0.0.1:{port}"))
            lifecycle_return_code = studio_lifecycle_main(
                [
                    "--base-url",
                    f"http://127.0.0.1:{port}",
                    "--output",
                    str(lifecycle_output),
                ]
            )
            lifecycle = json.loads(lifecycle_output.read_text(encoding="utf-8"))
            if lifecycle_return_code != 0 or lifecycle.get("status") != "passed":
                raise RuntimeError(
                    "Installed Studio lifecycle did not pass: "
                    f"return_code={lifecycle_return_code}, status={lifecycle.get('status')!r}"
                )
            evidence.update(
                {
                    "studio_lifecycle_exercised": True,
                    "studio_lifecycle_passed": True,
                    "studio_lifecycle_receipt": str(lifecycle_output.resolve()),
                    "studio_lifecycle_receipt_sha256": _sha256(lifecycle_output),
                    "studio_lifecycle_evidence_sha256": _json_identity(lifecycle),
                    "studio_lifecycle_evidence": lifecycle,
                }
            )
        return evidence
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def run_acceptance(
    *,
    bundle: Path,
    output: Path,
    network_disabled: bool,
    network_isolation_note: str | None,
    network_isolation_evidence: dict[str, Any] | None = None,
    models_directory: Path | None = None,
    lifecycle_output: Path | None = None,
) -> dict[str, Any]:
    """Install into an empty environment and probe from an unrelated directory."""

    if network_disabled and not network_isolation_note:
        raise ValueError("--network-disabled requires --network-isolation-note")
    if network_disabled and network_isolation_evidence is not None:
        raise ValueError("Operator attestation and enforced process isolation are exclusive.")
    enforced_isolation = network_isolation_evidence is not None
    network_isolation_verified = network_disabled or enforced_isolation
    manifest = verify_bundle(bundle)
    report: dict[str, Any] = {
        "schema_version": 1,
        "evidence_scope": "fresh_application_bundle_offline_after_acquisition",
        "generated_at": datetime.now(UTC).isoformat(),
        "target": manifest["target"],
        "product_version": manifest["product_version"],
        "python": manifest["python"],
        "bundle_id": manifest["bundle_id"],
        "product_wheel_sha256": manifest["product_wheel_sha256"],
        "dependency_lock_sha256": manifest["lock_sha256"],
        "package_indexes_disabled": True,
        "package_index_isolation_enforced": True,
        "process_network_isolation_enforced": enforced_isolation,
        "operator_attested_network_disabled_during_test": network_disabled,
        "network_isolation_evidence_kind": (
            "macos_sandbox_exec"
            if enforced_isolation
            else "operator_attestation"
            if network_disabled
            else "none"
        ),
        "network_isolation_note": (
            "macOS sandbox-exec denied every pre-reachable fixed external TCP control "
            "while permitting a 127.0.0.1 round trip."
            if enforced_isolation
            else network_isolation_note
        ),
        "network_isolation_evidence": network_isolation_evidence,
        "independent_platform_evidence": False,
        "voice_synthesis_tested": False,
        "llm_prompt_generation_tested": False,
        "studio_lifecycle_requested": lifecycle_output is not None,
        "studio_lifecycle_exercised": False,
        "studio_lifecycle_passed": False,
        "offline_launch_passed": False,
        "packaging_passed": False,
        "offline_acceptance_passed": False,
        "local_v1_release_ready": False,
    }
    source_models = (
        models_directory.expanduser().resolve(strict=True) if models_directory is not None else None
    )
    readiness_before = _readiness_tree_sha256(source_models) if source_models is not None else None
    if readiness_before is not None:
        report["workspace_model_readiness_sha256_before"] = readiness_before
    try:
        with tempfile.TemporaryDirectory(prefix="whoopy-fresh-offline-") as temporary:
            root = Path(temporary).resolve()
            prefix = root / "installation"
            isolated_models = (
                _clone_models_for_lifecycle(
                    source_models,
                    root / "product-data/models/managed",
                    isolation_root=root,
                )
                if source_models is not None
                else None
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    str(bundle.resolve() / "install.py"),
                    "--bundle",
                    str(bundle.resolve()),
                    "--prefix",
                    str(prefix),
                ],
                cwd=root,
                env=_offline_environment(),
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Offline installer failed ({completed.returncode}): "
                    f"{completed.stdout[-2000:]} {completed.stderr[-2000:]}"
                )
            receipt_path = prefix / ".whoopy-offline-install.json"
            receipt = _validate_install_receipt(
                json.loads(receipt_path.read_text(encoding="utf-8")), manifest
            )
            report["install_receipt_sha256"] = _sha256(receipt_path)
            report["install_receipt"] = receipt
            report["installed_interpreter"] = _probe_installed_interpreter(prefix, manifest)
            if report["installed_interpreter"]["target"] != _target_id():
                raise ValueError("Installed interpreter target differs from the acceptance host.")
            if isolated_models is not None:
                assert source_models is not None
                source_fingerprint = _runtime_fingerprint_with_python(
                    Path(sys.executable), source_models, cwd=Path(REPOSITORY_IMPORT_ROOT)
                )
                installed_fingerprint = _runtime_fingerprint_with_python(
                    _venv_python(prefix), isolated_models, cwd=root
                )
                report.update(
                    {
                        "source_kokoro_runtime_fingerprint": source_fingerprint,
                        "installed_kokoro_runtime_fingerprint": installed_fingerprint,
                        "runtime_fingerprint_match": source_fingerprint == installed_fingerprint,
                        "isolated_models_directory_used": True,
                    }
                )
                if source_fingerprint != installed_fingerprint:
                    raise ValueError(
                        "Installed and source Kokoro runtime fingerprints do not match."
                    )
            report.update(
                _launch_and_probe(
                    _launcher(prefix),
                    root,
                    _offline_environment(),
                    models_directory=isolated_models,
                    lifecycle_output=lifecycle_output,
                )
            )
        report["packaging_passed"] = True
        report["offline_launch_passed"] = network_isolation_verified
        report["offline_acceptance_passed"] = network_isolation_verified and bool(
            report["studio_lifecycle_passed"]
        )
        report["voice_synthesis_tested"] = bool(report["studio_lifecycle_passed"])
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"[:4000]
    if source_models is not None and readiness_before is not None:
        readiness_after = _readiness_tree_sha256(source_models)
        report["workspace_model_readiness_sha256_after"] = readiness_after
        report["workspace_model_readiness_unchanged"] = readiness_after == readiness_before
        if readiness_after != readiness_before:
            report["packaging_passed"] = False
            report["offline_acceptance_passed"] = False
            report["error"] = "Workspace model readiness changed during isolated acceptance."
    _write(output, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--bundle", type=Path)
    source.add_argument(
        "--bundle-parent",
        type=Path,
        help="Directory containing exactly one unpacked application bundle.",
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--network-disabled",
        action="store_true",
        help="Assert that the operator disabled OS networking for the complete test.",
    )
    parser.add_argument(
        "--macos-sandbox-network-isolation",
        action="store_true",
        help=(
            "Re-execute the complete acceptance lifecycle under a macOS process sandbox "
            "that denies external networking but permits localhost, with before/after probes."
        ),
    )
    parser.add_argument(
        "--network-isolation-note",
        help="Describe the externally observed firewall, adapter, or isolated-host control.",
    )
    parser.add_argument(
        "--models-dir",
        type=Path,
        help="Acquired managed model directory used by an optional full Studio lifecycle.",
    )
    parser.add_argument(
        "--lifecycle-output",
        type=Path,
        help="Exercise generate/play/edit/export/delete and write its receipt here.",
    )
    args = parser.parse_args(argv)
    if args.network_disabled and args.macos_sandbox_network_isolation:
        parser.error(
            "--network-disabled and --macos-sandbox-network-isolation are mutually exclusive"
        )
    if args.network_isolation_note and not args.network_disabled:
        parser.error("--network-isolation-note requires --network-disabled")
    if (args.models_dir is None) != (args.lifecycle_output is None):
        parser.error("--models-dir and --lifecycle-output must be supplied together")
    if args.macos_sandbox_network_isolation and os.environ.get(MACOS_SANDBOX_ACTIVE) != "1":
        child_argv = list(argv if argv is not None else sys.argv[1:])
        try:
            return _run_in_macos_network_sandbox(child_argv)
        except (OSError, RuntimeError, ValueError) as error:
            parser.error(str(error))
    network_isolation_evidence = None
    if args.macos_sandbox_network_isolation:
        try:
            network_isolation_evidence = _verify_macos_sandbox_isolation()
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            parser.error(str(error))
    bundle = args.bundle
    if bundle is None:
        candidates = sorted(
            path.parent for path in args.bundle_parent.glob("*/bundle-manifest.json")
        )
        if len(candidates) != 1:
            parser.error(
                f"--bundle-parent must contain exactly one unpacked bundle; found {len(candidates)}"
            )
        bundle = candidates[0]
    try:
        report = run_acceptance(
            bundle=bundle,
            output=args.output,
            network_disabled=args.network_disabled,
            network_isolation_note=args.network_isolation_note,
            network_isolation_evidence=network_isolation_evidence,
            models_directory=args.models_dir,
            lifecycle_output=args.lifecycle_output,
        )
    except ValueError as error:
        parser.error(str(error))
    print(args.output.resolve())
    return 0 if report["packaging_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
