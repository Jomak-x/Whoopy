from __future__ import annotations

import base64
import math
import wave
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from whoopy.adapters.tts import Qwen3TTSAdapter
from whoopy.adapters.tts.qwen3_tts import Qwen3TTSInstallation
from whoopy.cli import (
    QWEN_BACKEND_VARIANTS,
    _build_parser,
    _qwen_adapter_from_durable,
)
from whoopy.meditation import GenerationError
from whoopy.pipeline import TTSRunSettings
from whoopy.timeline import SpeechSegment, VoiceMode
from whoopy.voice_references import VoiceConsent, VoiceReferenceManager


def _installation(tmp_path: Path, backend: str) -> Path:
    variant = QWEN_BACKEND_VARIANTS[backend]
    models_dir = tmp_path / "models" / "managed"
    runtime = tmp_path / "models" / "managed" / "runtimes" / "qwen3-tts"
    model = tmp_path / "models" / "managed" / "model-packs" / f"qwen3-tts-{variant}" / "files"
    (runtime / ".venv/bin").mkdir(parents=True)
    (runtime / ".venv/bin/python").write_text("python", encoding="utf-8")
    (runtime / ".whoopy-runtime.json").write_text(
        '{"schema_version":1,"runtime_id":"qwen3-tts","revision":"runtime-test",'
        '"upstream_revision":"upstream-test","target":"darwin-arm64",'
        '"bundle_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"installed_tree_sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
        '"installed_at":"2026-08-05T00:00:00Z"}',
        encoding="utf-8",
    )
    model.mkdir(parents=True)
    (model / "config.json").write_text("{}", encoding="utf-8")
    (model / "model.safetensors").write_bytes(b"checkpoint")
    (model / ".whoopy-model.json").write_text(
        ('{"schema_version":1,"variant":"' + variant + '","model_revision":"model-test"}'),
        encoding="utf-8",
    )
    return models_dir


def _durable(backend: str, **changes: object) -> TTSRunSettings:
    variant = QWEN_BACKEND_VARIANTS[backend]
    if variant.startswith("custom-voice"):
        voice_name = "Serena"
        voice_mode = "preset"
    elif variant == "voice-design-1.7b":
        voice_name = "warm_meditation"
        voice_mode = "designed"
    else:
        voice_name = "owned-voice"
        voice_mode = "reference"
    values: dict[str, object] = {
        "backend": backend,
        "voice_name": voice_name,
        "speaker_id": 0,
        "speed": 1.0,
        "num_threads": 1,
        "provider": "auto",
        "language": "English",
        "seed": 7,
        "voice_mode": voice_mode,
        "qwen_model_revision": "model-test",
        "qwen_runtime_revision": "runtime-test",
    }
    values.update(changes)
    return TTSRunSettings.model_validate(values)


@pytest.mark.parametrize("backend", list(QWEN_BACKEND_VARIANTS))
def test_cli_parser_exposes_every_qwen_variant_as_an_explicit_choice(backend: str) -> None:
    arguments = _build_parser().parse_args(
        ["generate", "--script-file", "meditation.md", "--tts-model", backend]
    )

    assert arguments.tts_model == backend


@pytest.mark.parametrize(
    ("backend", "mode"),
    [
        ("qwen3-custom-voice-0.6b", "preset"),
        ("qwen3-custom-voice-1.7b", "preset"),
        ("qwen3-voice-design-1.7b", "designed"),
    ],
)
def test_durable_qwen_non_clone_contracts_require_exact_revision_and_mode(
    backend: str, mode: str
) -> None:
    settings = _durable(backend)
    assert settings.voice_mode == mode

    with pytest.raises(ValidationError, match="exact model and runtime revisions"):
        _durable(backend, qwen_model_revision=None)


def test_qwen_base_durable_contract_requires_complete_consented_revision() -> None:
    with pytest.raises(ValidationError, match="complete consented reference revision"):
        _durable("qwen3-base-clone-0.6b")


class _FakeQwenController:
    requests: ClassVar[list[dict[str, object]]] = []

    def __init__(self, **_kwargs: Any) -> None:
        self.running = False
        self.process_id = 123

    def start(self) -> dict[str, object]:
        self.running = True
        return {"status": "ready", "sample_rate": 24_000, "device": "cpu"}

    def request(self, request: dict[str, object]) -> dict[str, object]:
        self.requests.append(request)
        return {
            "status": "ok",
            "pcm_s16le": base64.b64encode(b"\x00\x10" * 2_400).decode("ascii"),
        }

    def close(self) -> None:
        self.running = False


@pytest.mark.parametrize(
    ("backend", "voice_mode", "expressive"),
    [
        ("qwen3-custom-voice-0.6b", "preset", False),
        ("qwen3-custom-voice-1.7b", "preset", True),
        ("qwen3-voice-design-1.7b", "designed", True),
    ],
)
def test_durable_reconstruction_uses_real_qwen_adapter_and_worker_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    voice_mode: VoiceMode,
    expressive: bool,
) -> None:
    models_dir = _installation(tmp_path, backend)
    variant = QWEN_BACKEND_VARIANTS[backend]
    monkeypatch.setattr(
        "whoopy.cli.resolve_qwen3_tts_installation",
        lambda _root, _variant, *, registry_path: Qwen3TTSInstallation(
            runtime_directory=tmp_path / "models/managed/runtimes/qwen3-tts",
            model_directory=(
                tmp_path / "models/managed/model-packs" / f"qwen3-tts-{variant}" / "files"
            ),
            runtime_revision="runtime-test",
            model_revision="model-test",
        ),
    )
    monkeypatch.setattr(
        "whoopy.adapters.tts.qwen3_tts.JsonLineProcessController", _FakeQwenController
    )
    _FakeQwenController.requests.clear()

    adapter = _qwen_adapter_from_durable(_durable(backend), models_dir=models_dir)
    try:
        audio = adapter.synthesize(
            SpeechSegment(
                id="speech-1",
                text="Settle into this moment.",
                delivery="calm" if expressive else "neutral",
                voice_mode=voice_mode,
            )
        )
    finally:
        adapter.close()

    assert isinstance(adapter, Qwen3TTSAdapter)
    assert audio.frame_count == 2_400
    assert _FakeQwenController.requests[0]["text"] == "Settle into this moment."
    if expressive:
        assert "calm" in str(_FakeQwenController.requests[0]["instruction"])
    else:
        assert "instruction" not in _FakeQwenController.requests[0]
    assert "[delivery" not in str(_FakeQwenController.requests[0])


def _reference_wav(path: Path) -> None:
    rate = 24_000
    samples = array(
        "h",
        (round(4_000 * math.sin(2 * math.pi * 220 * index / rate)) for index in range(rate * 2)),
    )
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())


@pytest.mark.parametrize("backend", ["qwen3-base-clone-0.6b", "qwen3-base-clone-1.7b"])
def test_base_reconstruction_uses_the_recorded_immutable_reference_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    models_dir = _installation(tmp_path, backend)
    variant = QWEN_BACKEND_VARIANTS[backend]
    installation = Qwen3TTSInstallation(
        runtime_directory=tmp_path / "models/managed/runtimes/qwen3-tts",
        model_directory=(
            tmp_path / "models/managed/model-packs" / f"qwen3-tts-{variant}" / "files"
        ),
        runtime_revision="runtime-test",
        model_revision="model-test",
    )
    monkeypatch.setattr(
        "whoopy.cli.resolve_qwen3_tts_installation",
        lambda _root, _variant, *, registry_path: installation,
    )
    source = tmp_path / "reference.wav"
    _reference_wav(source)
    manager = VoiceReferenceManager(tmp_path / "models/voice-references")
    reference = manager.import_wav(
        reference_id="owned-voice",
        display_name="Owned voice",
        source_wav=source,
        transcript="This is the exact reference transcript.",
        consent=VoiceConsent(
            confirmed=True,
            scope="local_voice_cloning_experiment_only",
            source_kind="user_provided",
            rights_attestation="I own this voice or have permission to clone it.",
            recorded_at=datetime(2026, 8, 5, tzinfo=UTC),
        ),
    )
    durable = _durable(
        backend,
        reference_id=reference.reference_id,
        reference_revision=reference.revision,
        reference_revision_id=reference.revision_id,
        reference_audio_sha256=reference.audio_sha256,
        reference_transcript_sha256=reference.transcript_sha256,
        reference_consent_scope=reference.consent.scope,
    )

    adapter = _qwen_adapter_from_durable(durable, models_dir=models_dir)
    adapter.close()
    assert adapter.settings.reference_audio == manager.root / reference.audio_path
    assert adapter.settings.consent_scope == "local_voice_cloning_experiment_only"

    monkeypatch.setattr(
        "whoopy.cli.resolve_qwen3_tts_installation",
        lambda _root, _variant, *, registry_path: Qwen3TTSInstallation(
            runtime_directory=installation.runtime_directory,
            model_directory=installation.model_directory,
            runtime_revision="different",
            model_revision="model-test",
        ),
    )
    with pytest.raises(GenerationError, match="revision changed"):
        _qwen_adapter_from_durable(durable, models_dir=models_dir)


def test_qwen_reconstruction_uses_selected_registry_and_defers_live_memory_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from whoopy.audio.synthesis import TransientSynthesisError
    from whoopy.hardware import HardwareSnapshot
    from whoopy.model_packs.registry import HardwareRequirement

    backend = "qwen3-custom-voice-0.6b"
    models_dir = _installation(tmp_path, backend)
    config_dir = tmp_path / "custom-config"
    requirement = HardwareRequirement(
        min_total_ram_gb=8,
        min_available_ram_gb=4,
        min_free_disk_gb=0,
        accelerator_any_of=["cpu"],
    )

    def resolve(_root: Path, _variant: str, *, registry_path: Path) -> Qwen3TTSInstallation:
        assert registry_path == config_dir / "model_packs.yaml"
        return Qwen3TTSInstallation(
            runtime_directory=tmp_path / "models/managed/runtimes/qwen3-tts",
            model_directory=tmp_path
            / "models/managed/model-packs/qwen3-tts-custom-voice-0.6b/files",
            runtime_revision="runtime-test",
            model_revision="model-test",
            hardware_requirement=requirement,
        )

    monkeypatch.setattr("whoopy.cli.resolve_qwen3_tts_installation", resolve)
    monkeypatch.setattr(
        "whoopy.adapters.tts.qwen3_tts.inspect_hardware",
        lambda _path: HardwareSnapshot(
            operating_system="darwin",
            architecture="arm64",
            cpu_count=8,
            total_ram_gb=48,
            available_ram_gb=0,
            free_disk_gb=100,
            accelerators=["cpu"],
        ),
    )
    adapter = _qwen_adapter_from_durable(
        _durable(backend), models_dir=models_dir, config_dir=config_dir
    )
    assert adapter.runtime_process_id is None
    with pytest.raises(TransientSynthesisError, match="live resource check"):
        adapter.prepare()
    assert adapter.runtime_process_id is None
    adapter.close()
