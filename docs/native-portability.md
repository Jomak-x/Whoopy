# Native Portability Contract

Serenity aims for one user experience across ordinary Windows, macOS, and Linux laptops without requiring Docker or asking users to understand inference backends. The promise is not that every machine can run the same large model. The promise is that Serenity detects what is safe, selects an appropriate local mode, and refuses unsafe model loads before downloading gigabytes of data.

## One Logical Runtime Path

The default model configuration is `auto`:

```text
Serenity pipeline
    |
    +-- capability inspection
    |
    +-- runtime profile
            |
            +-- llama.cpp + GGUF for local text generation
            +-- sherpa-onnx + Kokoro for local speech
            +-- FFmpeg for deterministic rendering
```

`llama.cpp` is the default text runtime because its official project supports CPU execution plus Metal, CUDA, HIP, Vulkan, and SYCL backends across broad hardware. `sherpa-onnx` is the default speech runtime because its official project supports TTS on Windows, macOS, and Linux across x64 and ARM64. MLX remains an optional Apple Silicon optimization behind the same ports; it is not a requirement for using Serenity.

Primary technical references:

- [llama.cpp supported hardware and quantization](https://github.com/ggml-org/llama.cpp)
- [llama.cpp device and memory-fit benchmarking](https://github.com/ggml-org/llama.cpp/blob/master/tools/llama-bench/README.md)
- [sherpa-onnx supported platforms and TTS](https://github.com/k2-fsa/sherpa-onnx)
- [ONNX Runtime execution providers](https://onnxruntime.ai/docs/execution-providers/)
- [uv cross-platform Python management](https://docs.astral.sh/uv/)

## Profiles Instead Of Backend Settings

Profiles in `config/runtime_profiles.yaml` describe conservative resource floors and capabilities:

| Profile | Initial floor | Local modes |
|---|---:|---|
| Basic | 4 GB total / 1.5 GB available RAM | templates, pasted scripts, local TTS |
| Lite | 8 GB total / 4 GB available RAM | Basic plus a 1–2B local GGUF LLM |
| Standard | 16 GB total / 8 GB available RAM | a 3–8B local GGUF LLM |
| High | 24 GB total / 14 GB available RAM | an 8–14B local GGUF LLM |
| Studio | 48 GB total / 28 GB available RAM | a 30B-class local GGUF LLM |

These are safety defaults, not performance claims. Real adapter PRs must add short `llama-bench` and TTS real-time-factor measurements before binding a precise model to a profile. Benchmark evidence can change thresholds without adding platform branches to domain code.

## Weak-Laptop Behavior

Basic mode makes the LLM optional. A weak but otherwise supported laptop can:

- select an authored meditation template;
- paste or edit a script;
- compile the canonical timeline;
- synthesize speech locally;
- render exact pauses and final audio.

If the Basic floor is not met, `serenity doctor` returns a non-zero status and explains which resource is insufficient. No model is loaded. Later download management must run this check before selecting or fetching an artifact.

Remote script generation may be added as an explicit opt-in, but `hardware.allow_remote_fallback` defaults to `false`. Local audio generation must not silently upload prompts or scripts.

## What Phase 0 Detects

`serenity doctor` currently reports:

- operating system and architecture;
- logical CPU count;
- total and currently available RAM;
- free disk space;
- CPU availability;
- Apple Metal capability;
- NVIDIA CUDA presence when `nvidia-smi` works;
- the highest profile meeting every live-resource margin.

`serenity doctor --profile lite` checks a specific tier without making it the global default. Normal users keep `hardware.profile: auto`.

Runtime PRs will extend the check with llama.cpp device enumeration, model memory fitting, measured tokens per second, TTS real-time factor, FFmpeg validation, and signed artifact checksums.

## Distribution Contract

Developers use one locked `uv` workflow across Windows, macOS, and Linux. End users should eventually receive native artifacts built on each target OS:

- a signed macOS application or installer;
- a signed Windows installer;
- a Linux AppImage or equivalent package.

Those artifacts will bundle the application runtime and the correct platform binaries. Models remain separate, verified downloads selected only after the compatibility check. The user should never need Python, CMake, CUDA configuration, or a model-format decision for the normal path.

## Test Contract

Every PR runs the same Python checks on Windows, macOS, and Linux. Real runtime releases additionally require:

- CPU smoke tests on all three operating systems;
- Apple Silicon Metal tests;
- NVIDIA CUDA tests;
- profile boundary and refusal tests;
- clean-machine installer tests;
- an offline launch test after required artifacts are downloaded.
