from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from whoopy.adapters.tts.mlx_installation import (
    _BACKENDS,
    _MLX_REVISION,
    _RUNTIME,
    _SITE,
    _tree_fingerprint,
    resolve_mlx_qwen_installation,
)
from whoopy.audio.synthesis import FatalSynthesisError

CUSTOM = "mlx-qwen3-custom-voice-1.7b-8bit"
DESIGN = "mlx-qwen3-voice-design-1.7b-8bit"


def fingerprint(path: Path) -> dict[str, Any]:
    return {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def installation(tmp_path: Path) -> tuple[Path, Path, dict[str, Any]]:
    root = tmp_path / "models"
    runtime = root / _RUNTIME
    site = runtime / _SITE
    direct = site / "mlx_audio-0.6.3.dist-info/direct_url.json"
    direct.parent.mkdir(parents=True)
    direct.write_text(
        json.dumps(
            {
                "url": "https://github.com/Blaizzy/mlx-audio.git",
                "vcs_info": {
                    "vcs": "git",
                    "commit_id": _MLX_REVISION,
                    "requested_revision": _MLX_REVISION,
                },
            }
        )
    )
    (direct.parent / "RECORD").write_text("../mlx_audio.py,sha256=mutable-record,3\n")
    (site / "mlx_audio.py").write_text("# actual installed code\n")
    (site / "dependency.so").write_bytes(b"native dependency")
    python = runtime / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    target = tmp_path / "python3.12"
    target.write_bytes(b"fake pinned Python executable; never executed")
    python.symlink_to(target)
    config = runtime / ".venv/pyvenv.cfg"
    config.write_text("version_info = 3.12.14\ninclude-system-site-packages = false\n")
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "classification": "experimental_local",
        "managed_ready": False,
        "runtime": {
            "directory": _RUNTIME,
            "python": ".venv/bin/python",
            "python_target": str(target),
            "python_binary": fingerprint(target),
            "pyvenv_cfg": fingerprint(config),
            "site_packages": _SITE,
            "revision": _MLX_REVISION,
            "mlx_audio_direct_url": "mlx_audio-0.6.3.dist-info/direct_url.json",
            "site_packages_fingerprint": _tree_fingerprint(site),
        },
        "backends": {},
    }
    for backend, (directory, task, repo, revision) in _BACKENDS.items():
        weights = runtime / directory
        weights.mkdir(parents=True)
        model = weights / "model.safetensors"
        model.write_bytes(backend.encode())
        manifest["backends"][backend] = {
            "directory": directory,
            "task": task,
            "repository": repo,
            "revision": revision,
            "files": [{"path": "model.safetensors", **fingerprint(model)}],
        }
    path = tmp_path / "experimental_tts.yaml"
    save(path, manifest)
    return root, path, manifest


def save(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(manifest))


@pytest.mark.parametrize("backend", [CUSTOM, DESIGN])
def test_resolve_explicit_unmanaged_installation(installation: Any, backend: str) -> None:
    root, path, manifest = installation
    result = resolve_mlx_qwen_installation(root, backend, manifest_path=path)
    assert result.runtime_directory == root / _RUNTIME
    assert result.model_directory == (
        result.runtime_directory / manifest["backends"][backend]["directory"]
    )
    assert result.python_executable == result.runtime_directory / ".venv/bin/python"
    assert result.runtime_revision == _MLX_REVISION
    assert result.model_revision == manifest["backends"][backend]["revision"]
    assert result.task == manifest["backends"][backend]["task"]
    assert result.classification == "experimental_local"
    assert result.managed_ready is False
    assert len(result.installation_sha256) == 64
    assert result == resolve_mlx_qwen_installation(root, backend, manifest_path=path)


@pytest.mark.parametrize("relative", ["mlx_audio.py", "dependency.so", "injected.pth"])
def test_actual_code_and_native_dependencies_are_pinned(installation: Any, relative: str) -> None:
    root, path, _ = installation
    (root / _RUNTIME / _SITE / relative).write_bytes(b"altered dependency")
    with pytest.raises(FatalSynthesisError, match="fingerprint mismatch"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


def test_updated_record_cannot_bless_changed_code(installation: Any) -> None:
    root, path, _ = installation
    site = root / _RUNTIME / _SITE
    (site / "mlx_audio.py").write_text("# changed code\n")
    (site / "mlx_audio-0.6.3.dist-info/RECORD").write_text("new valid-looking RECORD\n")
    with pytest.raises(FatalSynthesisError, match="fingerprint mismatch"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


def test_pinned_direct_url_commit_is_required(installation: Any) -> None:
    root, path, _ = installation
    direct = root / _RUNTIME / _SITE / "mlx_audio-0.6.3.dist-info/direct_url.json"
    direct.write_text(json.dumps({"url": "same name", "vcs_info": {"commit_id": "0" * 40}}))
    with pytest.raises(FatalSynthesisError, match="direct_url"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


@pytest.mark.parametrize("change", ["mutated", "missing", "extra"])
def test_checkpoint_integrity(installation: Any, change: str) -> None:
    root, path, _ = installation
    model = root / _RUNTIME / "weights/mlx-qwen-custom/model.safetensors"
    if change == "mutated":
        model.write_bytes(b"x" * model.stat().st_size)
    elif change == "missing":
        model.unlink()
    else:
        (model.parent / "tokenizer.py").write_text("# unexpected checkpoint code")
    with pytest.raises(FatalSynthesisError, match=r"SHA256 mismatch|Missing|inventory mismatch"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("directory", "../outside"),
        ("python", "/arbitrary/python"),
        ("site_packages", "../packages"),
        ("revision", "0" * 40),
    ],
)
def test_fixed_runtime_paths_and_revision(installation: Any, field: str, value: str) -> None:
    root, path, manifest = installation
    manifest["runtime"][field] = value
    save(path, manifest)
    with pytest.raises(FatalSynthesisError, match=r"fixed|Unexpected"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("directory", "weights/somewhere-else"),
        ("revision", "0" * 40),
        ("repository", "other/model"),
        ("task", "voice_clone"),
    ],
)
def test_fixed_checkpoint_identity(installation: Any, field: str, value: str) -> None:
    root, path, manifest = installation
    manifest["backends"][CUSTOM][field] = value
    save(path, manifest)
    with pytest.raises(FatalSynthesisError, match="must be fixed"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


@pytest.mark.parametrize("relative", ["../escape", "/absolute", "dir/../escape", "./model"])
def test_checkpoint_file_traversal_rejected(installation: Any, relative: str) -> None:
    root, path, manifest = installation
    manifest["backends"][CUSTOM]["files"][0]["path"] = relative
    save(path, manifest)
    with pytest.raises(FatalSynthesisError, match=r"Unsafe|Non-canonical"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


def test_symlinked_model_file_rejected(installation: Any) -> None:
    root, path, _ = installation
    model = root / _RUNTIME / "weights/mlx-qwen-custom/model.safetensors"
    moved = model.with_suffix(".external")
    model.rename(moved)
    model.symlink_to(moved)
    with pytest.raises(FatalSynthesisError, match="Symlink"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


def test_python_target_and_bytes_pinned(installation: Any) -> None:
    root, path, manifest = installation
    Path(manifest["runtime"]["python_target"]).write_bytes(b"different Python")
    with pytest.raises(FatalSynthesisError, match=r"size mismatch|SHA256 mismatch"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


def test_no_claim_of_managed_ready(installation: Any) -> None:
    root, path, manifest = installation
    manifest["managed_ready"] = True
    save(path, manifest)
    with pytest.raises(FatalSynthesisError, match="managed readiness"):
        resolve_mlx_qwen_installation(root, CUSTOM, manifest_path=path)


def test_missing_installation_and_unsupported_backend(installation: Any) -> None:
    root, path, _ = installation
    with pytest.raises(ValueError, match="Unsupported"):
        resolve_mlx_qwen_installation(root, "managed-or-arbitrary-model", manifest_path=path)
    with pytest.raises(FatalSynthesisError, match="Missing"):
        resolve_mlx_qwen_installation(root / "absent", CUSTOM, manifest_path=path)
