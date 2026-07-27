"""Subprocess-isolated local script generation through verified llama.cpp."""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from whoopy.artifacts import ArtifactLock, ArtifactSpec, ArtifactStore, TargetPlatform
from whoopy.ports import (
    AdapterMetadata,
    FatalAdapterError,
    InvalidAdapterOutput,
    ScriptGenerationRequest,
    ScriptGenerationResult,
    TransientAdapterError,
)

ProcessRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True)
class LlamaCppSettings:
    """Bounded settings shared by every llama.cpp invocation."""

    context_tokens: int = 8_192
    temperature: float = 0.7
    top_p: float = 0.9
    threads: int = 0
    timeout_seconds: float = 300.0
    gpu_layers: str = "auto"
    reasoning: str = "off"

    def __post_init__(self) -> None:
        if self.context_tokens < 512:
            raise ValueError("llama.cpp context must contain at least 512 tokens")
        if not 0 <= self.temperature <= 2:
            raise ValueError("llama.cpp temperature must be between 0 and 2")
        if not 0 < self.top_p <= 1:
            raise ValueError("llama.cpp top_p must be greater than 0 and at most 1")
        if self.threads < 0:
            raise ValueError("llama.cpp threads cannot be negative")
        if self.timeout_seconds <= 0:
            raise ValueError("llama.cpp timeout must be positive")
        if self.reasoning not in {"on", "off", "auto"}:
            raise ValueError("llama.cpp reasoning must be on, off, or auto")


def _run_process(
    command: Sequence[str],
    timeout_seconds: float,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )


def _one_file(root: Path, names: set[str], description: str) -> Path:
    matches = sorted(path for path in root.rglob("*") if path.is_file() and path.name in names)
    if len(matches) != 1:
        raise FatalAdapterError(f"Expected one {description} under {root}, found {len(matches)}.")
    return matches[0]


def _component(artifacts: Sequence[ArtifactSpec], component: str) -> ArtifactSpec:
    matches = [artifact for artifact in artifacts if artifact.component == component]
    if len(matches) != 1:
        raise FatalAdapterError(
            f"Expected one resolved {component} artifact, found {len(matches)}."
        )
    return matches[0]


def _assistant_text(output: str) -> str:
    """Remove llama-cli's single-turn transcript wrapper when it is present."""

    normalized = output.strip()
    marker = "\nAssistant:\n"
    if marker in normalized and (normalized.startswith("User:") or "\nUser:\n" in normalized):
        return normalized.rpartition(marker)[2].strip()
    return normalized


class LlamaCppScriptGenerator:
    """Run one local generation without linking llama.cpp into the worker process."""

    def __init__(
        self,
        *,
        executable_path: Path,
        model_path: Path,
        versioned_model_id: str,
        runtime_version: str,
        license_id: str,
        device: str,
        settings: LlamaCppSettings | None = None,
        process_runner: ProcessRunner = _run_process,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        if not executable_path.is_file():
            raise FatalAdapterError(f"llama.cpp executable not found: {executable_path}")
        if not model_path.is_file():
            raise FatalAdapterError(f"GGUF model not found: {model_path}")
        self.executable_path = executable_path
        self.model_path = model_path
        self.settings = settings or LlamaCppSettings()
        self.process_runner = process_runner
        self.monotonic = monotonic
        self.metadata = AdapterMetadata(
            adapter_id="whoopy.llama_cpp",
            versioned_model_id=versioned_model_id,
            runtime_id="llama.cpp",
            runtime_version=runtime_version,
            license_id=license_id,
            device=device,
            settings=(
                f"context_tokens={self.settings.context_tokens}",
                f"temperature={self.settings.temperature}",
                f"top_p={self.settings.top_p}",
                f"threads={self.settings.threads}",
                f"gpu_layers={self.settings.gpu_layers}",
                f"reasoning={self.settings.reasoning}",
            ),
        )

    @classmethod
    def from_artifact_store(
        cls,
        *,
        artifact_lock: ArtifactLock,
        store: ArtifactStore,
        profile_name: str,
        target: TargetPlatform,
        device: str,
        settings: LlamaCppSettings | None = None,
    ) -> LlamaCppScriptGenerator:
        """Resolve and fully verify locked runtime/model bytes before use."""

        artifacts = artifact_lock.resolve(profile_name, target)
        runtime = _component(artifacts, "llm_runtime")
        model_component = "llm_model_lite" if profile_name == "lite" else "llm_model_standard"
        model = _component(artifacts, model_component)
        runtime_root = store.require(runtime)
        model_path = store.require(model)
        executable = _one_file(
            runtime_root,
            {"llama-cli", "llama-cli.exe"},
            "llama.cpp CLI executable",
        )
        return cls(
            executable_path=executable,
            model_path=model_path,
            versioned_model_id=f"{model.display_name}@{model.version}#{model.filename}",
            runtime_version=runtime.version,
            license_id=model.license_id,
            device=device,
            settings=settings,
        )

    def generate(self, request: ScriptGenerationRequest) -> ScriptGenerationResult:
        """Generate one response through temporary private input/output files."""

        with tempfile.TemporaryDirectory(prefix="whoopy-llama-") as temporary_name:
            temporary = Path(temporary_name)
            prompt_path = temporary / "prompt.txt"
            output_path = temporary / "output.txt"
            prompt_path.write_text(request.prompt, encoding="utf-8")
            prompt_path.chmod(0o600)

            command = [
                str(self.executable_path),
                "--model",
                str(self.model_path),
                "--file",
                str(prompt_path),
                "--output-file",
                str(output_path),
                "--ctx-size",
                str(self.settings.context_tokens),
                "--n-predict",
                str(request.max_output_tokens),
                "--temperature",
                str(self.settings.temperature),
                "--top-p",
                str(self.settings.top_p),
                "--seed",
                str(request.seed),
                "--gpu-layers",
                self.settings.gpu_layers,
                "--reasoning",
                self.settings.reasoning,
                "--conversation",
                "--single-turn",
                "--no-display-prompt",
                "--no-show-timings",
                "--simple-io",
                "--color",
                "off",
                "--offline",
            ]
            if self.settings.threads:
                command.extend(["--threads", str(self.settings.threads)])
            if request.system_prompt is not None:
                system_path = temporary / "system.txt"
                system_path.write_text(request.system_prompt, encoding="utf-8")
                system_path.chmod(0o600)
                command.extend(["--system-prompt-file", str(system_path)])

            started = self.monotonic()
            try:
                completed = self.process_runner(command, self.settings.timeout_seconds)
            except subprocess.TimeoutExpired as error:
                raise TransientAdapterError(
                    f"llama.cpp exceeded its {self.settings.timeout_seconds:g}-second timeout"
                ) from error
            except OSError as error:
                raise FatalAdapterError(f"Could not start llama.cpp: {error}") from error
            elapsed = max(0.0, self.monotonic() - started)

            if completed.returncode != 0:
                diagnostic = completed.stderr.strip()[-2_000:] or "no diagnostic output"
                raise FatalAdapterError(
                    f"llama.cpp exited with code {completed.returncode}: {diagnostic}"
                )
            try:
                text = _assistant_text(output_path.read_text(encoding="utf-8"))
            except OSError as error:
                raise InvalidAdapterOutput(
                    f"llama.cpp did not create a readable output file: {error}"
                ) from error
            if not text:
                raise InvalidAdapterOutput("llama.cpp returned empty text")
            return ScriptGenerationResult(
                text=text,
                metadata=self.metadata,
                elapsed_seconds=elapsed,
            )
