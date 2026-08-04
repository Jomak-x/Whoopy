"""Resolve ready model packs and explicit consented voice references.

Generation, resume, smoke tests, and the Studio use this one boundary. Nothing
in this module downloads, loads, or imports a model runtime.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from whoopy.model_packs.operations import ModelPackOperationError
from whoopy.model_packs.registry import (
    ModelPackRegistry,
    ModelPackSpec,
    ModelPackState,
    load_model_pack_registry,
)

_SAFE_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
OptionalTTSBackend = Literal["fish-1.4", "moss-local-v1.5", "moss-v1.5"]
_BACKEND_PACK_IDS: dict[OptionalTTSBackend, str] = {
    "fish-1.4": "fish-speech-1.4",
    "moss-local-v1.5": "moss-local-5b",
    "moss-v1.5": "moss-8b",
}


def _safe_relative(path: Path) -> Path:
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("reference paths must be safe and relative to the models root")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verified_reference_path(models_root: Path, relative: Path, *, label: str) -> Path:
    """Resolve one declared file without permitting symlink indirection.

    A reference recording can influence cloned speech.  Rejecting symlinks in
    every path component prevents an unrelated file from being swapped in
    after the checked-in manifest has been reviewed.
    """

    candidate = models_root / _safe_relative(relative)
    current = models_root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise ModelPackOperationError(f"Consented reference {label} may not use symlinks.")
    try:
        root = models_root.resolve(strict=True)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (FileNotFoundError, OSError, ValueError) as error:
        raise ModelPackOperationError(
            f"Consented reference {label} is missing or outside the models root: {candidate}"
        ) from error
    if not resolved.is_file():
        raise ModelPackOperationError(f"Consented reference {label} is not a file: {candidate}")
    return resolved


class VoiceReferenceSpec(BaseModel):
    """Pinned identity and consent scope for one user-owned reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: str
    display_name: str = Field(min_length=1, max_length=120)
    audio_path: Path
    audio_size_bytes: int = Field(gt=0)
    audio_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    transcript_path: Path
    transcript_size_bytes: int = Field(gt=0)
    transcript_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    consent_confirmed: bool
    consent_scope: Literal["local_voice_cloning_experiment_only"]
    source_kind: Literal["user_provided", "project_generated"]

    @model_validator(mode="after")
    def _safe_identity(self) -> VoiceReferenceSpec:
        if _SAFE_ID.fullmatch(self.reference_id) is None:
            raise ValueError("reference_id must be a safe portable identifier")
        _safe_relative(self.audio_path)
        _safe_relative(self.transcript_path)
        if self.audio_path == self.transcript_path:
            raise ValueError("reference audio and transcript must be different files")
        if not self.consent_confirmed:
            raise ValueError("voice reference consent must be confirmed explicitly")
        return self


class VoiceReferenceManifest(BaseModel):
    """Versioned collection with one explicit default selection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    default_reference_id: str
    references: tuple[VoiceReferenceSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_and_default(self) -> VoiceReferenceManifest:
        ids = [item.reference_id for item in self.references]
        if len(ids) != len(set(ids)):
            raise ValueError("voice reference IDs must be unique")
        if self.default_reference_id not in ids:
            raise ValueError("default voice reference is not declared")
        return self

    def default(self) -> VoiceReferenceSpec:
        return next(
            item for item in self.references if item.reference_id == self.default_reference_id
        )


class ResolvedVoiceReference(BaseModel):
    """Verified local reference paths safe to pass to an adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    reference_id: str
    audio_path: Path
    transcript_path: Path
    audio_sha256: str
    transcript_sha256: str
    consent_scope: str


class ResolvedTTSModelPack(BaseModel):
    """All exact local paths needed by one optional TTS backend."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: OptionalTTSBackend
    pack_id: str
    revision: str
    runtime_directory: Path
    checkpoint_directory: Path
    codec_directory: Path | None = None
    reference: ResolvedVoiceReference


def models_root_from_artifact_store(artifact_store_root: Path) -> Path:
    """Map the baseline `models/managed` store to the shared `models` root."""

    return (
        artifact_store_root.parent if artifact_store_root.name == "managed" else artifact_store_root
    )


def load_voice_reference_manifest(path: Path) -> VoiceReferenceManifest:
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ModelPackOperationError(
            f"Could not load voice references from {path}: {error}"
        ) from error
    try:
        return VoiceReferenceManifest.model_validate(document)
    except ValueError as error:
        raise ModelPackOperationError(
            f"Invalid voice reference manifest {path}: {error}"
        ) from error


def resolve_voice_reference(path: Path, *, models_root: Path) -> ResolvedVoiceReference:
    spec = load_voice_reference_manifest(path).default()
    audio = _verified_reference_path(models_root, spec.audio_path, label="audio")
    transcript = _verified_reference_path(models_root, spec.transcript_path, label="transcript")
    for label, candidate, size, digest in (
        ("audio", audio, spec.audio_size_bytes, spec.audio_sha256),
        ("transcript", transcript, spec.transcript_size_bytes, spec.transcript_sha256),
    ):
        if candidate.stat().st_size != size or _sha256(candidate) != digest:
            raise ModelPackOperationError(
                f"Consented reference {label} does not match {spec.reference_id}."
            )
    return ResolvedVoiceReference(
        reference_id=spec.reference_id,
        audio_path=audio,
        transcript_path=transcript,
        audio_sha256=spec.audio_sha256,
        transcript_sha256=spec.transcript_sha256,
        consent_scope=spec.consent_scope,
    )


def _runtime_directory(registry: ModelPackRegistry, pack: ModelPackSpec) -> Path:
    for relative in pack.runtime.candidate_directories:
        candidate = registry.models_root / relative
        if all((candidate / marker).is_file() for marker in pack.runtime.required_markers):
            return candidate
    raise ModelPackOperationError(
        f"No complete isolated {pack.runtime.runtime_id} runtime exists for {pack.pack_id}."
    )


def resolve_tts_model_pack(
    backend: OptionalTTSBackend,
    *,
    registry_path: Path,
    references_path: Path,
    models_root: Path,
    require_ready: bool = True,
) -> ResolvedTTSModelPack:
    """Resolve a selected pack without falling back to experimental constants."""

    registry = load_model_pack_registry(registry_path, models_root=models_root)
    pack_id = _BACKEND_PACK_IDS[backend]
    pack = registry.get(pack_id)
    status = registry.inspect(pack_id)
    allowed = (
        {ModelPackState.READY}
        if require_ready
        else {
            ModelPackState.INSTALLED,
            ModelPackState.READY,
        }
    )
    if status.state not in allowed:
        raise ModelPackOperationError(
            f"Model pack {pack_id} is {status.state}; run `whoopy models pack list` "
            "and its offline smoke test first."
        )
    codec_directory: Path | None = None
    if backend.startswith("moss-"):
        codec = registry.inspect("moss-audio-tokenizer-v2")
        if codec.state is not ModelPackState.READY:
            raise ModelPackOperationError(
                f"MOSS Audio Tokenizer v2 is {codec.state}; smoke-test a MOSS pack first."
            )
        codec_directory = codec.selected_directory
    return ResolvedTTSModelPack(
        backend=backend,
        pack_id=pack_id,
        revision=pack.revision,
        runtime_directory=_runtime_directory(registry, pack),
        checkpoint_directory=status.selected_directory,
        codec_directory=codec_directory,
        reference=resolve_voice_reference(references_path, models_root=models_root),
    )
