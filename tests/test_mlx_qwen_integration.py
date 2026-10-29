"""Exercise durable experimental generation with a stub worker, never MLX inference."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import struct
from array import array
from pathlib import Path
from typing import Any, ClassVar
from uuid import uuid4

import pytest
from pydantic import ValidationError

from whoopy.adapters.llm import LlamaCppScriptGenerator
from whoopy.adapters.tts.mlx_installation import MlxQwenInstallation
from whoopy.adapters.tts.mlx_qwen3_tts import (
    MLX_QWEN_BACKENDS,
    MlxEvidenceCache,
    MlxEvidenceCheckpoints,
    MlxProcessedSpeech,
    MlxQwenAdapter,
    MlxQwenSettings,
    adapter_source_sha256,
    pcm_from_native_float_wave,
    source_sha256,
)
from whoopy.audio.synthesis import InvalidSynthesisOutput, cache_key_for, synthesis_input_bytes
from whoopy.cli import _build_parser, _mlx_adapter_from_durable, main
from whoopy.hardware import HardwareSnapshot
from whoopy.meditation import GenerationError
from whoopy.pipeline import RunStore, TTSRunSettings
from whoopy.timeline import SpeechSegment

CUSTOM = "mlx-qwen3-custom-voice-1.7b-8bit"


def installation(tmp_path: Path, backend: str = CUSTOM) -> MlxQwenInstallation:
    return MlxQwenInstallation(
        runtime_directory=tmp_path / "runtime",
        model_directory=tmp_path / "weights",
        python_executable=tmp_path / "runtime/.venv/bin/python",
        runtime_revision="runtime-test",
        model_revision="model-test",
        installation_sha256="a" * 64,
        backend=backend,
        task=MLX_QWEN_BACKENDS[backend],
        repository="mlx-community/test",
    )


def durable(**changes: Any) -> TTSRunSettings:
    fields = dict(
        backend=CUSTOM,
        voice_name="Ryan",
        speaker_id=0,
        speed=1.0,
        num_threads=1,
        provider="mlx-metal",
        language="English",
        seed=42,
        use_reference=False,
        voice_mode="preset",
        experimental_installation_sha256="a" * 64,
        experimental_model_revision="model-test",
        experimental_runtime_revision="runtime-test",
        experimental_worker_sha256=source_sha256(Path("scripts/mlx_qwen3_tts_worker.py")),
        experimental_adapter_sha256=adapter_source_sha256(),
        experimental_classification="experimental_local",
        experimental_managed_ready=False,
    )
    fields.update(changes)
    return TTSRunSettings.model_validate(fields)


class FakeController:
    requests: ClassVar[list[dict[str, Any]]] = []
    commands: ClassVar[list[list[str]]] = []
    corrupt_pcm: ClassVar[bool] = False

    def __init__(self, *, command: list[str], **_kwargs: Any) -> None:
        self.command = command
        self.commands.append(command)
        self.running = False
        self.process_id = None
        self.root = Path(command[command.index("--raw-output-dir") + 1])

    def start(self) -> dict[str, Any]:
        self.running = True
        assert self.root.is_absolute() and self.root.is_dir()
        return {"status": "ready", "sample_rate": 24000, "device": "mlx-metal"}

    def request(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(request)
        frames = max(2400, round(len(request["text"].split()) * 60 / 133 * 24000))
        samples = array("h", (round(4000 * math.sin(index / 19)) for index in range(frames)))
        pcm = samples.tobytes()
        float_data = array("f", (value / 32767 for value in samples)).tobytes()
        fmt = struct.pack("<HHIIHH", 3, 1, 24000, 96000, 4, 32)
        body = b"WAVEfmt " + struct.pack("<I", 16) + fmt
        body += b"data" + struct.pack("<I", len(float_data)) + float_data
        folder = self.root / f"{request['native_artifact_key']}-{uuid4().hex}"
        folder.mkdir()
        wav, sidecar = folder / "native.wav", folder / "metadata.json"
        wav.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
        evidence = {
            "wav_path": str(wav),
            "sidecar_path": str(sidecar),
            "sha256": source_sha256(wav),
            "frames": frames,
            "sample_rate": 24000,
            "subtype": "FLOAT",
            "eos_observed": True,
            "native_artifact_key": request["native_artifact_key"],
            "segment_id": request["segment_id"],
            "pcm_s16le_sha256": hashlib.sha256(pcm).hexdigest(),
            "pcm_conversion": "float32_clip_scale_rint_even_s16le@1",
        }
        sidecar.write_text(
            json.dumps(
                {
                    **request,
                    **evidence,
                    "stop_evidence": {
                        "completion_valid": True,
                        "codec_eos_observed": True,
                        "segments": [
                            {
                                "codec_eos_observed": True,
                                "generated_codec_steps": 1,
                                "max_tokens": 192,
                            }
                        ],
                    },
                }
            )
        )
        evidence["sidecar_sha256"] = source_sha256(sidecar)
        return {
            "status": "ok",
            "pcm_s16le": base64.b64encode(
                b"\x01\x00" + pcm[2:] if self.corrupt_pcm else pcm
            ).decode(),
            "raw_evidence": evidence,
        }

    def close(self) -> None:
        self.running = False


@pytest.fixture
def stub_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    FakeController.requests.clear()
    FakeController.commands.clear()
    FakeController.corrupt_pcm = False
    monkeypatch.setattr(
        "whoopy.adapters.tts.mlx_qwen3_tts.JsonLineProcessController", FakeController
    )
    monkeypatch.setattr(
        "whoopy.cli.resolve_mlx_qwen_installation",
        lambda _root, backend, **_kwargs: installation(tmp_path, backend),
    )


@pytest.mark.parametrize("backend", MLX_QWEN_BACKENDS)
def test_explicit_cli_choice_does_not_enter_web_generation(backend: str) -> None:
    from whoopy.webui.api import GenerationRequest

    assert (
        _build_parser().parse_args(["generate", "test", "--tts-model", backend]).tts_model
        == backend
    )
    with pytest.raises(ValidationError):
        GenerationRequest.model_validate({"mode": "prompt", "text": "test", "tts_model": backend})


@pytest.mark.parametrize(
    "changes",
    [
        {"experimental_installation_sha256": None},
        {"experimental_managed_ready": True},
        {"use_reference": True},
        {"voice_mode": "reference"},
        {"voice_name": "unreviewed"},
        {"language": "French"},
        {"experimental_worker_sha256": None},
    ],
)
def test_durable_contract_refuses_incomplete_or_misleading_identity(
    changes: dict[str, Any],
) -> None:
    with pytest.raises(ValidationError):
        durable(**changes)


def test_durable_reconstruction_and_cache_separate_model_voice_and_code(
    tmp_path: Path, stub_runtime: None
) -> None:
    kwargs = dict(
        models_dir=tmp_path / "models/managed",
        config_dir=Path("config"),
        raw_output_directory=tmp_path / "raw",
    )
    first = _mlx_adapter_from_durable(durable(), **kwargs)
    second = _mlx_adapter_from_durable(durable(voice_name="Aiden"), **kwargs)
    assert first.cache_identity != second.cache_identity
    assert "managed_ready=false" in first.metadata.settings
    with pytest.raises(GenerationError, match="changed"):
        _mlx_adapter_from_durable(durable(experimental_installation_sha256="b" * 64), **kwargs)
    with pytest.raises(GenerationError, match="changed"):
        _mlx_adapter_from_durable(durable(experimental_worker_sha256="b" * 64), **kwargs)
    first.close()
    second.close()


def test_cache_requires_corresponding_native_evidence_and_preserves_isolated_launch(
    tmp_path: Path, stub_runtime: None
) -> None:
    raw = MlxQwenAdapter(
        MlxQwenSettings(
            installation=installation(tmp_path),
            worker_script=Path("scripts/mlx_qwen3_tts_worker.py"),
            models_root=tmp_path / "models",
            raw_output_directory=tmp_path / "native",
        )
    )
    processed = MlxProcessedSpeech(raw)
    cache = MlxEvidenceCache(tmp_path / "cache", tmp_path / "native")
    segment = SpeechSegment(id="speech-1", text="Settle into this moment.")
    try:
        audio = processed.synthesize(segment)
        key = cache_key_for(segment, processed)
        cache.store(
            key,
            audio,
            synthesis_inputs=synthesis_input_bytes(segment, processed),
            synthesizer_identity=processed.cache_identity,
        )
        assert cache.load(key, expected_synthesizer=processed) is not None
        assert len(FakeController.requests) == 1
        command = FakeController.commands[-1]
        assert command[1:4] == ["-I", "-B", "-X"]
        bytecode = Path(command[4].split("=", 1)[1])
        assert bytecode.is_dir()
        assert raw.last_evidence is not None
        Path(raw.last_evidence["wav_path"]).write_bytes(b"changed")
        assert cache.load(key, expected_synthesizer=processed) is None
    finally:
        processed.close()
    assert not bytecode.exists()


@pytest.mark.parametrize(
    "backend, voice",
    [(CUSTOM, "Ryan"), ("mlx-qwen3-voice-design-1.7b-8bit", "grounded_meditation")],
)
def test_actual_cli_script_generation_has_native_and_processed_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_runtime: None,
    backend: str,
    voice: str,
) -> None:
    monkeypatch.setattr(
        "whoopy.cli.inspect_hardware",
        lambda _: HardwareSnapshot(
            operating_system="darwin",
            architecture="arm64",
            cpu_count=8,
            total_ram_gb=48,
            available_ram_gb=30,
            free_disk_gb=100,
            accelerators=["metal"],
        ),
    )
    script = tmp_path / "script.md"
    script.write_text("Settle into this moment.\n\n[pause: 2s]\n\nNotice the support beneath you.")
    voice_arg = "--qwen-speaker" if backend == CUSTOM else "--qwen-voice-design-preset"
    assert (
        main(
            [
                "generate",
                "--script-file",
                str(script),
                "--tts-model",
                backend,
                voice_arg,
                voice,
                "--runs-dir",
                str(tmp_path / "runs"),
                "--models-dir",
                str(tmp_path / "models/managed"),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    run = tmp_path / "runs" / result["run_id"]
    assert result["status"] == "completed"
    assert (run / "narration.wav").is_file()
    assert len(list((run / "native-speech/cache-references").glob("*.json"))) == 2
    settings = json.loads((run / "resolved-config.json").read_text())["tts"]
    assert settings["experimental_managed_ready"] is False
    assert settings["voice_name"] == voice
    assert settings["experimental_classification"] == "experimental_local"
    assert all(request["task"] == MLX_QWEN_BACKENDS[backend] for request in FakeController.requests)
    manifest = json.loads((run / "audio-manifest.json").read_text())
    span = next(item for item in manifest["segments"] if item["segment_type"] == "SPEECH")
    checkpoints = MlxEvidenceCheckpoints(RunStore(tmp_path / "runs"))
    assert (
        checkpoints.load_completed(
            result["run_id"], span["segment_id"], cache_key=span["cache_key"]
        )
        is not None
    )
    reference_path = run / "native-speech/cache-references" / f"{span['cache_key']}.json"
    reference = json.loads(reference_path.read_text())
    Path(reference["native"]["wav_path"]).unlink()
    assert (
        checkpoints.load_completed(
            result["run_id"], span["segment_id"], cache_key=span["cache_key"]
        )
        is None
    )


def test_whole_session_high_profile_reaches_real_durable_mlx_pipeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stub_runtime: None,
) -> None:
    from tests.test_recipe_whole_session import WholeSessionAdapter

    monkeypatch.setattr(
        "whoopy.cli.inspect_hardware",
        lambda _: HardwareSnapshot(
            operating_system="darwin",
            architecture="arm64",
            cpu_count=8,
            total_ram_gb=48,
            available_ram_gb=30,
            free_disk_gb=100,
            accelerators=["metal"],
        ),
    )
    writer = WholeSessionAdapter()
    profiles = []

    def factory(_cls: Any, /, **kwargs: Any) -> WholeSessionAdapter:
        profiles.append(kwargs["profile_name"])
        return writer

    monkeypatch.setattr(LlamaCppScriptGenerator, "from_artifact_store", classmethod(factory))
    assert (
        main(
            [
                "generate",
                "Steady me before a meeting",
                "--writing-method",
                "recipe_whole_session",
                "--minutes",
                "1",
                "--profile",
                "high",
                "--tts-model",
                CUSTOM,
                "--qwen-speaker",
                "Ryan",
                "--runs-dir",
                str(tmp_path / "runs"),
                "--models-dir",
                str(tmp_path / "models/managed"),
                "--json",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    run = tmp_path / "runs" / result["run_id"]
    assert result["status"] == "completed" and result["source_kind"] == "generated_prompt"
    assert profiles == ["high"]
    assert writer.requests and FakeController.requests
    resolved = json.loads((run / "resolved-config.json").read_text())
    assert resolved["generation"]["writing_method"] == "recipe_whole_session"
    assert resolved["profile"] == "high"
    assert (run / "plan.json").is_file() and (run / "timeline.json").is_file()
    assert list((run / "raw-model-output").glob("*.json"))
    assert list((run / "native-speech/cache-references").glob("*.json"))


@pytest.mark.parametrize("misdeclare_hash", [False, True])
def test_equal_length_wrong_pcm_is_rejected_against_native_wave(
    tmp_path: Path, stub_runtime: None, monkeypatch: pytest.MonkeyPatch, misdeclare_hash: bool
) -> None:
    raw = MlxQwenAdapter(
        MlxQwenSettings(
            installation=installation(tmp_path),
            worker_script=Path("scripts/mlx_qwen3_tts_worker.py"),
            models_root=tmp_path / "models",
            raw_output_directory=tmp_path / "native",
        )
    )
    FakeController.corrupt_pcm = True
    if misdeclare_hash:
        original = FakeController.request

        def forged_hash(self: FakeController, request: dict[str, Any]) -> dict[str, Any]:
            response = original(self, request)
            evidence = response["raw_evidence"]
            evidence["pcm_s16le_sha256"] = hashlib.sha256(
                base64.b64decode(response["pcm_s16le"])
            ).hexdigest()
            sidecar = Path(evidence["sidecar_path"])
            document = json.loads(sidecar.read_text())
            document["pcm_s16le_sha256"] = evidence["pcm_s16le_sha256"]
            sidecar.write_text(json.dumps(document))
            evidence["sidecar_sha256"] = source_sha256(sidecar)
            return response

        monkeypatch.setattr(FakeController, "request", forged_hash)
    try:
        with pytest.raises(InvalidSynthesisOutput, match="PCM"):
            raw.synthesize(SpeechSegment(id="speech-1", text="Settle into this moment."))
        assert raw.last_evidence is None
        # Failed inference artifacts survive and are never promoted as cache references.
        assert list((tmp_path / "native").glob("*/native.wav"))
        assert not (tmp_path / "native/cache-references").exists()
    finally:
        raw.close()


def test_receipt_recomputes_distinct_native_and_processed_keys(
    tmp_path: Path, stub_runtime: None
) -> None:
    raw = MlxQwenAdapter(
        MlxQwenSettings(
            installation=installation(tmp_path),
            worker_script=Path("scripts/mlx_qwen3_tts_worker.py"),
            models_root=tmp_path / "models",
            raw_output_directory=tmp_path / "native",
        )
    )
    processed = MlxProcessedSpeech(raw)
    cache = MlxEvidenceCache(tmp_path / "cache", tmp_path / "native")
    segments = [
        SpeechSegment(id="speech-1", text="Settle into this moment."),
        SpeechSegment(id="speech-2", text="Hear the quiet surrounding sounds."),
    ]
    try:
        receipts = []
        for segment in segments:
            audio = processed.synthesize(segment)
            key = cache_key_for(segment, processed)
            cache.store(
                key,
                audio,
                synthesis_inputs=synthesis_input_bytes(segment, processed),
                synthesizer_identity=processed.cache_identity,
            )
            path = tmp_path / "native/cache-references" / f"{key}.json"
            receipts.append((path, json.loads(path.read_text())))
        path, first = receipts[0]
        assert first["cache_key"] != first["native_cache_key"]
        assert first["native_cache_key"] == first["native"]["native_artifact_key"]
        assert cache.load(first["cache_key"], expected_synthesizer=processed) is not None
        # Every referenced file/hash is valid, but belongs to a different typed input.
        first["native"] = receipts[1][1]["native"]
        path.write_text(json.dumps(first))
        assert cache.load(first["cache_key"], expected_synthesizer=processed) is None
    finally:
        processed.close()


def test_independent_float_conversion_clipping_and_ties_even() -> None:
    # Include real float32 values just either side of scaled half boundaries.
    values = [-1.25, -1.0, -0.125, -0.0, 0.125, 1.0, 1.25, 0.1426282525062561]
    data = struct.pack("<" + "f" * len(values), *values)
    fmt = struct.pack("<HHIIHH", 3, 1, 24000, 96000, 4, 32)
    body = b"WAVEfmt " + struct.pack("<I", 16) + fmt + b"data" + struct.pack("<I", len(data)) + data
    payload = b"RIFF" + struct.pack("<I", len(body)) + body
    assert struct.unpack("<8h", pcm_from_native_float_wave(payload)) == (
        -32767,
        -32767,
        -4096,
        0,
        4096,
        32767,
        32767,
        4674,  # float64 multiplication would incorrectly yield 4673
    )
    with pytest.raises(InvalidSynthesisOutput, match="size is inconsistent"):
        pcm_from_native_float_wave(payload[:-1])


@pytest.mark.parametrize("backend", MLX_QWEN_BACKENDS)
def test_explicit_numeric_speed_is_rejected_before_experimental_loading(
    backend: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    script = tmp_path / "script.md"
    script.write_text("Notice one ordinary sound. [pause: 1s]", encoding="utf-8")
    with pytest.raises(SystemExit) as error:
        main(
            [
                "generate",
                "--script-file",
                str(script),
                "--profile",
                "basic",
                "--tts-model",
                backend,
                "--speed",
                "0.8",
                "--audience",
                "lab",
                "--runs-dir",
                str(tmp_path / "runs"),
            ]
        )
    assert error.value.code == 2
    assert "does not support numeric --speed" in capsys.readouterr().err


@pytest.mark.parametrize("change", [None, "text", "delivery", "pcm", "native"])
def test_runner_binds_repeated_speech_to_shared_native_attempt(
    tmp_path: Path, stub_runtime: None, change: str | None
) -> None:
    from scripts.overnight_e2e import selected_native_evidence

    raw = MlxQwenAdapter(
        MlxQwenSettings(
            installation=installation(tmp_path),
            worker_script=Path("scripts/mlx_qwen3_tts_worker.py"),
            models_root=tmp_path / "models",
            raw_output_directory=tmp_path / "native-speech",
        )
    )
    processed = MlxProcessedSpeech(raw)
    cache = MlxEvidenceCache(tmp_path / "cache", tmp_path / "native-speech")
    first = SpeechSegment(id="speech-1", text="Return to the ordinary sound.")
    repeated = first.model_copy(update={"id": "speech-2"})
    try:
        audio = processed.synthesize(first)
        key = cache_key_for(first, processed)
        cache.store(
            key,
            audio,
            synthesis_inputs=synthesis_input_bytes(first, processed),
            synthesizer_identity=processed.cache_identity,
        )
        assert cache_key_for(repeated, processed) == key
        hit = cache.load(key, expected_synthesizer=processed)
        assert hit is not None
        delivered = {
            "segment_id": repeated.id,
            "cache_key": key,
            "pcm_sha256": hit.metadata.pcm_sha256,
        }
        wav, sidecar, native_id = selected_native_evidence(
            tmp_path, delivered, repeated.model_dump(mode="json")
        )
        assert native_id == first.id
        assert json.loads(sidecar.read_text())["segment_id"] == first.id
        assert len(FakeController.requests) == 1
        if change is None:
            return
        if change == "text":
            repeated = repeated.model_copy(update={"text": "Different words."})
        elif change == "delivery":
            repeated = repeated.model_copy(update={"delivery": "soothing"})
        elif change == "pcm":
            delivered["pcm_sha256"] = "0" * 64
        else:
            wav.write_bytes(b"changed native waveform")
        with pytest.raises(ValueError, match=r"synthesis inputs|evidence does not verify"):
            selected_native_evidence(tmp_path, delivered, repeated.model_dump(mode="json"))
    finally:
        processed.close()
