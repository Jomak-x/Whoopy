"""Install a verified Whoopy application bundle without contacting a package index.

This file is copied into every target-specific bundle and deliberately uses only
the Python standard library.  Run it with the same Python 3.11/3.12 family named
in ``bundle-manifest.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any, cast
from uuid import uuid4

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SUPPORTED_TARGETS = {
    "windows-x86_64",
    "darwin-arm64",
    "darwin-x86_64",
    "linux-x86_64",
    "linux-arm64",
}
_SUPPORTED_PYTHON_FAMILIES = {"3.11", "3.12"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _target_id() -> str:
    operating_system = platform.system().lower()
    machine = platform.machine().lower()
    os_name = {"windows": "windows", "darwin": "darwin", "linux": "linux"}.get(
        operating_system, operating_system
    )
    architecture = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine)
    return f"{os_name}-{architecture}"


def _safe_member(root: Path, relative: str) -> Path:
    logical = PurePosixPath(relative)
    if (
        logical.is_absolute()
        or not logical.parts
        or any(part in {"", ".", ".."} for part in logical.parts)
    ):
        raise ValueError(f"Unsafe bundle member path: {relative!r}")
    resolved = root.joinpath(*logical.parts).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"Bundle member escapes its root: {relative!r}") from error
    return resolved


def _is_reparse_point(path: Path) -> bool:
    """Return true for symlinks and Windows junctions/reparse points."""

    try:
        status = path.lstat()
    except OSError:
        return False
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(status, "st_file_attributes", 0)
    return stat.S_ISLNK(status.st_mode) or bool(file_attributes & reparse_flag)


def _assert_regular_tree(root: Path) -> None:
    """Reject links, junctions, sockets, devices, and paths escaping ``root``."""

    if _is_reparse_point(root):
        raise ValueError(f"Bundle root may not be a link or reparse point: {root}")
    resolved_root = root.resolve(strict=True)
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in [*directory_names, *file_names]:
            path = base / name
            if _is_reparse_point(path):
                raise ValueError(f"Bundle contains a link or reparse point: {path}")
            try:
                path.resolve(strict=True).relative_to(resolved_root)
            except (OSError, ValueError) as error:
                raise ValueError(f"Bundle member escapes its root: {path}") from error
            mode = path.lstat().st_mode
            if name in directory_names:
                if not stat.S_ISDIR(mode):
                    raise ValueError(f"Bundle member is not a regular directory: {path}")
            elif not stat.S_ISREG(mode):
                raise ValueError(f"Bundle member is not a regular file: {path}")


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"Manifest field {field!r} must be a lowercase SHA-256 digest.")
    return value


def _validate_requirements(path: Path) -> None:
    """Forbid requirement sources that could bypass the bundled wheelhouse."""

    text = path.read_text(encoding="utf-8")
    forbidden = re.compile(
        r"(?:https?|ftp|file)://|\s@\s|(?:^|\s)--(?:index-url|extra-index-url|find-links|"
        r"trusted-host|editable|requirement|constraint)\b",
        re.IGNORECASE | re.MULTILINE,
    )
    if forbidden.search(text):
        raise ValueError("requirements.txt contains a non-wheelhouse package source or option.")
    saw_requirement = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("--hash=sha256:"):
            continue
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*==[^\s\\;]+(?:\s*;.*)?(?:\s*\\)?$", stripped):
            saw_requirement = True
            continue
        raise ValueError(f"requirements.txt contains an unsupported line: {stripped!r}")
    if not saw_requirement:
        raise ValueError("requirements.txt contains no pinned dependencies.")


def verify_bundle(
    bundle_root: Path, *, enforce_runtime_compatibility: bool = True
) -> dict[str, Any]:
    """Verify target, interpreter, inventory, size, and SHA-256 before installation."""

    if not bundle_root.exists():
        raise ValueError(f"Bundle root does not exist: {bundle_root}")
    _assert_regular_tree(bundle_root)
    root = bundle_root.resolve(strict=True)
    manifest_path = root / "bundle-manifest.json"
    if not manifest_path.is_file() or _is_reparse_point(manifest_path):
        raise ValueError("Bundle manifest is missing or not a regular file.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Bundle manifest is not valid UTF-8 JSON.") from error
    if not isinstance(manifest, dict):
        raise ValueError("Bundle manifest must be a JSON object.")
    if manifest.get("schema_version") != 1:
        raise ValueError("Unsupported or missing application bundle schema version.")
    if manifest.get("evidence_scope") != "whoopy_application_offline_bundle":
        raise ValueError("Manifest is not a Whoopy application offline bundle.")
    expected_target = manifest.get("target")
    if expected_target not in _SUPPORTED_TARGETS:
        raise ValueError(f"Bundle declares an unsupported target: {expected_target!r}.")
    if enforce_runtime_compatibility and expected_target != _target_id():
        raise ValueError(
            f"Bundle target is {expected_target!r}, but this machine is {_target_id()!r}."
        )
    expected_python = manifest.get("python")
    if expected_python not in _SUPPORTED_PYTHON_FAMILIES:
        raise ValueError(f"Bundle declares an unsupported Python family: {expected_python!r}.")
    if enforce_runtime_compatibility and platform.python_implementation() != "CPython":
        raise ValueError("Whoopy offline bundles require CPython.")
    current_python = f"{sys.version_info.major}.{sys.version_info.minor}"
    if enforce_runtime_compatibility and expected_python != current_python:
        raise ValueError(
            f"Bundle requires Python {expected_python}; installer is using {current_python}."
        )
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("Bundle manifest has no file inventory.")
    declared: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Bundle file inventory contains a non-object entry.")
        relative = entry.get("path")
        if not isinstance(relative, str) or relative in declared:
            raise ValueError(f"Duplicate or invalid bundle path: {relative!r}")
        declared.add(relative)
        path = _safe_member(root, relative)
        if _is_reparse_point(path) or not path.is_file():
            raise ValueError(f"Bundle member is missing or not a regular file: {relative}")
        size = entry.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise ValueError(f"Bundle member has an invalid declared size: {relative}")
        if path.stat().st_size != size:
            raise ValueError(f"Bundle member size mismatch: {relative}")
        expected_digest = _require_sha256(entry.get("sha256"), field=f"files[{relative}].sha256")
        if _sha256(path) != expected_digest:
            raise ValueError(f"Bundle member checksum mismatch: {relative}")
    actual = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path != manifest_path
    }
    if actual != declared:
        extra = sorted(actual - declared)
        missing = sorted(declared - actual)
        raise ValueError(f"Bundle inventory mismatch; extra={extra}, missing={missing}.")
    identity = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if _require_sha256(manifest.get("bundle_id"), field="bundle_id") != identity:
        raise ValueError("Bundle content identity does not match its file inventory.")
    _require_sha256(manifest.get("lock_sha256"), field="lock_sha256")
    _require_sha256(manifest.get("product_wheel_sha256"), field="product_wheel_sha256")
    product_wheel = manifest.get("product_wheel")
    product_version = manifest.get("product_version")
    if (
        not isinstance(product_version, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,63}", product_version) is None
    ):
        raise ValueError("Bundle declares an invalid product version.")
    if (
        not isinstance(product_wheel, str)
        or not product_wheel.startswith("wheelhouse/")
        or not product_wheel.endswith(".whl")
    ):
        raise ValueError("Product wheel path must name a wheel inside wheelhouse/.")
    if not Path(product_wheel).name.startswith(f"whoopy-{product_version}-"):
        raise ValueError("Product wheel filename does not match the declared product version.")
    matching_product = next(
        (entry for entry in entries if entry.get("path") == product_wheel), None
    )
    if matching_product is None or matching_product.get("sha256") != manifest.get(
        "product_wheel_sha256"
    ):
        raise ValueError("Product wheel identity is missing or inconsistent.")
    for required_path in ("install.py", "requirements.txt", product_wheel):
        if required_path not in declared:
            raise ValueError(f"Required bundle member is not inventoried: {required_path}")
    _validate_requirements(root / "requirements.txt")
    return cast(dict[str, Any], manifest)


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _launcher(root: Path) -> Path:
    return root / ("Scripts/whoopy.exe" if os.name == "nt" else "bin/whoopy")


def _offline_environment() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "PIP_NO_INDEX": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
            "PIP_CONFIG_FILE": os.devnull,
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "NO_PROXY": "*",
            "no_proxy": "*",
            "PYTHONNOUSERSITE": "1",
        }
    )
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "PIP_USER",
        "PIP_TARGET",
        "PIP_PREFIX",
        "PYTHONUSERBASE",
        "PYTHONHOME",
        "PYTHONPATH",
        "VIRTUAL_ENV",
    ):
        environment.pop(name, None)
    return environment


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as output:
        output.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)


def _snapshot_bundle(source: Path, destination: Path, manifest: dict[str, Any]) -> Path:
    snapshot = destination / "bundle-snapshot"
    snapshot.mkdir()
    for entry in cast(list[dict[str, Any]], manifest["files"]):
        relative = cast(str, entry["path"])
        source_path = _safe_member(source, relative)
        target_path = _safe_member(snapshot, relative)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, target_path, follow_symlinks=False)
    shutil.copyfile(source / "bundle-manifest.json", snapshot / "bundle-manifest.json")
    verify_bundle(snapshot)
    return snapshot


def install_bundle(bundle_root: Path, prefix: Path) -> dict[str, Any]:
    """Create a fresh environment from the local wheelhouse at its stable final path."""

    root = bundle_root.resolve()
    manifest = verify_bundle(root)
    destination = prefix.expanduser().resolve()
    if destination == root or destination.is_relative_to(root) or root.is_relative_to(destination):
        raise ValueError("Install destination and verified bundle must not overlap.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    marker = destination / ".whoopy-offline-installing.json"
    if destination.exists():
        try:
            interrupted = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            interrupted = None
        if (
            not isinstance(interrupted, dict)
            or interrupted.get("schema_version") != 2
            or interrupted.get("bundle_id") != manifest.get("bundle_id")
            or interrupted.get("destination") != str(destination)
            or interrupted.get("python_executable") != str(Path(sys.executable).resolve())
            or interrupted.get("state") != "installing"
            or not isinstance(interrupted.get("attempt_id"), str)
            or re.fullmatch(r"[0-9a-f]{32}", interrupted["attempt_id"]) is None
        ):
            raise ValueError(f"Install destination already exists: {destination}")
    else:
        destination.mkdir()
        _atomic_write_json(
            marker,
            {
                "schema_version": 2,
                "bundle_id": manifest["bundle_id"],
                "attempt_id": uuid4().hex,
                "destination": str(destination),
                "python_executable": str(Path(sys.executable).resolve()),
                "state": "installing",
            },
        )
    environment = _offline_environment()
    with tempfile.TemporaryDirectory(
        prefix=f".{destination.name}.whoopy-input-", dir=destination.parent
    ) as temporary:
        snapshot = _snapshot_bundle(root, Path(temporary), manifest)
        snapshot_manifest = verify_bundle(snapshot)
        subprocess.run(
            [sys.executable, "-I", "-m", "venv", str(destination)],
            check=True,
            env=environment,
        )
        python = _venv_python(destination)
        wheelhouse = snapshot / "wheelhouse"
        verify_bundle(snapshot)
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-index",
                "--only-binary=:all:",
                "--require-hashes",
                "--find-links",
                str(wheelhouse),
                "--requirement",
                str(snapshot / "requirements.txt"),
            ],
            check=True,
            env=environment,
        )
        verify_bundle(snapshot)
        product_wheel = _safe_member(snapshot, str(snapshot_manifest["product_wheel"]))
        product_requirement = Path(temporary) / "product-requirement.txt"
        product_requirement.write_text(
            f"whoopy @ {product_wheel.as_uri()} "
            f"--hash=sha256:{snapshot_manifest['product_wheel_sha256']}\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "install",
                "--no-index",
                "--no-deps",
                "--require-hashes",
                "--requirement",
                str(product_requirement),
            ],
            check=True,
            env=environment,
        )
        subprocess.run(
            [str(python), "-I", "-m", "pip", "--isolated", "check"],
            check=True,
            env=environment,
            capture_output=True,
        )
        launcher = _launcher(destination)
        subprocess.run([str(launcher), "--help"], check=True, env=environment, capture_output=True)
        receipt = {
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
        _atomic_write_json(destination / ".whoopy-offline-install.json", receipt)
        marker.unlink()
        return receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--prefix", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)
    manifest = verify_bundle(args.bundle)
    if args.verify_only:
        print(json.dumps({"verified": True, "bundle_id": manifest["bundle_id"]}))
        return 0
    if args.prefix is None:
        parser.error("--prefix is required unless --verify-only is used")
    receipt = install_bundle(args.bundle, args.prefix)
    print(json.dumps(receipt))
    print(f"Launch: {_launcher(args.prefix.resolve())} app")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
