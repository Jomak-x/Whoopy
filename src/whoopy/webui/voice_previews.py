"""Optional, integrity-checked local auditions; never model or speech qualification."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import wave
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MANIFEST_BYTES = 128 * 1024
MAX_WAV_BYTES = 32 * 1024 * 1024
MAX_PREVIEWS = 128
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class _PreviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    model_id: str
    voice_id: str
    path: str = Field(min_length=1, max_length=512)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    model_revision: str = Field(min_length=1, max_length=160)
    recorded_at: str
    human_quality: Literal["unrated"]
    label: str | None = Field(default=None, min_length=1, max_length=120)
    source: str | None = Field(default=None, min_length=1, max_length=200)
    controls: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=16)

    @field_validator("model_id", "voice_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not _ID.fullmatch(value) or value in {".", ".."}:
            raise ValueError("Invalid preview identifier")
        return value

    @field_validator("path")
    @classmethod
    def _relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or str(path) != value
            or any(part in {".", ".."} for part in path.parts)
            or any(char in value for char in "\\:?#\x00")
            or path.suffix.lower() != ".wav"
        ):
            raise ValueError("Preview must use a canonical relative WAV path")
        return value

    @field_validator("recorded_at")
    @classmethod
    def _date(cls, value: str) -> str:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError("recorded_at must use YYYY-MM-DD")
        return value

    @field_validator("label", "source", "model_revision")
    @classmethod
    def _plain_text(cls, value: str | None) -> str | None:
        if value is not None and any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Preview metadata must be plain text")
        return value

    @field_validator("controls")
    @classmethod
    def _controls(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", key):
                raise ValueError("Invalid control name")
            if isinstance(item, str) and (
                len(item) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in item)
            ):
                raise ValueError("Invalid control text")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("Control numbers must be finite")
            if isinstance(item, int) and abs(item) > 2**53:
                raise ValueError("Control integer is too large")
        return value


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    previews: list[_PreviewRecord] = Field(max_length=MAX_PREVIEWS)

    @field_validator("schema_version", mode="before")
    @classmethod
    def _version(cls, value: Any) -> Any:
        if type(value) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @model_validator(mode="after")
    def _unique_pairs(self) -> _Manifest:
        pairs = [(record.model_id, record.voice_id) for record in self.previews]
        if len(pairs) != len(set(pairs)):
            raise ValueError("Duplicate model/voice preview")
        return self


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON field")
        result[key] = value
    return result


def _read_local(root: Path, relative: str, limit: int) -> bytes:
    """Read one bounded regular file, refusing symlinks in its relative ancestry."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Preview directory is unavailable or symlinked")
    parts = PurePosixPath(relative).parts
    for index in range(len(parts)):
        if root.joinpath(*parts[: index + 1]).is_symlink():
            raise ValueError("Preview symlinks are not allowed")
    path = root.joinpath(*parts)
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Preview escapes its directory")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    if os.open in os.supports_dir_fd:
        # Pin every directory while walking, so a concurrent parent symlink
        # swap cannot redirect the open outside the trusted preview directory.
        directory_flag = getattr(os, "O_DIRECTORY", None)
        nofollow_flag = getattr(os, "O_NOFOLLOW", None)
        if directory_flag is None or nofollow_flag is None:
            raise ValueError("Secure directory-relative preview reads are unavailable")
        directory_flags = os.O_RDONLY | directory_flag | nofollow_flag
        parent = os.open(root, directory_flags)
        try:
            for part in parts[:-1]:
                child = os.open(part, directory_flags, dir_fd=parent)
                os.close(parent)
                parent = child
            descriptor = os.open(parts[-1], flags, dir_fd=parent)
        finally:
            os.close(parent)
    else:
        descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise ValueError("Preview file is empty, oversized, or not regular")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
    if len(data) != before.st_size or (before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ValueError("Preview changed while being read")
    # Also reject a parent or file changed into a symlink during the read.
    if any(root.joinpath(*parts[: index + 1]).is_symlink() for index in range(len(parts))):
        raise ValueError("Preview symlinks are not allowed")
    return data


def _wav_metadata(data: bytes) -> dict[str, int | float]:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("Preview must be a PCM RIFF WAV")
    if struct.unpack_from("<I", data, 4)[0] + 8 != len(data):
        raise ValueError("WAV container length is inconsistent")
    offset = 12
    chunks: dict[bytes, bytes] = {}
    count = 0
    while offset < len(data):
        count += 1
        if count > 1024 or offset + 8 > len(data):
            raise ValueError("WAV chunk header is invalid")
        kind, size = struct.unpack_from("<4sI", data, offset)
        start = offset + 8
        offset = start + size + size % 2
        if offset > len(data):
            raise ValueError("WAV chunk is truncated")
        if kind in {b"fmt ", b"data"}:
            if kind in chunks:
                raise ValueError("Duplicate WAV format or data chunk")
            chunks[kind] = data[start : start + size]
    fmt = chunks.get(b"fmt ", b"")
    if len(fmt) < 16 or len(fmt) > 128 or b"data" not in chunks:
        raise ValueError("WAV format or data is missing")
    encoding, channels, rate, byte_rate, alignment, bits = struct.unpack_from("<HHIIHH", fmt)
    if (
        encoding != 1
        or channels not in {1, 2}
        or bits not in {8, 16, 24, 32}
        or not 8000 <= rate <= 192000
        or alignment != channels * bits // 8
        or byte_rate != rate * alignment
        or len(chunks[b"data"]) % alignment
    ):
        raise ValueError("WAV must contain complete uncompressed PCM frames")
    with wave.open(BytesIO(data), "rb") as source:
        frames = source.getnframes()
        if not 0 < frames <= rate * 180:
            raise ValueError("Preview must contain positive audio no longer than 180 seconds")
        if len(source.readframes(frames + 1)) != frames * alignment:
            raise ValueError("WAV frames are truncated")
    return {
        "duration_seconds": frames / rate,
        "sample_rate": rate,
        "channels": channels,
        "sample_width_bytes": bits // 8,
        "frames": frames,
    }


@dataclass(frozen=True)
class VoicePreview:
    path: Path
    url: str
    metadata: dict[str, Any]
    wav_bytes: bytes


class VoicePreviewService:
    """Reverify local auditions on every lookup; missing configuration is empty."""

    def __init__(self, models_root: Path) -> None:
        self.models_root = Path(models_root)
        self.root = self.models_root / "voice-previews"

    def _manifest(self) -> _Manifest:
        if self.models_root.is_symlink():
            raise ValueError("Models root must not be a symlink")
        if not self.root.exists() or not (self.root / "index.json").exists():
            return _Manifest(schema_version=1, previews=[])
        raw = _read_local(self.root, "index.json", MAX_MANIFEST_BYTES)
        try:
            return _Manifest.model_validate(json.loads(raw, object_pairs_hook=_unique_object))
        except RecursionError as error:
            raise ValueError("Preview manifest nesting is excessive") from error

    def _resolve(self, record: _PreviewRecord) -> VoicePreview:
        data = _read_local(self.root, record.path, MAX_WAV_BYTES)
        if hashlib.sha256(data).hexdigest() != record.sha256:
            raise ValueError("Preview digest does not match")
        audio = _wav_metadata(data)
        url = f"/api/v1/voice-previews/{record.model_id}/{record.voice_id}"
        metadata = record.model_dump(exclude={"path"}, exclude_none=True)
        metadata.update(audio, url=url, integrity_verified=True, managed_ready=False)
        return VoicePreview(self.root / record.path, url, metadata, data)

    def lookup(self, model_id: str, voice_id: str) -> VoicePreview | None:
        try:
            for record in self._manifest().previews:
                if (record.model_id, record.voice_id) == (model_id, voice_id):
                    return self._resolve(record)
        except (OSError, ValueError, wave.Error, EOFError):
            return None
        return None

    def status(self) -> dict[str, Any]:
        result: dict[str, Any] = {"entries": [], "rejected": [], "error": None}
        try:
            records = self._manifest().previews
        except (OSError, ValueError, wave.Error, EOFError):
            result["error"] = "Local preview manifest is invalid or unavailable."
            return result
        for record in records:
            try:
                result["entries"].append(self._resolve(record).metadata)
            except (OSError, ValueError, wave.Error, EOFError):
                result["rejected"].append(
                    {
                        "model_id": record.model_id,
                        "voice_id": record.voice_id,
                        "reason": "Local WAV integrity or format check failed.",
                    }
                )
        return result

    def urls(self, model_id: str) -> dict[str, str]:
        return {
            entry["voice_id"]: entry["url"]
            for entry in self.status()["entries"]
            if entry["model_id"] == model_id
        }
