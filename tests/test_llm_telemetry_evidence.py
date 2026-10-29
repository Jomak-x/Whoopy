"""Telemetry is tested from new adapter outputs, never a prior generation receipt."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from whoopy.adapters.llm.llama_cpp import LlamaCppScriptGenerator, LlamaProcessResult
from whoopy.ports import ScriptGenerationRequest, ScriptGenerationResult


@pytest.mark.parametrize("with_diagnostics", [True, False])
def test_generated_telemetry_serializes_measurements_or_explicit_unavailability(
    tmp_path: Path, with_diagnostics: bool
) -> None:
    executable = tmp_path / "llama-cli"
    model = tmp_path / "model.gguf"
    executable.write_text("injected runner; never executed", encoding="utf-8")
    model.write_bytes(b"test model; never loaded")
    diagnostic = "\n".join(
        (
            "llama_model_load: using device Metal0 (Test GPU)",
            "llama_perf_context_print: load time = 125.50 ms",
            "llama_perf_context_print: eval time = 500.00 ms / 20 tokens",
            "[ Prompt: 240.0 t/s | Generation: 40.0 t/s ]",
        )
    )

    def runner(command: Sequence[str], _timeout: float) -> LlamaProcessResult:
        arguments = list(command)
        output = Path(arguments[arguments.index("--output-file") + 1])
        output.write_text("Rest your attention here.", encoding="utf-8")
        return LlamaProcessResult(
            completed=subprocess.CompletedProcess(
                arguments, 0, stdout="", stderr=diagnostic if with_diagnostics else ""
            ),
            peak_process_tree_memory_bytes=987_654_321 if with_diagnostics else None,
            memory_unavailable_reason=None
            if with_diagnostics
            else "test runner did not measure RSS",
        )

    ticks = iter([4.0, 5.0])
    adapter = LlamaCppScriptGenerator(
        executable_path=executable,
        model_path=model,
        versioned_model_id="fixture/model@1",
        runtime_version="fixture-runtime@1",
        license_id="Apache-2.0",
        device="cpu",
        process_runner=runner,
        monotonic=lambda: next(ticks),
    )
    generated = adapter.generate(ScriptGenerationRequest(prompt="A short meditation.", seed=91))
    receipt = tmp_path / "generation.json"
    receipt.write_text(generated.model_dump_json(), encoding="utf-8")
    restored = ScriptGenerationResult.model_validate_json(receipt.read_text(encoding="utf-8"))

    assert restored == generated
    assert restored.text == "Rest your attention here."
    assert restored.elapsed_seconds == 1
    assert restored.metadata.runtime_id == "llama.cpp"
    assert restored.metadata.runtime_version == "fixture-runtime@1"
    assert restored.metadata.license_id == "Apache-2.0"
    telemetry = restored.telemetry
    assert telemetry.live_process_tree_memory_after_bytes is None
    assert "exit" in (telemetry.live_process_tree_memory_after_unavailable_reason or "")
    if with_diagnostics:
        assert telemetry.load_elapsed_seconds == pytest.approx(0.1255)
        assert telemetry.output_tokens == 20
        assert telemetry.output_token_elapsed_seconds == 0.5
        assert telemetry.output_tokens_per_second == 40
        assert telemetry.peak_process_tree_memory_bytes == 987_654_321
        assert telemetry.runtime_device == "Metal0 (Test GPU)"
    else:
        for field, reason in (
            ("load_elapsed_seconds", "load_elapsed_unavailable_reason"),
            ("output_tokens", "output_tokens_unavailable_reason"),
            ("output_token_elapsed_seconds", "output_token_elapsed_unavailable_reason"),
            ("output_tokens_per_second", "output_tokens_per_second_unavailable_reason"),
            ("peak_process_tree_memory_bytes", "peak_process_tree_memory_unavailable_reason"),
            ("runtime_device", "runtime_device_unavailable_reason"),
        ):
            assert getattr(telemetry, field) is None
            assert getattr(telemetry, reason)
