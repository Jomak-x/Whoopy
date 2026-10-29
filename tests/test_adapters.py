from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest

from tests.adapter_contracts import (
    assert_script_generator_contract,
    assert_speech_synthesizer_contract,
)
from whoopy.adapters.llm.llama_cpp import (
    LlamaCppScriptGenerator,
    LlamaCppSettings,
    LlamaProcessResult,
    _assistant_text,
    _llama_timing_evidence,
    _run_process,
)
from whoopy.adapters.tts.fish_speech import FishSpeech14Adapter
from whoopy.adapters.tts.moss_tts import (
    MOSS_DURATION_BUDGET_STRATEGY,
    MossTTSAdapter,
    MossTTSSettings,
    moss_duration_budget,
)
from whoopy.adapters.tts.sherpa_onnx import (
    SherpaOnnxKokoroAdapter,
    SherpaOnnxSettings,
)
from whoopy.audio.fixture import FixtureSpeechSynthesizer
from whoopy.audio.synthesis import (
    FatalSynthesisError,
    InvalidSynthesisOutput,
    TransientSynthesisError,
)
from whoopy.ports import (
    FatalAdapterError,
    InvalidAdapterOutput,
    ScriptGenerationRequest,
    TransientAdapterError,
)
from whoopy.timeline import PaceIntent, PronunciationOverride, SpeechSegment


def _llama_files(tmp_path: Path) -> tuple[Path, Path]:
    executable = tmp_path / "llama-cli"
    model = tmp_path / "model.gguf"
    executable.write_bytes(b"test executable")
    model.write_bytes(b"test model")
    return executable, model


def test_fixture_synthesizer_passes_the_production_speech_contract() -> None:
    assert_speech_synthesizer_contract(FixtureSpeechSynthesizer())


def test_llama_adapter_passes_contract_without_putting_prompt_in_process_arguments(
    tmp_path: Path,
) -> None:
    executable, model = _llama_files(tmp_path)
    commands: list[list[str]] = []
    prompt_text = "Return one calm sentence."

    def runner(command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        command_list = list(command)
        commands.append(command_list)
        prompt_path = Path(command_list[command_list.index("--file") + 1])
        assert prompt_path.read_text(encoding="utf-8") == prompt_text
        output_path = Path(command_list[command_list.index("--output-file") + 1])
        output_path.write_text("Welcome to a calmer moment.", encoding="utf-8")
        return subprocess.CompletedProcess(command_list, 0, stdout="", stderr="")

    ticks = iter([10.0, 10.25])
    adapter = LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="Qwen/test@revision#model.gguf",
        runtime_version="test-runtime",
        license_id="Apache-2.0",
        device="cpu",
        process_runner=runner,
        monotonic=lambda: next(ticks),
    )

    assert_script_generator_contract(adapter)

    command = commands[0]
    assert prompt_text not in command
    assert "--offline" in command
    assert "--single-turn" in command
    assert "--reasoning" in command
    assert adapter.metadata.versioned_model_id == "Qwen/test@revision#model.gguf"


def test_llama_adapter_classifies_timeout_as_transient(tmp_path: Path) -> None:
    executable, model = _llama_files(tmp_path)

    def timeout_runner(
        command: Sequence[str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, timeout)

    adapter = LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="test/model@1",
        runtime_version="1",
        license_id="Apache-2.0",
        device="cpu",
        settings=LlamaCppSettings(timeout_seconds=1),
        process_runner=timeout_runner,
    )

    with pytest.raises(TransientAdapterError, match="timeout"):
        adapter.generate(ScriptGenerationRequest(prompt="A prompt."))


def test_default_llama_runner_bounds_and_reaps_a_hung_process() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        _run_process(
            [sys.executable, "-c", "import time; time.sleep(120)"],
            0.05,
        )


def test_default_llama_runner_measures_observable_model_ready_signal() -> None:
    result = _run_process(
        [
            sys.executable,
            "-c",
            ("import time; print('available commands:', flush=True); time.sleep(0.03)"),
        ],
        1,
    )

    assert result.completed.returncode == 0
    assert result.load_elapsed_seconds is not None
    assert result.load_elapsed_seconds >= 0
    assert result.load_elapsed_unavailable_reason is None


def test_llama_adapter_classifies_nonzero_exit_as_fatal(tmp_path: Path) -> None:
    executable, model = _llama_files(tmp_path)

    def failed_runner(
        command: Sequence[str],
        _timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(command), 2, stdout="", stderr="invalid GGUF")

    adapter = LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="test/model@1",
        runtime_version="1",
        license_id="Apache-2.0",
        device="cpu",
        process_runner=failed_runner,
    )

    with pytest.raises(FatalAdapterError, match="invalid GGUF"):
        adapter.generate(ScriptGenerationRequest(prompt="A prompt."))


def test_llama_adapter_delivers_requested_schema_to_native_process(tmp_path: Path) -> None:
    executable, model = _llama_files(tmp_path)
    schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    consumed: list[dict[str, Any]] = []

    def runner(command: Sequence[str], _timeout: float) -> subprocess.CompletedProcess[str]:
        argv = list(command)
        path = Path(argv[argv.index("--json-schema-file") + 1])
        consumed.append(json.loads(path.read_text()))
        Path(argv[argv.index("--output-file") + 1]).write_text('{"message":"original prose"}')
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    adapter = LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="test/model@1",
        runtime_version="1",
        license_id="Apache-2.0",
        device="cpu",
        process_runner=runner,
    )
    result = adapter.generate(
        ScriptGenerationRequest(prompt="Write it.", response_json_schema=schema)
    )
    assert consumed == [schema]
    assert result.text == '{"message":"original prose"}'


def test_llama_adapter_rejects_empty_output(tmp_path: Path) -> None:
    executable, model = _llama_files(tmp_path)

    def empty_runner(
        command: Sequence[str],
        _timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        command_list = list(command)
        output_path = Path(command_list[command_list.index("--output-file") + 1])
        output_path.write_text(" \n", encoding="utf-8")
        return subprocess.CompletedProcess(command_list, 0, stdout="", stderr="")

    adapter = LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="test/model@1",
        runtime_version="1",
        license_id="Apache-2.0",
        device="cpu",
        process_runner=empty_runner,
    )

    with pytest.raises(InvalidAdapterOutput, match="empty text"):
        adapter.generate(ScriptGenerationRequest(prompt="A prompt."))


def test_llama_adapter_removes_single_turn_transcript_wrapper() -> None:
    output = "User:\nWrite one sentence.\n\nAssistant:\nWelcome to this moment.\n"

    assert _assistant_text(output) == "Welcome to this moment."


def test_llama_parser_accepts_pinned_cli_generation_summary() -> None:
    evidence = _llama_timing_evidence("[ Prompt: 246.2 t/s | Generation: 135.6 t/s ]")

    assert evidence["output_tokens_per_second"] == pytest.approx(135.6)


def test_llama_adapter_persists_parsed_runtime_and_measured_process_evidence(
    tmp_path: Path,
) -> None:
    executable, model = _llama_files(tmp_path)

    def measured_runner(command: Sequence[str], _timeout: float) -> LlamaProcessResult:
        command_list = list(command)
        output_path = Path(command_list[command_list.index("--output-file") + 1])
        output_path.write_text("Welcome to this moment.", encoding="utf-8")
        diagnostic = "\n".join(
            (
                "llama_model_load: using device Metal0 (Apple M2)",
                "llama_perf_context_print: load time = 125.50 ms",
                "llama_perf_context_print: prompt eval time = 40.00 ms / 8 tokens",
                "llama_perf_context_print: eval time = 500.00 ms / 20 tokens",
                "[ Prompt: 240.0 t/s | Generation: 40.0 t/s ]",
            )
        )
        return LlamaProcessResult(
            completed=subprocess.CompletedProcess(command_list, 0, stdout="", stderr=diagnostic),
            peak_process_tree_memory_bytes=987_654_321,
        )

    ticks = iter([4.0, 5.0])
    adapter = LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="test/model@1",
        runtime_version="b1234",
        license_id="Apache-2.0",
        device="metal",
        process_runner=measured_runner,
        monotonic=lambda: next(ticks),
    )

    result = adapter.generate(ScriptGenerationRequest(prompt="A prompt.", seed=91))

    assert result.telemetry.load_elapsed_seconds == pytest.approx(0.1255)
    assert result.telemetry.output_tokens == 20
    assert result.telemetry.output_token_elapsed_seconds == pytest.approx(0.5)
    assert result.telemetry.output_tokens_per_second == pytest.approx(40.0)
    assert result.telemetry.peak_process_tree_memory_bytes == 987_654_321
    assert result.telemetry.runtime_device == "Metal0 (Apple M2)"
    assert result.telemetry.live_process_tree_memory_after_bytes is None
    assert "exits" in (result.telemetry.live_process_tree_memory_after_unavailable_reason or "")


@dataclass
class _GeneratedAudio:
    samples: list[float]
    sample_rate: int = 24_000


class _FakeConfig:
    def __init__(self, **values: Any) -> None:
        self.values = values

    def validate(self) -> None:
        return None


class _FakeEngine:
    def __init__(self, _config: object, *, fail: bool = False, sample_rate: int = 24_000) -> None:
        self.fail = fail
        self.sample_rate = sample_rate
        self.calls: list[tuple[str, int, float]] = []

    def generate(self, text: str, *, sid: int, speed: float) -> _GeneratedAudio:
        self.calls.append((text, sid, speed))
        if self.fail:
            raise RuntimeError("temporary engine failure")
        return _GeneratedAudio(samples=[0.0, 0.2, -0.2, 0.1], sample_rate=self.sample_rate)


class _FakeSherpa:
    __version__ = "1.13.4"
    OfflineTtsKokoroModelConfig = _FakeConfig
    OfflineTtsModelConfig = _FakeConfig
    OfflineTtsConfig = _FakeConfig

    def __init__(self, *, fail: bool = False, sample_rate: int = 24_000) -> None:
        self.fail = fail
        self.sample_rate = sample_rate
        self.engines: list[_FakeEngine] = []

    def OfflineTts(self, config: object) -> _FakeEngine:
        engine = _FakeEngine(config, fail=self.fail, sample_rate=self.sample_rate)
        self.engines.append(engine)
        return engine


def _kokoro_directory(tmp_path: Path) -> Path:
    model_root = tmp_path / "kokoro"
    (model_root / "espeak-ng-data").mkdir(parents=True)
    for filename in ("model.onnx", "voices.bin", "tokens.txt", "lexicon-us-en.txt"):
        (model_root / filename).write_bytes(b"fixture")
    return tmp_path


def test_kokoro_adapter_is_lazy_and_passes_speech_contract(tmp_path: Path) -> None:
    fake_sherpa = _FakeSherpa()
    loads = 0

    def loader() -> _FakeSherpa:
        nonlocal loads
        loads += 1
        return fake_sherpa

    adapter = SherpaOnnxKokoroAdapter(
        model_directory=_kokoro_directory(tmp_path),
        model_version="v1_0",
        runtime_version="1.13.4",
        license_id="Apache-2.0",
        module_loader=loader,
    )

    assert loads == 0
    assert_speech_synthesizer_contract(adapter)
    assert loads == 1
    assert fake_sherpa.engines[0].calls == [("Welcome to this calm moment.", 3, 0.9)]


def test_kokoro_cache_identity_changes_with_voice_or_speed(tmp_path: Path) -> None:
    model_directory = _kokoro_directory(tmp_path)
    first = SherpaOnnxKokoroAdapter(
        model_directory=model_directory,
        model_version="v1_0",
        runtime_version="1.13.4",
        license_id="Apache-2.0",
        settings=SherpaOnnxSettings(voice_name="af_heart", speaker_id=3, speed=0.9),
    )
    second = SherpaOnnxKokoroAdapter(
        model_directory=model_directory,
        model_version="v1_0",
        runtime_version="1.13.4",
        license_id="Apache-2.0",
        settings=SherpaOnnxSettings(voice_name="af_bella", speaker_id=0, speed=1.0),
    )

    assert first.cache_identity != second.cache_identity


def test_kokoro_missing_python_runtime_is_fatal(tmp_path: Path) -> None:
    def missing_loader() -> Any:
        raise ImportError("not installed")

    adapter = SherpaOnnxKokoroAdapter(
        model_directory=_kokoro_directory(tmp_path),
        model_version="v1_0",
        runtime_version="1.13.4",
        license_id="Apache-2.0",
        module_loader=missing_loader,
    )

    with pytest.raises(FatalSynthesisError, match="not installed"):
        adapter.synthesize(SpeechSegment(id="speech-1", text="Welcome."))


def test_kokoro_generation_failure_is_transient(tmp_path: Path) -> None:
    adapter = SherpaOnnxKokoroAdapter(
        model_directory=_kokoro_directory(tmp_path),
        model_version="v1_0",
        runtime_version="1.13.4",
        license_id="Apache-2.0",
        module_loader=lambda: _FakeSherpa(fail=True),
    )

    with pytest.raises(TransientSynthesisError, match="temporary engine failure"):
        adapter.synthesize(SpeechSegment(id="speech-1", text="Welcome."))


def test_kokoro_wrong_sample_rate_is_invalid_output(tmp_path: Path) -> None:
    adapter = SherpaOnnxKokoroAdapter(
        model_directory=_kokoro_directory(tmp_path),
        model_version="v1_0",
        runtime_version="1.13.4",
        license_id="Apache-2.0",
        module_loader=lambda: _FakeSherpa(sample_rate=22_050),
    )

    with pytest.raises(InvalidSynthesisOutput, match="requires 24000"):
        adapter.synthesize(SpeechSegment(id="speech-1", text="Welcome."))


def test_fish_availability_names_missing_local_components(tmp_path: Path) -> None:
    error = FishSpeech14Adapter.availability_error(tmp_path)

    assert error is not None
    assert "python" in error
    assert "model.pth" in error
    assert "whoopy-reference.wav" in error


def test_moss_metadata_records_license_style_and_reference_hash(tmp_path: Path) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference-audio")
    settings = MossTTSSettings(
        runtime_directory=tmp_path / "runtime",
        worker_script=tmp_path / "worker.py",
        model_directory=tmp_path / "model",
        codec_directory=tmp_path / "codec",
        variant="moss-local-v1.5",
        reference_audio=reference,
        language="English",
        instruction="Speak gently.",
    )

    adapter = MossTTSAdapter(settings)
    changed = MossTTSAdapter(replace(settings, instruction="Speak brightly."))

    assert adapter.metadata.license_id == "Apache-2.0"
    assert "instruction=Speak gently." in adapter.metadata.settings
    assert f"duration_budget_strategy={MOSS_DURATION_BUDGET_STRATEGY}" in adapter.metadata.settings
    assert adapter.cache_identity != changed.cache_identity


def test_moss_translates_pronunciation_without_changing_spoken_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference-audio")
    adapter = MossTTSAdapter(
        MossTTSSettings(
            runtime_directory=tmp_path / "runtime",
            worker_script=tmp_path / "worker.py",
            model_directory=tmp_path / "model",
            codec_directory=tmp_path / "codec",
            variant="moss-local-v1.5",
            reference_audio=reference,
        )
    )
    requests: list[dict[str, object]] = []

    class Controller:
        def request(self, payload: dict[str, object]) -> dict[str, object]:
            requests.append(payload)
            return {"status": "ok", "pcm_s16le": "AAA="}

    monkeypatch.setattr(adapter, "_start", lambda: Controller())
    segment = SpeechSegment(
        id="speech-1",
        text="Welcome to Whoopy.",
        target_duration_ms=4_000,
        pronunciation_overrides=(PronunciationOverride(source="Whoopy", pronunciation="Woo-pee"),),
    )

    audio = adapter.synthesize(segment)

    assert audio.pcm_s16le == b"\x00\x00"
    assert requests[0]["text"] == "Welcome to Whoopy."
    instruction = requests[0]["instruction"]
    assert isinstance(instruction, str)
    assert '"source":"Whoopy"' in instruction
    assert '"pronunciation":"Woo-pee"' in instruction
    assert "Woo-pee" not in requests[0]["text"]
    assert requests[0]["duration_tokens"] == 50
    assert requests[0]["max_new_tokens"] == 127
    assert requests[0]["duration_budget_source"] == "explicit_target"
    assert requests[0]["duration_budget_strategy"] == MOSS_DURATION_BUDGET_STRATEGY


def test_moss_fallback_duration_budget_is_bounded_and_uses_typed_pace() -> None:
    text = "Notice the support beneath you."
    paces: tuple[PaceIntent, ...] = ("natural", "unhurried", "slow", "very_slow")
    budgets = [
        moss_duration_budget(SpeechSegment(id=f"speech-{index}", text=text, pace=pace))
        for index, pace in enumerate(paces, start=1)
    ]

    assert [budget.duration_tokens for budget in budgets] == [27, 31, 36, 43]
    assert all(budget.source == "text_pace_fallback" for budget in budgets)
    assert [budget.max_new_tokens for budget in budgets] == [98, 103, 109, 118]
    assert all(budget.max_new_tokens < 2_048 for budget in budgets)


def test_moss_duration_budget_explicit_target_wins_and_long_text_is_clamped() -> None:
    explicit = moss_duration_budget(
        SpeechSegment(
            id="speech-explicit",
            text="A deliberately long sentence that must not affect the explicit target.",
            pace="very_slow",
            target_duration_ms=4_000,
        )
    )
    long_fallback = moss_duration_budget(
        SpeechSegment(id="speech-long", text="measured " * 2_000, pace="very_slow")
    )

    assert explicit.duration_tokens == 50
    assert explicit.max_new_tokens == 127
    assert explicit.source == "explicit_target"
    assert long_fallback.duration_tokens == 1_500
    assert long_fallback.max_new_tokens == 1_536


def test_moss_availability_requires_runtime_checkpoint_and_reference(tmp_path: Path) -> None:
    error = MossTTSAdapter.availability_error(
        tmp_path / "runtime",
        tmp_path / "checkpoint",
        tmp_path / "codec",
        tmp_path / "reference.wav",
    )

    assert error == (
        "missing isolated Python runtime, model checkpoint, audio tokenizer, reference voice"
    )


def test_moss_direct_voice_does_not_require_or_hash_a_reference(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    python = runtime / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.touch()
    model = tmp_path / "model"
    codec = tmp_path / "codec"
    for directory in (model, codec):
        directory.mkdir()
        (directory / "config.json").write_text("{}", encoding="utf-8")
        (directory / "model.safetensors").write_bytes(b"checkpoint")

    assert (
        MossTTSAdapter.availability_error(
            runtime,
            model,
            codec,
            None,
            use_reference=False,
        )
        is None
    )
    settings = MossTTSSettings(
        runtime_directory=runtime,
        worker_script=tmp_path / "worker.py",
        model_directory=model,
        codec_directory=codec,
        variant="moss-local-v1.5",
        reference_audio=None,
        use_reference=False,
    )
    adapter = MossTTSAdapter(settings)

    assert "reference_sha256=none" in adapter.metadata.settings


def test_moss_availability_rejects_incomplete_sharded_checkpoint(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    (runtime / ".venv" / "bin").mkdir(parents=True)
    (runtime / ".venv" / "bin" / "python").touch()
    model = tmp_path / "model"
    model.mkdir()
    (model / "config.json").write_text("{}")
    (model / "model.safetensors.index.json").write_text(
        json.dumps(
            {
                "weight_map": {
                    "first": "model-00001-of-00002.safetensors",
                    "second": "model-00002-of-00002.safetensors",
                }
            }
        )
    )
    (model / "model-00001-of-00002.safetensors").write_bytes(b"partial")
    codec = tmp_path / "codec"
    codec.mkdir()
    (codec / "config.json").write_text("{}")
    (codec / "model.safetensors").write_bytes(b"complete")
    reference = tmp_path / "reference.wav"
    reference.write_bytes(b"reference")

    error = MossTTSAdapter.availability_error(runtime, model, codec, reference)

    assert error == "missing model checkpoint"
