"""PCM fixtures prove transport/integrity only, never speech or listening quality."""

from __future__ import annotations

import hashlib
import json
import struct
import wave
from pathlib import Path
from typing import Any

import pytest

from whoopy.webui.voice_previews import (
    MAX_MANIFEST_BYTES,
    MAX_PREVIEWS,
    MAX_WAV_BYTES,
    VoicePreviewService,
)


def _fixture(tmp_path: Path) -> tuple[VoicePreviewService, Path, dict[str, Any]]:
    root = tmp_path / "voice-previews"
    root.mkdir()
    path = root / "fixture.wav"
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        output.writeframes(b"\x00\x00" * 2400)
    record = {
        "model_id": "mlx-qwen3-custom-voice-1.7b-8bit",
        "voice_id": "Ryan",
        "path": "fixture.wav",
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "model_revision": "fixture-only",
        "recorded_at": "2026-11-05",
        "human_quality": "unrated",
        "label": "Transport fixture, not qualified speech",
        "controls": {"pace": "slow", "speed": 0.8},
    }
    _manifest(root, [record])
    return VoicePreviewService(tmp_path), root, record


def _manifest(root: Path, records: list[dict[str, Any]]) -> None:
    (root / "index.json").write_text(json.dumps({"schema_version": 1, "previews": records}))


def test_missing_manifest_is_empty(tmp_path: Path) -> None:
    service = VoicePreviewService(tmp_path)
    assert service.status() == {"entries": [], "rejected": [], "error": None}
    assert service.lookup("kokoro", "af_bella") is None
    assert service.urls("kokoro") == {}


def test_exact_original_pcm_bytes_and_public_metadata(tmp_path: Path) -> None:
    service, root, record = _fixture(tmp_path)
    preview = service.lookup(record["model_id"], "Ryan")
    assert preview is not None
    assert preview.wav_bytes == (root / "fixture.wav").read_bytes()
    assert preview.metadata["duration_seconds"] == 0.1
    assert preview.metadata["human_quality"] == "unrated"
    assert preview.metadata["managed_ready"] is False
    assert "path" not in preview.metadata
    assert service.urls(record["model_id"]) == {"Ryan": preview.url}
    assert service.lookup("different-backend", "Ryan") is None
    assert service.lookup(record["model_id"], "ryan") is None


@pytest.mark.parametrize(
    "change",
    [
        {"path": "../escape.wav"},
        {"path": "/tmp/escape.wav"},
        {"path": "https://example.com/voice.wav"},
        {"path": "a/../fixture.wav"},
        {"path": "a\\fixture.wav"},
        {"path": "./fixture.wav"},
        {"voice_id": "../Ryan"},
        {"human_quality": "approved"},
        {"recorded_at": "yesterday"},
        {"model_revision": ""},
        {"extra": "forbidden"},
        {"controls": {"speed": float("nan")}},
        {"controls": {"speed": [1, 2]}},
    ],
)
def test_malformed_records_fail_closed(tmp_path: Path, change: dict[str, Any]) -> None:
    service, root, record = _fixture(tmp_path)
    _manifest(root, [record | change])
    assert service.status()["error"]
    assert service.lookup(record["model_id"], "Ryan") is None


def test_duplicate_model_voice_rejected(tmp_path: Path) -> None:
    service, root, record = _fixture(tmp_path)
    _manifest(root, [record, record])
    assert service.status()["error"]
    assert service.urls(record["model_id"]) == {}


def test_duplicate_json_fields_rejected(tmp_path: Path) -> None:
    service, root, _record = _fixture(tmp_path)
    (root / "index.json").write_text('{"schema_version":1,"previews":[],"previews":[]}')
    assert service.status()["error"]


@pytest.mark.parametrize("version", [True, "1", 2])
def test_manifest_version_is_strict(tmp_path: Path, version: object) -> None:
    service, root, record = _fixture(tmp_path)
    (root / "index.json").write_text(json.dumps({"schema_version": version, "previews": [record]}))
    assert service.status()["error"]


def test_oversized_wav_rejected_before_read(tmp_path: Path) -> None:
    service, root, record = _fixture(tmp_path)
    with (root / "fixture.wav").open("wb") as output:
        output.truncate(MAX_WAV_BYTES + 1)
    assert service.lookup(record["model_id"], "Ryan") is None
    assert service.status()["rejected"]


def test_nested_regular_wav_and_same_voice_in_different_models(tmp_path: Path) -> None:
    service, root, record = _fixture(tmp_path)
    (root / "samples").mkdir()
    (root / "fixture.wav").rename(root / "samples/fixture.wav")
    record["path"] = "samples/fixture.wav"
    _manifest(root, [record, record | {"model_id": "different-backend"}])
    assert len(service.status()["entries"]) == 2
    assert service.lookup(record["model_id"], "Ryan") is not None


def test_manifest_bounds(tmp_path: Path) -> None:
    service, root, record = _fixture(tmp_path)
    (root / "index.json").write_bytes(b" " * (MAX_MANIFEST_BYTES + 1))
    assert service.status()["error"]
    _manifest(root, [record | {"voice_id": f"voice-{i}"} for i in range(MAX_PREVIEWS + 1)])
    assert service.status()["error"]


def test_excessively_nested_manifest_fails_closed(tmp_path: Path) -> None:
    service, root, _record = _fixture(tmp_path)
    (root / "index.json").write_text("[" * 2000 + "0" + "]" * 2000)
    assert service.status()["error"]
    assert service.lookup("kokoro", "af_bella") is None


@pytest.mark.parametrize("kind", ["file", "directory", "manifest", "root"])
def test_symlinks_rejected(tmp_path: Path, kind: str) -> None:
    service, root, record = _fixture(tmp_path)
    if kind == "file":
        (root / "link.wav").symlink_to(root / "fixture.wav")
        _manifest(root, [record | {"path": "link.wav"}])
    elif kind == "directory":
        (root / "linked").symlink_to(root, target_is_directory=True)
        _manifest(root, [record | {"path": "linked/fixture.wav"}])
    elif kind == "manifest":
        (root / "index.json").rename(root / "original.json")
        (root / "index.json").symlink_to(root / "original.json")
    else:
        moved = tmp_path / "elsewhere"
        root.rename(moved)
        root.symlink_to(moved, target_is_directory=True)
    assert service.lookup(record["model_id"], "Ryan") is None
    assert not service.status()["entries"]


def test_symlinked_models_root_rejected(tmp_path: Path) -> None:
    _service, _root, record = _fixture(tmp_path)
    alias = tmp_path / "models-alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    service = VoicePreviewService(alias)
    assert service.lookup(record["model_id"], "Ryan") is None
    assert service.status()["error"]


def test_missing_and_changed_files_are_rejected_on_every_lookup(tmp_path: Path) -> None:
    service, root, record = _fixture(tmp_path)
    good = service.lookup(record["model_id"], "Ryan")
    assert good is not None
    path = root / "fixture.wav"
    path.write_bytes(good.wav_bytes[:-2] + b"\x01\x00")
    assert service.lookup(record["model_id"], "Ryan") is None
    assert service.status()["rejected"]
    path.unlink()
    assert service.lookup(record["model_id"], "Ryan") is None
    # Already-returned bytes remain exactly the validated stream.
    assert hashlib.sha256(good.wav_bytes).hexdigest() == record["sha256"]


@pytest.mark.parametrize("kind", ["truncated", "partial-frame", "float", "trailing", "empty"])
def test_rehashed_invalid_wavs_still_rejected(tmp_path: Path, kind: str) -> None:
    service, root, record = _fixture(tmp_path)
    path = root / "fixture.wav"
    data = bytearray(path.read_bytes())
    if kind == "truncated":
        data = data[:-2]
    elif kind == "partial-frame":
        data = data[:-1] + b"\x00"
        struct.pack_into("<I", data, 40, 4799)
    elif kind == "float":
        struct.pack_into("<H", data, 20, 3)
    elif kind == "trailing":
        data.extend(b"trailing bytes")
    else:
        data = bytearray()
    path.write_bytes(data)
    _manifest(root, [record | {"sha256": hashlib.sha256(data).hexdigest()}])
    assert service.lookup(record["model_id"], "Ryan") is None
    assert service.status()["rejected"]


def test_one_bad_file_does_not_hide_another_valid_record(tmp_path: Path) -> None:
    service, root, record = _fixture(tmp_path)
    _manifest(root, [record, record | {"voice_id": "Serena", "path": "missing.wav"}])
    assert list(service.urls(record["model_id"])) == ["Ryan"]
    assert service.status()["rejected"][0]["voice_id"] == "Serena"
