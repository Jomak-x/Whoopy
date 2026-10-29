from __future__ import annotations

import hashlib
import shutil
import sys
import wave
from datetime import UTC, datetime
from pathlib import Path

import pytest

import whoopy.audio.exports as exports_module
from whoopy.audio import (
    AudioExportError,
    ExportMetadata,
    discover_ffmpeg,
    export_finished_audio,
    load_export_manifest,
)
from whoopy.audio.exports import ExportArtifact, ExportKind, ExportManifest
from whoopy.pipeline.runs import RunStore
from whoopy.webui.server import LocalWebApplication


def _write_narration(path: Path, *, seconds: float = 1.0) -> None:
    sample_rate = 24_000
    frame_count = round(sample_rate * seconds)
    # A low-amplitude square wave is deterministic, audible, and quick to encode.
    frames = b"".join(
        (2_000 if (index // 100) % 2 == 0 else -2_000).to_bytes(2, "little", signed=True)
        for index in range(frame_count)
    )
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)


def _metadata() -> ExportMetadata:
    return ExportMetadata(
        title="Evening Grounding",
        model="kokoro-v1",
        created_at=datetime(2026, 8, 5, 12, 30, tzinfo=UTC),
    )


def test_export_metadata_rejects_blank_and_naive_values() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        ExportMetadata(title="  ", model="kokoro", created_at=datetime.now(UTC))
    with pytest.raises(ValueError, match="timezone"):
        ExportMetadata(
            title="Grounding",
            model="kokoro",
            created_at=datetime(2026, 8, 5, 12, 30),
        )


def test_discovery_has_actionable_missing_ffmpeg_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(AudioExportError, match=r"Install FFmpeg.*on PATH"):
        discover_ffmpeg()


def test_export_requires_authoritative_master_filename(tmp_path: Path) -> None:
    source = tmp_path / "other.wav"
    _write_narration(source)

    with pytest.raises(AudioExportError, match=r"authoritative narration\.wav"):
        export_finished_audio(source, tmp_path / "exports", _metadata())


def test_failed_export_preserves_previous_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "narration.wav"
    _write_narration(source)
    output = tmp_path / "exports"
    output.mkdir()
    prior = output / "prior-export.txt"
    prior.write_text("still authoritative", encoding="utf-8")

    monkeypatch.setattr(exports_module, "_ffmpeg_version", lambda _path: "test ffmpeg")

    def fail_encoding(*_args: object, **_kwargs: object) -> object:
        raise AudioExportError("simulated encoder failure")

    monkeypatch.setattr(exports_module, "_encode_and_verify", fail_encoding)

    with pytest.raises(AudioExportError, match="simulated encoder failure"):
        export_finished_audio(
            source,
            output,
            _metadata(),
            ffmpeg_executable=Path(sys.executable),
            ffprobe_executable=Path(sys.executable),
        )

    assert prior.read_text(encoding="utf-8") == "still authoritative"
    assert not list(tmp_path.glob(".exports.*.tmp"))
    assert not list(tmp_path.glob(".exports.*.backup"))


def test_export_freezes_source_and_rejects_changed_master(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "narration.wav"
    _write_narration(source)
    original = source.read_bytes()
    replacement = original[:-2] + b"\x00\x00"
    output = tmp_path / "exports"
    output.mkdir()
    prior = output / "prior-export.txt"
    prior.write_bytes(b"prior export")
    encoded_sources: list[bytes] = []

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
        encoded_sources.append(data)
        (staging / filename).write_bytes(data)
        if len(encoded_sources) == 1:
            revised = tmp_path / "reassembled.wav"
            revised.write_bytes(replacement)
            revised.replace(source)
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

    monkeypatch.setattr(exports_module, "_ffmpeg_version", lambda _path: "test ffmpeg")
    monkeypatch.setattr(exports_module, "_encode_and_verify", encode)

    with pytest.raises(AudioExportError, match="Narration master changed"):
        export_finished_audio(
            source,
            output,
            _metadata(),
            ffmpeg_executable=Path(sys.executable),
            ffprobe_executable=Path(sys.executable),
        )

    assert encoded_sources == [original] * 4
    assert source.read_bytes() == replacement
    assert prior.read_bytes() == b"prior export"
    assert list(output.iterdir()) == [prior]
    assert not list(tmp_path.glob(".exports.*.tmp"))
    assert not list(tmp_path.glob(".exports.*.backup"))


@pytest.mark.parametrize("change", ["replace", "truncate", "remove"])
def test_export_listing_and_download_reject_changed_artifact(tmp_path: Path, change: str) -> None:
    application = LocalWebApplication(
        project_root=Path.cwd(),
        config_directory=Path.cwd() / "config",
        models_directory=tmp_path / "models",
        runs_directory=tmp_path / "runs",
    )
    store = RunStore(application.runs_directory)
    record = store.create("A quiet moment.")
    run_id = str(record.run_id)
    audio = store.audio_path(run_id)
    audio.write_bytes(b"narration revision A")
    digest = hashlib.sha256(audio.read_bytes()).hexdigest()
    output = audio.parent / "exports"
    output.mkdir()
    exported = output / "finished.wav"
    exported.write_bytes(audio.read_bytes())
    manifest = ExportManifest(
        generated_at=datetime.now(UTC),
        source_filename=audio.name,
        source_byte_count=audio.stat().st_size,
        source_sha256=digest,
        source_duration_ms=1_000,
        metadata=_metadata(),
        ffmpeg_executable="fixture-ffmpeg",
        ffmpeg_version="fixture",
        artifacts=(
            ExportArtifact(
                kind="wav",
                filename=exported.name,
                codec="pcm_s16le",
                byte_count=exported.stat().st_size,
                sha256=digest,
                duration_ms=1_000,
                sample_rate=24_000,
                channels=1,
                embedded_tags={},
            ),
        ),
    )
    receipt = output / "export-manifest.json"
    receipt.write_text(manifest.model_dump_json(), encoding="utf-8")
    original_receipt = receipt.read_bytes()
    available = application.exports(run_id)
    assert available is not None and available["available"] is True
    assert application.export_path(run_id, "wav") == exported

    if change == "replace":
        exported.write_bytes(b"narration revision B")
    elif change == "truncate":
        exported.write_bytes(b"short")
    else:
        exported.unlink()

    assert application.exports(run_id) == {"available": False, "artifacts": []}
    assert application.export_path(run_id, "wav") is None
    assert receipt.read_bytes() == original_receipt
    assert audio.read_bytes() == b"narration revision A"


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed on this test machine",
)
def test_creates_and_verifies_all_finished_formats_atomically(tmp_path: Path) -> None:
    source = tmp_path / "narration.wav"
    _write_narration(source)
    source_bytes = source.read_bytes()
    output = tmp_path / "exports"

    manifest = export_finished_audio(
        source,
        output,
        _metadata(),
        generated_at=datetime(2026, 8, 5, 13, 0, tzinfo=UTC),
    )

    assert source.read_bytes() == source_bytes
    assert manifest.source_sha256 == hashlib.sha256(source_bytes).hexdigest()
    assert manifest.source_duration_ms == 1_000
    assert manifest.metadata.title == "Evening Grounding"
    assert manifest.metadata.model == "kokoro-v1"
    assert {artifact.kind for artifact in manifest.artifacts} == {"wav", "flac", "aac", "opus"}
    assert {artifact.codec for artifact in manifest.artifacts} == {
        "pcm_s16le",
        "flac",
        "aac",
        "opus",
    }
    for artifact in manifest.artifacts:
        path = output / artifact.filename
        assert path.stat().st_size == artifact.byte_count
        assert hashlib.sha256(path.read_bytes()).hexdigest() == artifact.sha256
        assert artifact.decode_verified is True
        assert artifact.channels == 1
        # Opus has a normative 48 kHz decode rate; lossless/AAC preserve 24 kHz.
        assert artifact.sample_rate == (48_000 if artifact.kind == "opus" else 24_000)
        assert artifact.embedded_tags["title"] == "Evening Grounding"

    saved = load_export_manifest(output / "export-manifest.json")
    assert saved == manifest
    assert not list(tmp_path.glob(".exports.*.tmp"))
    assert not list(tmp_path.glob(".exports.*.backup"))


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="FFmpeg is not installed on this test machine",
)
def test_reexport_replaces_complete_directory_and_removes_stale_files(tmp_path: Path) -> None:
    source = tmp_path / "narration.wav"
    _write_narration(source, seconds=0.5)
    output = tmp_path / "exports"
    output.mkdir()
    (output / "stale.txt").write_text("old", encoding="utf-8")

    export_finished_audio(source, output, _metadata())

    assert not (output / "stale.txt").exists()
    assert (output / "export-manifest.json").is_file()
    assert len(list(output.iterdir())) == 5
