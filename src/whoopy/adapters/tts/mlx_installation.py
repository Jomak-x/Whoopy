"""Fail-closed provenance for the explicitly selected, unmanaged MLX experiment.

This is deliberately separate from model-pack readiness and install receipts. The
trusted, checked-in manifest pins actual installed bytes, not mutable wheel RECORD
claims. Workers must use isolated Python and a fresh bytecode-cache prefix: the
fingerprint excludes __pycache__, and no existing bytecode may be executed.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import yaml

from whoopy.audio.synthesis import FatalSynthesisError

_RUNTIME = "experimental/voice-benchmark-2026-10-28"
_SITE = ".venv/lib/python3.12/site-packages"
_MLX_REVISION = "8c244a1faff44cfa69e3151c195f52a4c1c90be5"
_BACKENDS = {
    "mlx-qwen3-custom-voice-1.7b-8bit": (
        "weights/mlx-qwen-custom",
        "custom_voice",
        "mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit",
        "41d3337e8b7f2843a75841595fc14e4b9a7a4b96",
    ),
    "mlx-qwen3-voice-design-1.7b-8bit": (
        "weights/mlx-qwen-design",
        "voice_design",
        "mlx-community/Qwen3-TTS-12Hz-1.7B-VoiceDesign-8bit",
        "f90d617701d9f7f4ca499291e0b57f2b3c2fd2ee",
    ),
}


@dataclass(frozen=True)
class MlxQwenInstallation:
    runtime_directory: Path
    model_directory: Path
    python_executable: Path
    runtime_revision: str
    model_revision: str
    installation_sha256: str
    backend: str
    task: str
    repository: str
    classification: Literal["experimental_local"] = "experimental_local"
    managed_ready: Literal[False] = False


def _sha256(path: Path) -> str:
    """Hash without executing code, and reject a file changed while being read."""
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError(f"File changed during verification: {path}")
    return digest.hexdigest()


def _path(root: Path, relative: str) -> Path:
    pure = PurePosixPath(relative)
    if not relative or pure.is_absolute() or any(part in {".", ".."} for part in pure.parts):
        raise ValueError(f"Unsafe experimental installation path: {relative!r}")
    if str(pure) != relative:
        raise ValueError(f"Non-canonical experimental installation path: {relative!r}")
    current = root
    for part in pure.parts:
        current /= part
        if current.is_symlink():
            raise ValueError(f"Symlink is not allowed in experimental installation: {current}")
    if not current.resolve().is_relative_to(root.resolve(strict=True)):
        raise ValueError(f"Experimental installation escapes models root: {relative}")
    return current


def _tree_fingerprint(directory: Path) -> dict[str, Any]:
    """Pin complete installed contents, including RECORD and unrecorded files.

    Ignoring caches is safe only with a fresh worker pycache_prefix. The tree
    digest incorporates relative name, length, and SHA256 for each regular file;
    additions, missing packages, changed native libraries and code all invalidate it.
    """
    if not directory.is_dir():
        raise ValueError(f"Missing experimental runtime directory: {directory}")
    digest = hashlib.sha256()
    count = total = 0
    for parent, dirs, files in os.walk(directory, followlinks=False):
        dirs.sort()
        files.sort()
        for name in dirs + files:
            if (Path(parent) / name).is_symlink():
                raise ValueError(f"Symlink is not allowed in runtime tree: {Path(parent) / name}")
        dirs[:] = [name for name in dirs if name != "__pycache__"]
        for name in files:
            path = Path(parent) / name
            if not path.is_file():
                raise ValueError(f"Non-regular file in experimental runtime: {path}")
            size = path.stat().st_size
            row = [path.relative_to(directory).as_posix(), size, _sha256(path)]
            digest.update(json.dumps(row, separators=(",", ":")).encode() + b"\n")
            count += 1
            total += size
    return {"sha256": digest.hexdigest(), "files": count, "bytes": total}


def _verify_file(path: Path, expected: dict[str, Any]) -> str:
    if not path.is_file():
        raise ValueError(f"Missing experimental installation file: {path}")
    if path.stat().st_size != expected["bytes"]:
        raise ValueError(f"Experimental installation size mismatch: {path}")
    actual = _sha256(path)
    if actual != expected["sha256"]:
        raise ValueError(f"Experimental installation SHA256 mismatch: {path}")
    return actual


def resolve_mlx_qwen_installation(
    models_root: Path, backend: str, *, manifest_path: Path
) -> MlxQwenInstallation:
    """Verify a fixed local experiment; never install, download, or claim READY.

    The caller must explicitly opt into this backend and supply the checked-in
    manifest. No arbitrary Python/model directory can be selected by this API.
    All model files are rehashed, without metadata-based verification caches.
    """
    if backend not in _BACKENDS:
        raise ValueError(f"Unsupported experimental MLX backend: {backend}")
    try:
        return _resolve(models_root, backend, manifest_path)
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError) as error:
        raise FatalSynthesisError(
            f"Experimental MLX installation verification failed: {error}"
        ) from error


def _resolve(models_root: Path, backend: str, manifest_path: Path) -> MlxQwenInstallation:
    if models_root.is_symlink() or not models_root.is_dir():
        raise ValueError(f"Missing or symlinked models root: {models_root}")
    root = models_root.resolve(strict=True)
    raw = manifest_path.read_bytes()
    manifest = yaml.safe_load(raw)
    if manifest["schema_version"] != 1 or manifest["classification"] != "experimental_local":
        raise ValueError("Manifest is not an experimental_local version 1 installation")
    if manifest["managed_ready"] is not False:
        raise ValueError("Experimental MLX installation cannot claim managed readiness")
    runtime_spec = manifest["runtime"]
    if runtime_spec["directory"] != _RUNTIME or runtime_spec["python"] != ".venv/bin/python":
        raise ValueError("Experimental runtime and Python paths must be fixed")
    if runtime_spec["site_packages"] != _SITE or runtime_spec["revision"] != _MLX_REVISION:
        raise ValueError("Unexpected MLX runtime revision or site-packages path")
    runtime = _path(root, _RUNTIME)
    site = _path(runtime, _SITE)
    config = _path(runtime, ".venv/pyvenv.cfg")
    _verify_file(config, runtime_spec["pyvenv_cfg"])
    if "include-system-site-packages = false" not in config.read_text():
        raise ValueError("Experimental Python must exclude system site-packages")

    # This is the only permitted symlink, pinned to its precise base interpreter.
    python_parent = _path(runtime, ".venv/bin")
    python = python_parent / "python"
    target = Path(runtime_spec["python_target"])
    if not target.is_absolute() or target.name != "python3.12":
        raise ValueError("Expected a pinned absolute Python 3.12 executable target")
    if (
        not python.is_symlink()
        or Path(os.readlink(python)) != target
        or target.is_symlink()
        or python.resolve(strict=True) != target
    ):
        raise ValueError("Experimental Python executable target mismatch")
    _verify_file(target, runtime_spec["python_binary"])

    if runtime_spec["mlx_audio_direct_url"] != "mlx_audio-0.6.3.dist-info/direct_url.json":
        raise ValueError("Unexpected mlx-audio installed distribution path")
    direct = _path(site, runtime_spec["mlx_audio_direct_url"])
    direct_url = json.loads(direct.read_text())
    if direct_url != {
        "url": "https://github.com/Blaizzy/mlx-audio.git",
        "vcs_info": {"vcs": "git", "commit_id": _MLX_REVISION, "requested_revision": _MLX_REVISION},
    }:
        raise ValueError("mlx-audio direct_url does not match pinned Git commit")
    environment = _tree_fingerprint(site)
    if environment != runtime_spec["site_packages_fingerprint"]:
        raise ValueError("Experimental MLX installed code/dependency fingerprint mismatch")

    directory, task, repository, revision = _BACKENDS[backend]
    model = manifest["backends"][backend]
    if (model["directory"], model["task"], model["repository"], model["revision"]) != (
        directory,
        task,
        repository,
        revision,
    ):
        raise ValueError("Experimental checkpoint path/repository/revision must be fixed")
    model_directory = _path(runtime, directory)
    files = model["files"]
    if not isinstance(files, list) or not files:
        raise ValueError("Experimental checkpoint manifest has no file hashes")
    seen: set[str] = set()
    for entry in files:
        relative = entry["path"]
        if relative in seen:
            raise ValueError(f"Duplicate checkpoint file: {relative}")
        seen.add(relative)
        _verify_file(_path(model_directory, relative), entry)
    # Extra model files can alter tokenizer/model loading, so reject them as well.
    actual_files: set[str] = set()
    for parent, dirs, names in os.walk(model_directory, followlinks=False):
        for name in dirs + names:
            if (Path(parent) / name).is_symlink():
                raise ValueError("Symlink is not allowed in experimental checkpoint")
        if ".cache" not in Path(parent).relative_to(model_directory).parts:
            actual_files.update(
                (Path(parent) / name).relative_to(model_directory).as_posix() for name in names
            )
    if actual_files != seen:
        raise ValueError("Experimental checkpoint file inventory mismatch")
    identity = hashlib.sha256(
        raw + b"\0" + backend.encode() + b"\0" + json.dumps(environment, sort_keys=True).encode()
    )
    return MlxQwenInstallation(
        runtime_directory=runtime,
        model_directory=model_directory,
        python_executable=python,
        runtime_revision=_MLX_REVISION,
        model_revision=revision,
        installation_sha256=identity.hexdigest(),
        backend=backend,
        task=task,
        repository=repository,
    )
