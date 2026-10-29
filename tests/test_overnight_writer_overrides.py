from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
import yaml

from scripts.overnight_e2e import freeze_writer_inputs
from whoopy.artifacts import ArtifactError, TargetPlatform, load_artifact_lock


def lock(path: Path, identity: str) -> bytes:
    payload = yaml.safe_dump(
        {
            "schema_version": 1,
            "artifacts": [
                {
                    "artifact_id": identity,
                    "component": "llm_model_high",
                    "display_name": identity,
                    "version": "fixture-1",
                    "kind": "model",
                    "license_id": "Apache-2.0",
                    "source_url": "https://example.invalid/writer.gguf",
                    "filename": "writer.gguf",
                    "size_bytes": 1,
                    "sha256": "0" * 64,
                    "operating_systems": ["all"],
                    "architectures": ["all"],
                }
            ],
            "profiles": {"high": {"components": ["llm_model_high"]}},
        }
    ).encode()
    path.write_bytes(payload)
    return payload


def test_effective_frozen_lock_uses_real_consumer_and_leaves_originals_unchanged(
    tmp_path: Path,
) -> None:
    config = tmp_path / "campaign/config"
    config.mkdir(parents=True)
    baseline = lock(config / "artifacts.yaml", "baseline_writer")
    original = tmp_path / "experimental-lock.yaml"
    experimental = lock(original, "experimental_qwen35")
    store = tmp_path / "installed/managed"
    store.mkdir(parents=True)
    source_snapshot_lock = tmp_path / "source-snapshot-artifacts.yaml"
    source_snapshot_lock.write_bytes(baseline)
    provenance = freeze_writer_inputs(
        {
            "assets": str(tmp_path),
            "writer_artifact_lock": str(original),
            "writer_models_dir": str(store),
        },
        config,
    )
    effective = Path(provenance["writer_artifact_lock_effective_path"])
    resolved = load_artifact_lock(effective).resolve("high", TargetPlatform.current())
    assert resolved[0].artifact_id == "experimental_qwen35"
    assert provenance["writer_artifact_lock_original_path"] == str(original)
    assert provenance["writer_artifact_lock_sha256"] == hashlib.sha256(experimental).hexdigest()
    assert provenance["writer_models_dir"] == str(store.resolve())
    assert original.read_bytes() == experimental
    assert source_snapshot_lock.read_bytes() == baseline
    original.write_text("later external change")
    assert effective.read_bytes() == experimental


def test_copy_failure_and_invalid_lock_leave_existing_frozen_default_unchanged(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    config.mkdir()
    baseline = lock(config / "artifacts.yaml", "baseline_writer")
    original = tmp_path / "experimental.yaml"
    experimental = lock(original, "experimental_qwen35")
    plan: dict[str, Any] = {"assets": str(tmp_path), "writer_artifact_lock": str(original)}

    def fail_copy(source: Path, destination: Path) -> None:
        destination.write_text("partial copy")
        raise OSError("fixture copy failure")

    with patch("scripts.overnight_e2e.shutil.copyfile", fail_copy), pytest.raises(OSError):
        freeze_writer_inputs(plan, config)
    assert (config / "artifacts.yaml").read_bytes() == baseline
    assert original.read_bytes() == experimental
    assert not list(config.glob(".writer-artifacts-*"))
    original.write_text("invalid: lock")
    with pytest.raises(ArtifactError):
        freeze_writer_inputs(plan, config)
    assert (config / "artifacts.yaml").read_bytes() == baseline
    assert original.read_text() == "invalid: lock"


def test_absent_overrides_preserve_default_lock_and_store(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    baseline = lock(config / "artifacts.yaml", "baseline_writer")
    provenance = freeze_writer_inputs({"assets": str(tmp_path)}, config)
    assert (config / "artifacts.yaml").read_bytes() == baseline
    assert provenance["writer_artifact_lock_original_path"] is None
    assert provenance["writer_models_dir"] == str(tmp_path / "models/managed")
