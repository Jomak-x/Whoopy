"""Acquire and assemble one hash-locked Whoopy application bundle for a native target."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

REPOSITORY_IMPORT_ROOT = str(Path(__file__).resolve().parent.parent)
if REPOSITORY_IMPORT_ROOT not in sys.path:  # Direct ``python scripts/...`` execution.
    sys.path.insert(0, REPOSITORY_IMPORT_ROOT)

from scripts.install_offline_bundle import (  # noqa: E402
    _is_reparse_point,
    _target_id,
    verify_bundle,
)

BUNDLE_GUIDES = (
    "setup.md",
    "troubleshooting.md",
    "model-capabilities.md",
    "privacy.md",
    "user-guide.md",
    "native-portability.md",
    "local-v1-acceptance-status.md",
    "release-finalization.md",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _run(command: list[str], *, cwd: Path, environment: dict[str, str] | None = None) -> None:
    completed = subprocess.run(command, cwd=cwd, env=environment, check=False, text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Bundle acquisition command failed ({completed.returncode}): {command}")


def _venv_python(root: Path) -> Path:
    return root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _file_entry(root: Path, path: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _regular_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for directory, directory_names, file_names in os.walk(root, followlinks=False):
        base = Path(directory)
        for name in [*directory_names, *file_names]:
            path = base / name
            mode = path.lstat().st_mode
            if _is_reparse_point(path):
                raise ValueError(f"Refusing to bundle link or reparse point: {path}")
            if name in directory_names:
                if not stat.S_ISDIR(mode):
                    raise ValueError(f"Refusing non-directory bundle member: {path}")
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                raise ValueError(f"Refusing non-regular bundle member: {path}")
    return sorted(files)


def _deterministic_zip(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    files = _regular_files(source)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    if temporary.exists():
        raise ValueError(f"Temporary archive output already exists: {temporary}")
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
        ) as archive:
            seen: set[str] = set()
            for path in files:
                relative = path.relative_to(source.parent).as_posix()
                if relative in seen or relative.startswith("/") or ".." in Path(relative).parts:
                    raise ValueError(f"Unsafe or duplicate archive path: {relative!r}")
                seen.add(relative)
                info = zipfile.ZipInfo(relative)
                info.date_time = (1980, 1, 1, 0, 0, 0)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (0o100755 if path.name == "install.py" else 0o100644) << 16
                archive.writestr(
                    info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9
                )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def build_bundle(
    *,
    repository: Path,
    wheel: Path,
    output_directory: Path,
    target: str,
    python: str,
    uv: str,
) -> tuple[Path, Path]:
    """Build a target-native bundle; dependency acquisition is the only online phase."""

    repository = repository.resolve()
    wheel = wheel.resolve()
    if target != _target_id():
        raise ValueError(f"Cannot acquire {target!r} from native target {_target_id()!r}.")
    if not wheel.is_file() or wheel.suffix != ".whl" or not wheel.name.startswith("whoopy-"):
        raise ValueError(f"Expected one built Whoopy wheel, got: {wheel}")
    if not re.fullmatch(r"3\.(?:11|12)", python):
        raise ValueError("--python must be the supported family '3.11' or '3.12'.")
    python_family = python
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    wheel_parts = wheel.name.split("-")
    if len(wheel_parts) < 5:
        raise ValueError(f"Could not derive product version from wheel: {wheel.name}")
    product_version = wheel_parts[1]
    bundle_name = f"whoopy-app-{product_version}-{target}-py{python_family.replace('.', '')}"
    final = output_directory / bundle_name
    archive = output_directory / f"{bundle_name}.zip"
    digest_path = archive.with_suffix(".zip.sha256")
    collisions = [path for path in (final, archive, digest_path) if path.exists()]
    if collisions:
        raise ValueError(f"Bundle output already exists: {collisions[0]}")
    with tempfile.TemporaryDirectory(
        prefix="whoopy-app-bundle-", dir=output_directory
    ) as temporary:
        staging = Path(temporary) / bundle_name
        wheelhouse = staging / "wheelhouse"
        wheelhouse.mkdir(parents=True)
        requirements = staging / "requirements.txt"
        _run(
            [
                uv,
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--no-editable",
                "--no-header",
                "--format",
                "requirements.txt",
                "--output-file",
                str(requirements),
            ],
            cwd=repository,
        )
        acquisition_venv = Path(temporary) / "acquisition-venv"
        _run([uv, "venv", "--seed", "--python", python, str(acquisition_venv)], cwd=repository)
        acquisition_python = _venv_python(acquisition_venv)
        _run(
            [
                str(acquisition_python),
                "-I",
                "-m",
                "pip",
                "--isolated",
                "download",
                "--only-binary=:all:",
                "--require-hashes",
                "--destination-directory",
                str(wheelhouse),
                "--requirement",
                str(requirements),
            ],
            cwd=repository,
        )
        product = wheelhouse / wheel.name
        shutil.copyfile(wheel, product)
        shutil.copyfile(repository / "scripts/install_offline_bundle.py", staging / "install.py")
        guide_sources = [repository / "docs" / name for name in BUNDLE_GUIDES]
        guide_note = ""
        if any(path.exists() for path in guide_sources):
            missing_guides = [path for path in guide_sources if not path.is_file()]
            if missing_guides:
                raise ValueError(f"Release guide is missing: {missing_guides[0]}")
            guides = staging / "docs"
            guides.mkdir()
            for source in guide_sources:
                shutil.copyfile(source, guides / source.name)
            guide_note = (
                "\nThe docs directory contains setup, usage, model capability, privacy, "
                "portability, acceptance, and troubleshooting guides.\n"
            )
        (staging / "README.txt").write_text(
            "Whoopy offline application bundle\n\n"
            f"Target: {target}\nPython: {python_family}\n\n"
            "After installing the named Python family, verify or install without a network:\n\n"
            "  python install.py --verify-only\n"
            "  python install.py --prefix /path/to/whoopy\n\n"
            "Then run <prefix>/bin/whoopy app on macOS/Linux or "
            "<prefix>\\Scripts\\whoopy.exe app on Windows. Model checkpoints, FFmpeg, and "
            "Python itself are acquired separately.\n"
            f"{guide_note}",
            encoding="utf-8",
        )
        files = [_file_entry(staging, path) for path in _regular_files(staging)]
        content_identity = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "evidence_scope": "whoopy_application_offline_bundle",
            "bundle_id": content_identity,
            "product_version": product_version,
            "target": target,
            "python": python_family,
            "lock_sha256": _sha256(repository / "uv.lock"),
            "product_wheel": f"wheelhouse/{wheel.name}",
            "product_wheel_sha256": _sha256(product),
            "files": files,
        }
        (staging / "bundle-manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        verify_bundle(staging, enforce_runtime_compatibility=False)
        _run(
            [
                str(acquisition_python),
                "-I",
                str(staging / "install.py"),
                "--bundle",
                str(staging),
                "--verify-only",
            ],
            cwd=repository,
        )
        os.replace(staging, final)
    _deterministic_zip(final, archive)
    archive_digest = _sha256(archive)
    digest_temporary = digest_path.with_name(f".{digest_path.name}.{os.getpid()}.tmp")
    digest_temporary.write_text(f"{archive_digest}  {archive.name}\n", encoding="ascii")
    os.replace(digest_temporary, digest_path)
    return final, archive


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, default=Path.cwd())
    wheel_source = parser.add_mutually_exclusive_group()
    wheel_source.add_argument("--wheel", type=Path)
    wheel_source.add_argument("--wheel-dir", type=Path, default=Path("dist"))
    parser.add_argument("--output-dir", type=Path, default=Path("dist/offline"))
    parser.add_argument("--target", default=_target_id())
    parser.add_argument("--python", default=f"{sys.version_info.major}.{sys.version_info.minor}")
    parser.add_argument("--uv", default="uv")
    args = parser.parse_args(argv)
    wheels = (
        [args.wheel.resolve()]
        if args.wheel is not None
        else sorted(args.wheel_dir.resolve().glob("whoopy-*.whl"))
    )
    if len(wheels) != 1:
        raise SystemExit(
            f"Expected exactly one Whoopy wheel; found {len(wheels)} in {args.wheel_dir}."
        )
    directory, archive = build_bundle(
        repository=args.repository,
        wheel=wheels[0],
        output_directory=args.output_dir,
        target=args.target,
        python=args.python,
        uv=args.uv,
    )
    print(directory)
    print(f"{archive} sha256={_sha256(archive)}")
    print(archive.with_suffix(".zip.sha256"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
