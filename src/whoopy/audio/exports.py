"""Verified, atomic finished-audio exports derived from ``narration.wav``.

The narration master remains authoritative.  ffmpeg writes every consumer
format into a sibling staging directory, ffprobe and a full decode validate the
results, and the completed directory is activated only after its manifest has
been flushed to disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import wave
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

ExportKind = Literal["wav", "flac", "aac", "opus"]

_OUTPUT_SPECS: tuple[tuple[ExportKind, str, str, tuple[str, ...]], ...] = (
    ("wav", "finished.wav", "pcm_s16le", ("-c:a", "pcm_s16le")),
    ("flac", "finished.flac", "flac", ("-c:a", "flac")),
    (
        "aac",
        "finished.m4a",
        "aac",
        ("-c:a", "aac", "-b:a", "192k", "-movflags", "use_metadata_tags"),
    ),
    ("opus", "finished.opus", "opus", ("-c:a", "libopus", "-b:a", "128k")),
)


class AudioExportError(RuntimeError):
    """Raised when finished files cannot be created and proven valid."""


class ExportMetadata(BaseModel):
    """Human and provenance metadata embedded in every finished file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str = Field(min_length=1, max_length=300)
    model: str = Field(min_length=1, max_length=300)
    created_at: datetime

    @field_validator("title", "model")
    @classmethod
    def _reject_blank_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("created_at")
    @classmethod
    def _require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value


class ExportArtifact(BaseModel):
    """Verified evidence for one activated consumer audio file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ExportKind
    filename: str
    codec: str = Field(min_length=1)
    byte_count: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_ms: float = Field(gt=0)
    sample_rate: int = Field(gt=0)
    channels: int = Field(gt=0)
    decode_verified: Literal[True] = True
    embedded_tags: dict[str, str]


class ExportManifest(BaseModel):
    """Commit record proving the source and all finished export artifacts."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    generated_at: datetime
    source_filename: str
    source_byte_count: int = Field(gt=0)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_duration_ms: float = Field(gt=0)
    metadata: ExportMetadata
    ffmpeg_executable: str
    ffmpeg_version: str
    artifacts: tuple[ExportArtifact, ...]


def discover_ffmpeg() -> tuple[Path, Path]:
    """Return executable ffmpeg/ffprobe paths or an actionable setup error."""

    ffmpeg = _discover_executable("ffmpeg")
    ffprobe = _discover_executable("ffprobe", preferred_directory=ffmpeg.parent)
    return ffmpeg, ffprobe


def export_finished_audio(
    narration_master: Path,
    output_directory: Path,
    metadata: ExportMetadata,
    *,
    ffmpeg_executable: Path | None = None,
    ffprobe_executable: Path | None = None,
    generated_at: datetime | None = None,
) -> ExportManifest:
    """Create and atomically activate WAV, FLAC, AAC/M4A, and Opus exports.

    ``output_directory`` is replaced as one directory-level activation.  An
    existing directory is restored if activation fails, and the authoritative
    narration master is never modified.
    """

    source = narration_master.resolve()
    if not source.is_file():
        raise AudioExportError(f"Narration master does not exist: {source}")
    if source.name != "narration.wav":
        raise AudioExportError(
            f"Expected the authoritative narration.wav master, received {source.name!r}"
        )
    if ffmpeg_executable is None:
        ffmpeg = _discover_executable("ffmpeg")
    else:
        ffmpeg = _validate_executable(ffmpeg_executable, "ffmpeg")
    if ffprobe_executable is None:
        ffprobe = _discover_executable("ffprobe", preferred_directory=ffmpeg.parent)
    else:
        ffprobe = _validate_executable(ffprobe_executable, "ffprobe")

    timestamp = generated_at or datetime.now(UTC)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise AudioExportError("Export generation time must include a timezone")
    destination = output_directory.resolve()
    if destination == source.parent or destination in source.parents:
        raise AudioExportError("Export directory must not contain the narration master")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.{uuid4().hex}.tmp"
    backup = destination.parent / f".{destination.name}.{uuid4().hex}.backup"
    staging.mkdir()

    try:
        snapshot = staging / "narration.wav"
        shutil.copyfile(source, snapshot)
        source_duration_ms = _wave_duration_ms(snapshot)
        source_byte_count = snapshot.stat().st_size
        source_sha256 = _sha256(snapshot)
        ffmpeg_version = _ffmpeg_version(ffmpeg)
        artifacts = tuple(
            _encode_and_verify(
                snapshot,
                staging,
                metadata,
                source_duration_ms=source_duration_ms,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                kind=kind,
                filename=filename,
                expected_codec=expected_codec,
                codec_arguments=codec_arguments,
            )
            for kind, filename, expected_codec, codec_arguments in _OUTPUT_SPECS
        )
        for artifact in artifacts:
            _fsync_file(staging / artifact.filename)
        manifest = ExportManifest(
            generated_at=timestamp,
            source_filename=source.name,
            source_byte_count=source_byte_count,
            source_sha256=source_sha256,
            source_duration_ms=source_duration_ms,
            metadata=metadata,
            ffmpeg_executable=str(ffmpeg),
            ffmpeg_version=ffmpeg_version,
            artifacts=artifacts,
        )
        _write_manifest(staging / "export-manifest.json", manifest)
        snapshot.unlink()
        _fsync_directory(staging)
        if source.stat().st_size != source_byte_count or _sha256(source) != source_sha256:
            raise AudioExportError("Narration master changed during export; retry the export.")
        _activate_directory(staging, destination, backup)
        return manifest
    except AudioExportError:
        raise
    except OSError as error:
        raise AudioExportError(f"Could not create finished audio exports: {error}") from error
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def load_export_manifest(path: Path) -> ExportManifest:
    """Load a saved export manifest for library/API consumers."""

    try:
        return ExportManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AudioExportError(f"Could not read export manifest {path}: {error}") from error


def _discover_executable(name: str, *, preferred_directory: Path | None = None) -> Path:
    candidates: list[str] = []
    if preferred_directory is not None:
        candidates.append(str(preferred_directory / (f"{name}.exe" if os.name == "nt" else name)))
    discovered = shutil.which(name)
    if discovered is not None:
        candidates.append(discovered)
    for candidate in candidates:
        path = Path(candidate).resolve()
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise AudioExportError(
        f"{name} was not found. Install FFmpeg (including {name}) and ensure it is on PATH, "
        "then restart Whoopy and retry the export."
    )


def _validate_executable(candidate: Path, name: str) -> Path:
    path = candidate.expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise AudioExportError(f"Configured {name} executable is not runnable: {path}")
    return path


def _wave_duration_ms(source: Path) -> float:
    try:
        with wave.open(str(source), "rb") as wav:
            if wav.getnframes() <= 0 or wav.getframerate() <= 0:
                raise AudioExportError("Narration master contains no audio frames")
            if wav.getnchannels() != 1:
                raise AudioExportError("Narration master must be mono")
            return round(wav.getnframes() * 1_000 / wav.getframerate(), 3)
    except (wave.Error, EOFError, OSError) as error:
        raise AudioExportError(f"Narration master is not a valid WAV file: {error}") from error


def _ffmpeg_version(ffmpeg: Path) -> str:
    completed = _run((str(ffmpeg), "-version"), timeout_seconds=15)
    line = completed.stdout.splitlines()[0].strip() if completed.stdout else ""
    if not line:
        raise AudioExportError("ffmpeg returned no version information")
    return line


def _encode_and_verify(
    source: Path,
    staging: Path,
    metadata: ExportMetadata,
    *,
    source_duration_ms: float,
    ffmpeg: Path,
    ffprobe: Path,
    kind: ExportKind,
    filename: str,
    expected_codec: str,
    codec_arguments: tuple[str, ...],
) -> ExportArtifact:
    output = staging / filename
    creation_time = metadata.created_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    comment = f"Whoopy model={metadata.model}; duration_ms={source_duration_ms:.3f}"
    command = (
        str(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map_metadata",
        "-1",
        "-map",
        "0:a:0",
        "-vn",
        "-metadata",
        f"title={metadata.title}",
        "-metadata",
        f"model={metadata.model}",
        "-metadata",
        f"creation_time={creation_time}",
        "-metadata",
        f"date={creation_time}",
        "-metadata",
        f"whoopy_duration_ms={source_duration_ms:.3f}",
        "-metadata",
        f"comment={comment}",
        *codec_arguments,
        str(output),
    )
    _run(command, timeout_seconds=600)
    if not output.is_file() or output.stat().st_size <= 0:
        raise AudioExportError(f"ffmpeg produced an empty {kind.upper()} export")

    probe = _probe(ffprobe, output)
    codec = str(probe.get("codec_name", ""))
    if codec != expected_codec:
        raise AudioExportError(
            f"{filename} uses codec {codec or 'unknown'}; expected {expected_codec}"
        )
    duration_ms = _probe_duration_ms(probe, filename)
    tolerance_ms = max(75.0, source_duration_ms * 0.01)
    if abs(duration_ms - source_duration_ms) > tolerance_ms:
        raise AudioExportError(
            f"{filename} duration is {duration_ms:.3f} ms; expected "
            f"{source_duration_ms:.3f} ms (±{tolerance_ms:.3f} ms)"
        )
    sample_rate = _positive_int(probe.get("sample_rate"), f"{filename} sample rate")
    channels = _positive_int(probe.get("channels"), f"{filename} channel count")
    if channels != 1:
        raise AudioExportError(f"{filename} has {channels} channels; expected mono audio")

    _run(
        (
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-i",
            str(output),
            "-map",
            "0:a:0",
            "-f",
            "null",
            os.devnull,
        ),
        timeout_seconds=600,
    )
    raw_tags = probe.get("tags", {})
    if not isinstance(raw_tags, dict):
        raise AudioExportError(f"ffprobe returned invalid metadata for {filename}")
    tags = {str(key).lower(): str(value) for key, value in raw_tags.items()}
    embedded_tags = _verify_embedded_metadata(
        tags,
        filename=filename,
        title=metadata.title,
        model=metadata.model,
        creation_time=creation_time,
        duration_ms=source_duration_ms,
    )
    return ExportArtifact(
        kind=kind,
        filename=filename,
        codec=codec,
        byte_count=output.stat().st_size,
        sha256=_sha256(output),
        duration_ms=duration_ms,
        sample_rate=sample_rate,
        channels=channels,
        embedded_tags=embedded_tags,
    )


def _verify_embedded_metadata(
    tags: dict[str, str],
    *,
    filename: str,
    title: str,
    model: str,
    creation_time: str,
    duration_ms: float,
) -> dict[str, str]:
    """Verify semantic metadata, including WAV's limited RIFF tag vocabulary."""

    comment = tags.get("comment", "")
    duration_text = f"{duration_ms:.3f}"
    if tags.get("title") != title:
        raise AudioExportError(f"{filename} does not contain the expected title metadata")
    if tags.get("model") != model and f"model={model}" not in comment:
        raise AudioExportError(f"{filename} does not contain the expected model metadata")
    observed_creation = {tags.get("creation_time"), tags.get("date")}
    if creation_time not in observed_creation:
        raise AudioExportError(f"{filename} does not contain the expected creation metadata")
    if (
        tags.get("whoopy_duration_ms") != duration_text
        and f"duration_ms={duration_text}" not in comment
    ):
        raise AudioExportError(f"{filename} does not contain the expected duration metadata")
    return {
        "title": title,
        "model": model,
        "creation_time": creation_time,
        "whoopy_duration_ms": duration_text,
        "evidence": json.dumps(tags, sort_keys=True, separators=(",", ":")),
    }


def _probe(ffprobe: Path, output: Path) -> dict[str, object]:
    completed = _run(
        (
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,duration:stream_tags:format=duration:format_tags",
            "-of",
            "json",
            str(output),
        ),
        timeout_seconds=30,
    )
    try:
        document = json.loads(completed.stdout)
        stream = document["streams"][0]
        file_format = document.get("format", {})
        if not isinstance(stream, dict) or not isinstance(file_format, dict):
            raise TypeError
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as error:
        raise AudioExportError(f"ffprobe returned invalid evidence for {output.name}") from error
    result: dict[str, object] = dict(stream)
    if not result.get("duration"):
        result["duration"] = file_format.get("duration")
    stream_tags = result.get("tags", {})
    format_tags = file_format.get("tags", {})
    if isinstance(stream_tags, dict) and isinstance(format_tags, dict):
        result["tags"] = {**format_tags, **stream_tags}
    return result


def _probe_duration_ms(probe: dict[str, object], filename: str) -> float:
    try:
        raw_duration = probe["duration"]
        if not isinstance(raw_duration, (str, int, float)):
            raise TypeError
        duration_ms = float(raw_duration) * 1_000
    except (KeyError, TypeError, ValueError) as error:
        raise AudioExportError(f"ffprobe reported no valid duration for {filename}") from error
    if duration_ms <= 0:
        raise AudioExportError(f"ffprobe reported an empty duration for {filename}")
    return round(duration_ms, 3)


def _positive_int(value: object, label: str) -> int:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as error:
        raise AudioExportError(f"ffprobe reported no valid {label}") from error
    if parsed <= 0:
        raise AudioExportError(f"ffprobe reported no valid {label}")
    return parsed


def _run(command: tuple[str, ...], *, timeout_seconds: int) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise AudioExportError(
            f"Audio tool timed out after {timeout_seconds} seconds while running "
            f"{Path(command[0]).name}"
        ) from error
    except OSError as error:
        raise AudioExportError(f"Could not start audio tool {command[0]}: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "no diagnostic output").strip()
        raise AudioExportError(
            f"{Path(command[0]).name} failed with exit code {completed.returncode}: "
            f"{detail[-2_000:]}"
        )
    return completed


def _write_manifest(path: Path, manifest: ExportManifest) -> None:
    payload = manifest.model_dump_json(indent=2).encode("utf-8") + b"\n"
    with path.open("xb") as destination:
        destination.write(payload)
        destination.flush()
        os.fsync(destination.fileno())


def _fsync_file(path: Path) -> None:
    with path.open("r+b") as artifact:
        artifact.flush()
        os.fsync(artifact.fileno())


def _activate_directory(staging: Path, destination: Path, backup: Path) -> None:
    moved_existing = False
    activated = False
    try:
        if destination.exists():
            if not destination.is_dir():
                raise AudioExportError(f"Export destination is not a directory: {destination}")
            os.replace(destination, backup)
            moved_existing = True
        os.replace(staging, destination)
        activated = True
        _fsync_directory(destination.parent)
    except (OSError, AudioExportError) as error:
        try:
            if activated:
                os.replace(destination, staging)
            if moved_existing:
                os.replace(backup, destination)
            if activated or moved_existing:
                _fsync_directory(destination.parent)
        except OSError as rollback_error:
            retained = f" Previous exports remain at {backup}." if backup.exists() else ""
            raise AudioExportError(
                f"Could not activate finished exports: {error}; "
                f"rollback failed: {rollback_error}.{retained}"
            ) from rollback_error
        if isinstance(error, AudioExportError):
            raise
        raise AudioExportError(f"Could not activate finished exports: {error}") from error
    if moved_existing:
        shutil.rmtree(backup)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
