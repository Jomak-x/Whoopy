from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml
from pytest import MonkeyPatch

import whoopy.model_packs.resolution as resolution
from whoopy.model_packs.operations import ModelPackOperationError
from whoopy.model_packs.registry import (
    DigestSpec,
    FileRole,
    HardwareRequirement,
    ModelPackSpec,
    ModelPackState,
    ModelPackStatus,
    PinnedFileSpec,
    RuntimeSpec,
)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _write_reference_manifest(config: Path, models: Path) -> Path:
    audio = b"RIFF-explicit-consented-audio"
    transcript = b"Please speak slowly and warmly."
    audio_path = models / "references" / "voice.wav"
    text_path = models / "references" / "voice.txt"
    audio_path.parent.mkdir(parents=True)
    audio_path.write_bytes(audio)
    text_path.write_bytes(transcript)
    path = config / "voice_references.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "default_reference_id": "reviewed-local-voice",
                "references": [
                    {
                        "reference_id": "reviewed-local-voice",
                        "display_name": "Reviewed local voice",
                        "audio_path": "references/voice.wav",
                        "audio_size_bytes": len(audio),
                        "audio_sha256": _sha256(audio),
                        "transcript_path": "references/voice.txt",
                        "transcript_size_bytes": len(transcript),
                        "transcript_sha256": _sha256(transcript),
                        "consent_confirmed": True,
                        "consent_scope": "local_voice_cloning_experiment_only",
                        "source_kind": "user_provided",
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _fish_pack() -> ModelPackSpec:
    return ModelPackSpec(
        pack_id="fish-speech-1.4",
        display_name="Fish",
        revision="pinned-revision",
        source_repository="https://example.test/fish",
        license_id="CC-BY-NC-SA-4.0",
        license_url="https://example.test/license",
        commercial_use_allowed=False,
        license_notice="Non-commercial only.",
        managed_directory=Path("managed/model-packs/fish/files"),
        supported_platforms=["all"],
        files=[
            PinnedFileSpec(
                path=Path("model.pth"),
                size_bytes=1,
                digest=DigestSpec(algorithm="sha256", value="0" * 64),
                role=FileRole.MODEL,
            )
        ],
        runtime=RuntimeSpec(
            runtime_id="fish-speech",
            revision="runtime-revision",
            candidate_directories=[Path("runtimes/fish")],
            required_markers=[Path("marker")],
        ),
        hardware=HardwareRequirement(
            min_total_ram_gb=1,
            min_available_ram_gb=1,
            min_free_disk_gb=0,
            accelerator_any_of=["cpu"],
        ),
    )


def _ready_status(pack: ModelPackSpec, checkpoint: Path) -> ModelPackStatus:
    return ModelPackStatus(
        pack_id=pack.pack_id,
        display_name=pack.display_name,
        revision=pack.revision,
        state=ModelPackState.READY,
        selected_directory=checkpoint,
        license_id=pack.license_id,
        license_url=pack.license_url,
        commercial_use_allowed=pack.commercial_use_allowed,
        license_notice=pack.license_notice,
        files=[],
        checks=[],
    )


def test_reference_resolution_uses_only_the_declared_hash_bound_files(tmp_path: Path) -> None:
    models = tmp_path / "models"
    manifest = _write_reference_manifest(tmp_path / "config", models)
    # An attractive filename elsewhere must never influence selection.
    unrelated = models / "other" / "whoopy-reference.wav"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_bytes(b"unreviewed voice")

    resolved = resolution.resolve_voice_reference(manifest, models_root=models)

    assert resolved.reference_id == "reviewed-local-voice"
    assert resolved.audio_path == (models / "references" / "voice.wav").resolve()
    assert resolved.transcript_path == (models / "references" / "voice.txt").resolve()


def test_reference_resolution_rejects_changed_bytes_and_symlinks(tmp_path: Path) -> None:
    models = tmp_path / "models"
    manifest = _write_reference_manifest(tmp_path / "config", models)
    audio = models / "references" / "voice.wav"
    audio.write_bytes(b"same size is not enough........")
    with pytest.raises(ModelPackOperationError, match="does not match"):
        resolution.resolve_voice_reference(manifest, models_root=models)

    audio.unlink()
    outside = tmp_path / "outside.wav"
    outside.write_bytes(b"RIFF-explicit-consented-audio")
    audio.symlink_to(outside)
    with pytest.raises(ModelPackOperationError, match="symlink"):
        resolution.resolve_voice_reference(manifest, models_root=models)


def test_selected_tts_backend_resolves_its_ready_managed_checkpoint(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    models = tmp_path / "models"
    references = _write_reference_manifest(tmp_path / "config", models)
    runtime = models / "runtimes" / "fish"
    runtime.mkdir(parents=True)
    (runtime / "marker").write_text("ready", encoding="utf-8")
    checkpoint = models / "managed" / "model-packs" / "fish" / "files"
    checkpoint.mkdir(parents=True)
    pack = _fish_pack()
    status = _ready_status(pack, checkpoint)

    class FakeRegistry:
        models_root = models

        def get(self, pack_id: str) -> ModelPackSpec:
            assert pack_id == "fish-speech-1.4"
            return pack

        def inspect(self, pack_id: str) -> ModelPackStatus:
            assert pack_id == "fish-speech-1.4"
            return status

    def fake_load(_path: Path, *, models_root: Path) -> FakeRegistry:
        assert models_root == models
        return FakeRegistry()

    monkeypatch.setattr(resolution, "load_model_pack_registry", fake_load)

    selected = resolution.resolve_tts_model_pack(
        "fish-1.4",
        registry_path=tmp_path / "config" / "model_packs.yaml",
        references_path=references,
        models_root=models,
    )

    assert selected.checkpoint_directory == checkpoint
    assert selected.runtime_directory == runtime
    assert selected.reference.reference_id == "reviewed-local-voice"


def test_artifact_store_root_mapping_preserves_custom_model_roots(tmp_path: Path) -> None:
    assert resolution.models_root_from_artifact_store(tmp_path / "models" / "managed") == (
        tmp_path / "models"
    )
    assert resolution.models_root_from_artifact_store(tmp_path / "custom") == (tmp_path / "custom")
