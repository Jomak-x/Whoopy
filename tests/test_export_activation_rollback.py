from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from tests.test_exports import _metadata, _write_narration
from whoopy.audio import exports
from whoopy.audio.exports import AudioExportError, ExportArtifact, ExportKind, ExportMetadata


@pytest.mark.parametrize("rollback_failure", [None, "restore", "evacuate"])
def test_activation_sync_failure_preserves_previous_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rollback_failure: str | None
) -> None:
    source = tmp_path / "narration.wav"
    _write_narration(source)
    original_source = source.read_bytes()
    output = tmp_path / "exports"
    output.mkdir()
    prior = b"previous export bytes"
    (output / "previous.wav").write_bytes(prior)
    sync_directory = exports._fsync_directory
    replace = exports.os.replace
    activation_failed = False

    def encode(
        frozen: Path,
        staging: Path,
        _metadata: ExportMetadata,
        *,
        kind: ExportKind,
        filename: str,
        expected_codec: str,
        **_kwargs: object,
    ) -> ExportArtifact:
        data = frozen.read_bytes()
        (staging / filename).write_bytes(data)
        return ExportArtifact(
            kind=kind,
            filename=filename,
            codec=expected_codec,
            byte_count=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            duration_ms=1_000,
            sample_rate=24_000,
            channels=1,
            embedded_tags={},
        )

    def sync(directory: Path) -> None:
        nonlocal activation_failed
        if directory == tmp_path and not activation_failed:
            assert (output / "export-manifest.json").is_file()
            backup = next(tmp_path.glob(".exports.*.backup"))
            assert (backup / "previous.wav").read_bytes() == prior
            activation_failed = True
            raise OSError("activation directory sync failed")
        sync_directory(directory)

    def move(src: Path, dst: Path) -> None:
        if activation_failed and (
            (rollback_failure == "restore" and src.suffix == ".backup")
            or (rollback_failure == "evacuate" and src == output)
        ):
            raise OSError("rollback rename failed")
        replace(src, dst)

    monkeypatch.setattr(exports, "_ffmpeg_version", lambda _path: "fixture encoder")
    monkeypatch.setattr(exports, "_encode_and_verify", encode)
    monkeypatch.setattr(exports, "_fsync_directory", sync)
    monkeypatch.setattr(exports.os, "replace", move)

    with pytest.raises(AudioExportError, match="activation directory sync failed") as failure:
        exports.export_finished_audio(
            source,
            output,
            _metadata(),
            ffmpeg_executable=Path(sys.executable),
            ffprobe_executable=Path(sys.executable),
        )

    assert activation_failed
    assert source.read_bytes() == original_source
    assert not list(tmp_path.glob(".exports.*.tmp"))
    backups = list(tmp_path.glob(".exports.*.backup"))
    if rollback_failure is None:
        assert backups == []
        assert {path.name: path.read_bytes() for path in output.iterdir()} == {
            "previous.wav": prior
        }
    else:
        assert len(backups) == 1
        assert {path.name: path.read_bytes() for path in backups[0].iterdir()} == {
            "previous.wav": prior
        }
        assert str(backups[0]) in str(failure.value)
        assert "rollback failed" in str(failure.value)
