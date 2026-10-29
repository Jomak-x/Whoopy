"""Subprocess-isolated local script generation through verified llama.cpp."""

from __future__ import annotations

import json
import math
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import psutil

from whoopy.artifacts import ArtifactLock, ArtifactSpec, ArtifactStore, TargetPlatform
from whoopy.ports import (
    AdapterMetadata,
    FatalAdapterError,
    InvalidAdapterOutput,
    ScriptGenerationRequest,
    ScriptGenerationResult,
    ScriptGenerationTelemetry,
    TransientAdapterError,
)


@dataclass(frozen=True)
class LlamaProcessResult:
    """Completed subprocess plus measurements unavailable from llama.cpp logs."""

    completed: subprocess.CompletedProcess[str]
    load_elapsed_seconds: float | None = None
    load_elapsed_unavailable_reason: str | None = None
    peak_process_tree_memory_bytes: int | None = None
    memory_unavailable_reason: str | None = None


ProcessRunner = Callable[
    [Sequence[str], float], subprocess.CompletedProcess[str] | LlamaProcessResult
]
MonotonicClock = Callable[[], float]


class CapturingProcessRunner:
    """Keep immutable evidence before the adapter removes its temporary inputs."""

    def __init__(self, root: Path, runner: ProcessRunner) -> None:
        self.root = root
        self.runner = runner
        self.invocations = 0

    def __call__(
        self, command: Sequence[str], timeout_seconds: float
    ) -> subprocess.CompletedProcess[str] | LlamaProcessResult:
        self.invocations += 1
        folder = self.root / f"{self.invocations:03d}"
        folder.mkdir(mode=0o700, parents=True, exist_ok=False)

        def save(name: str, value: str | bytes) -> None:
            with (folder / name).open("xb") as destination:
                destination.write(value.encode("utf-8") if isinstance(value, str) else value)

        save("command.json", json.dumps(list(command), indent=2) + "\n")
        receipt: dict[str, Any] = {"timeout_seconds": timeout_seconds, "returncode": None}
        try:
            for flag, name in (
                ("--file", "prompt.txt"),
                ("--system-prompt-file", "system.txt"),
                ("--json-schema-file", "response-schema.json"),
            ):
                if flag in command:
                    save(name, Path(command[command.index(flag) + 1]).read_bytes())
            result = self.runner(command, timeout_seconds)
            completed = result.completed if isinstance(result, LlamaProcessResult) else result
            save("stdout.txt", completed.stdout)
            save("stderr.txt", completed.stderr)
            receipt.update(status="completed", returncode=completed.returncode)
            return result
        except BaseException as error:
            receipt.update(status="failed", exception_type=type(error).__name__, error=str(error))
            for stream in ("stdout", "stderr"):
                value = getattr(error, stream, None)
                if isinstance(value, str | bytes):
                    save(f"{stream}.txt", value)
                else:
                    receipt[f"{stream}_unavailable_reason"] = (
                        "Process runner did not return this stream on failure."
                    )
            raise
        finally:
            if "--output-file" in command:
                output = Path(command[command.index("--output-file") + 1])
                if output.is_file():
                    save("output.txt", output.read_bytes())
                else:
                    receipt["output_unavailable_reason"] = "Process produced no output file."
            save("process.json", json.dumps(receipt, indent=2) + "\n")


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
) -> LlamaProcessResult:
    started = time.monotonic()
    process = subprocess.Popen(
        list(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=os.name == "posix",
        creationflags=(
            int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
            if os.name == "nt"
            else 0
        ),
        bufsize=1,
    )
    stop_sampling = threading.Event()
    peak_bytes: int | None = None
    memory_error: str | None = None
    load_elapsed_seconds: float | None = None
    output_chunks: list[str] = []
    error_chunks: list[str] = []

    def read_stream(stream: Any, chunks: list[str], *, watch_startup: bool) -> None:
        nonlocal load_elapsed_seconds
        for line in iter(stream.readline, ""):
            chunks.append(line)
            if (
                watch_startup
                and load_elapsed_seconds is None
                and ("available commands:" in line or line.lstrip().startswith("> "))
            ):
                load_elapsed_seconds = max(0.0, time.monotonic() - started)

    def sample_process_tree() -> None:
        nonlocal peak_bytes, memory_error
        try:
            root = psutil.Process(process.pid)
            while not stop_sampling.is_set():
                total = 0
                for current in (root, *root.children(recursive=True)):
                    try:
                        total += current.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.ZombieProcess):
                        continue
                if total > 0:
                    peak_bytes = max(peak_bytes or total, total)
                stop_sampling.wait(0.02)
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            # Normal subprocess exit can race the final sample. Any prior peak
            # remains a complete observation of the process while it was live.
            if peak_bytes is None:
                memory_error = "process exited before a memory sample completed"
        except (psutil.AccessDenied, OSError) as error:
            memory_error = f"{type(error).__name__}: {error}"[:500]
        except psutil.Error as error:
            memory_error = f"{type(error).__name__}: {error}"[:500]

    sampler = threading.Thread(
        target=sample_process_tree,
        name="whoopy-llama-memory-telemetry",
        daemon=True,
    )
    sampler.start()
    stdout_reader = threading.Thread(
        target=read_stream,
        args=(process.stdout, output_chunks),
        kwargs={"watch_startup": True},
        name="whoopy-llama-stdout-reader",
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=read_stream,
        args=(process.stderr, error_chunks),
        kwargs={"watch_startup": True},
        name="whoopy-llama-stderr-reader",
        daemon=True,
    )
    stdout_reader.start()
    stderr_reader.start()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as error:
        _stop_process_tree(process)
        raise subprocess.TimeoutExpired(
            command,
            timeout_seconds,
            output="".join(output_chunks),
            stderr="".join(error_chunks),
        ) from error
    except BaseException:
        _stop_process_tree(process)
        raise
    finally:
        stop_sampling.set()
        sampler.join(timeout=1)
        stdout_reader.join(timeout=1)
        stderr_reader.join(timeout=1)
    completed = subprocess.CompletedProcess(
        command, process.returncode, "".join(output_chunks), "".join(error_chunks)
    )
    return LlamaProcessResult(
        completed=completed,
        load_elapsed_seconds=load_elapsed_seconds,
        load_elapsed_unavailable_reason=(
            None
            if load_elapsed_seconds is not None
            else "llama.cpp emitted no observable model-ready signal"
        ),
        peak_process_tree_memory_bytes=(peak_bytes if memory_error is None else None),
        memory_unavailable_reason=(
            memory_error
            if memory_error is not None
            else (
                None
                if peak_bytes is not None
                else "process exited before a memory sample completed"
            )
        ),
    )


def _stop_process_tree(process: subprocess.Popen[str]) -> None:
    """Bound cleanup so cancellation cannot leave a local LLM child behind."""

    if os.name == "posix":
        try:
            cast(Any, os).killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    elif process.poll() is None:
        # Windows has no stdlib process-group kill primitive. ``taskkill /T``
        # is part of Windows and follows descendants before forcing shutdown.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                capture_output=True,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            with suppress(ProcessLookupError):
                cast(Any, os).killpg(
                    process.pid,
                    getattr(signal, "SIGKILL", signal.SIGTERM),
                )
        else:
            process.kill()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)


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
    transcript, marker, answer = normalized.rpartition("\nAssistant:")
    if (
        marker
        and (transcript.startswith("User:") or "\nUser:\n" in transcript)
        and (not answer or answer.startswith("\n"))
    ):
        return answer.strip()
    return normalized


_MILLISECONDS = re.compile(r"(?P<value>[0-9]+(?:\.[0-9]+)?)\s*ms\b", re.IGNORECASE)
_TOKEN_COUNT = re.compile(r"/\s*(?P<value>[0-9]+)\s+(?:tokens?|runs?)\b", re.IGNORECASE)
_DEVICE = re.compile(r"\b(?:using|found) device\s*:?\s*(?P<value>[^\r\n,]+)", re.IGNORECASE)
_GENERATION_SPEED = re.compile(
    r"\bGeneration:\s*(?P<value>[0-9]+(?:\.[0-9]+)?)\s*t/s\b", re.IGNORECASE
)


_SAMPLER_FAILURE = re.compile(
    r"^[^\r\n]*\b(?:common_sampler_init|srv[ \t]+send_error):[^\r\n]*\b"
    r"(?P<diagnostic>Failed to initialize samplers|error initializing grammar sampler)\b",
    re.IGNORECASE | re.MULTILINE,
)


def _llama_timing_evidence(diagnostic: str) -> dict[str, float | int | str | None]:
    """Parse stable llama.cpp perf labels while tolerating version-specific prefixes."""

    load_seconds: float | None = None
    output_tokens: int | None = None
    output_seconds: float | None = None
    output_tokens_per_second: float | None = None
    runtime_device: str | None = None
    for line in diagnostic.splitlines():
        lowered = line.lower()
        if "load time" in lowered:
            match = _MILLISECONDS.search(line)
            if match is not None:
                load_seconds = float(match.group("value")) / 1_000
        if "eval time" in lowered and "prompt eval time" not in lowered:
            duration = _MILLISECONDS.search(line)
            count = _TOKEN_COUNT.search(line)
            if duration is not None and count is not None:
                output_seconds = float(duration.group("value")) / 1_000
                output_tokens = int(count.group("value"))
        if runtime_device is None:
            match = _DEVICE.search(line)
            if match is not None:
                runtime_device = match.group("value").strip()
        speed = _GENERATION_SPEED.search(line)
        if speed is not None:
            rate = float(speed.group("value"))
            output_tokens_per_second = rate if math.isfinite(rate) and rate > 0 else None
    return {
        "load_elapsed_seconds": load_seconds,
        "output_tokens": output_tokens,
        "output_token_elapsed_seconds": output_seconds,
        "output_tokens_per_second": output_tokens_per_second,
        "runtime_device": runtime_device,
    }


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
                f"timeout_seconds={self.settings.timeout_seconds}",
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
        model_component = {
            "lite": "llm_model_lite",
            "standard": "llm_model_standard",
            "high": "llm_model_high",
        }[profile_name]
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
                "--simple-io",
                "--color",
                "off",
                "--offline",
                "--perf",
            ]
            if self.settings.threads:
                command.extend(["--threads", str(self.settings.threads)])
            if request.system_prompt is not None:
                system_path = temporary / "system.txt"
                system_path.write_text(request.system_prompt, encoding="utf-8")
                system_path.chmod(0o600)
                command.extend(["--system-prompt-file", str(system_path)])
            if request.response_json_schema is not None:
                schema_path = temporary / "response-schema.json"
                schema_path.write_text(
                    json.dumps(request.response_json_schema, allow_nan=False), encoding="utf-8"
                )
                schema_path.chmod(0o600)
                command.extend(["--json-schema-file", str(schema_path)])

            runner = self.process_runner
            if request.raw_invocation_directory is not None:
                runner = CapturingProcessRunner(request.raw_invocation_directory, runner)
            started = self.monotonic()
            try:
                process_result = runner(command, self.settings.timeout_seconds)
            except subprocess.TimeoutExpired as error:
                raise TransientAdapterError(
                    f"llama.cpp exceeded its {self.settings.timeout_seconds:g}-second timeout"
                ) from error
            except OSError as error:
                raise FatalAdapterError(f"Could not start llama.cpp: {error}") from error
            elapsed = max(0.0, self.monotonic() - started)

            if isinstance(process_result, LlamaProcessResult):
                completed = process_result.completed
                external_load_seconds = process_result.load_elapsed_seconds
                external_load_reason = process_result.load_elapsed_unavailable_reason
                peak_memory = process_result.peak_process_tree_memory_bytes
                memory_reason = process_result.memory_unavailable_reason
            else:
                completed = process_result
                external_load_seconds = None
                external_load_reason = "injected process runner did not report model-ready timing"
                peak_memory = None
                memory_reason = "injected process runner did not report process-tree memory"

            if completed.returncode != 0:
                raise FatalAdapterError(f"llama.cpp exited with code {completed.returncode}")
            sampler_failure = _SAMPLER_FAILURE.search(completed.stderr)
            if sampler_failure is not None:
                raise InvalidAdapterOutput(
                    "llama.cpp runtime sampler failure: " + sampler_failure.group("diagnostic")
                )
            try:
                text = _assistant_text(output_path.read_text(encoding="utf-8"))
            except OSError as error:
                raise InvalidAdapterOutput(
                    f"llama.cpp did not create a readable output file: {error}"
                ) from error
            if not text:
                raise InvalidAdapterOutput("llama.cpp returned empty text")
            timing = _llama_timing_evidence(f"{completed.stderr}\n{completed.stdout}")
            load_seconds = cast(float | None, timing["load_elapsed_seconds"])
            if load_seconds is None:
                load_seconds = external_load_seconds
            output_tokens = cast(int | None, timing["output_tokens"])
            output_seconds = cast(float | None, timing["output_token_elapsed_seconds"])
            output_tokens_per_second = cast(float | None, timing["output_tokens_per_second"])
            runtime_device = cast(str | None, timing["runtime_device"])
            if output_tokens is not None and output_tokens < 1:
                output_tokens = None
            if output_seconds is not None and output_seconds <= 0:
                output_seconds = None
            return ScriptGenerationResult(
                text=text,
                metadata=self.metadata,
                elapsed_seconds=elapsed,
                telemetry=ScriptGenerationTelemetry(
                    load_elapsed_seconds=load_seconds,
                    load_elapsed_unavailable_reason=(
                        None
                        if load_seconds is not None
                        else external_load_reason
                        or "llama.cpp did not emit a parseable load-time metric"
                    ),
                    output_tokens=output_tokens,
                    output_tokens_unavailable_reason=(
                        None
                        if output_tokens is not None
                        else "llama.cpp did not emit a parseable output-token count"
                    ),
                    output_token_elapsed_seconds=output_seconds,
                    output_token_elapsed_unavailable_reason=(
                        None
                        if output_seconds is not None
                        else "llama.cpp did not emit a parseable output-token duration"
                    ),
                    output_tokens_per_second=output_tokens_per_second,
                    output_tokens_per_second_unavailable_reason=(
                        None
                        if output_tokens_per_second is not None
                        else "llama.cpp did not emit positive finite generation throughput"
                    ),
                    peak_process_tree_memory_bytes=peak_memory,
                    peak_process_tree_memory_unavailable_reason=(
                        None if peak_memory is not None else memory_reason
                    ),
                    live_process_tree_memory_after_unavailable_reason=(
                        "llama.cpp exits after each invocation, so no live subprocess tree "
                        "remains after completion"
                    ),
                    runtime_device=runtime_device,
                    runtime_device_unavailable_reason=(
                        None
                        if runtime_device is not None
                        else "llama.cpp did not emit a parseable selected-device line"
                    ),
                ),
            )
