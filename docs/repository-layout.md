# Repository Layout

This document is the placement guide for new code. It turns the conceptual architecture into directory ownership rules while keeping empty future areas honest about their status.

## Top-Level Boundaries

| Path | Owns | Must not own |
|---|---|---|
| `config/` | versioned defaults, registries, pacing data, prompts | secrets, machine-local values, model weights |
| `src/whoopy/` | local domain logic, ports, adapters, pipeline, QC, control plane | Commons-only publishing UI or generated artifacts |
| `assets/` | source assets with redistribution provenance | generated runs, untracked downloads |
| `db/` | persistence models and migrations | domain decisions tied directly to an ORM session |
| `web/` | local SvelteKit control and inspection UI | generation or audio rules |
| `commons/` | optional public distribution and moderation platform | dependencies required by local generation |
| `tests/` | unit, contract, golden, and integration checks | large model weights or generated runs |
| `docs/` | current explanations, decisions, and operational guides | secrets or undocumented generated output |

## Python Package Boundaries

`src/whoopy` uses a source layout so importing `whoopy` means the installed package is being tested. Future modules follow these dependency rules:

```text
timeline/domain <- pipeline -> ports <- adapters
                         |
                         +----> QC

API/CLI -> application services -> pipeline
```

- `timeline/` owns the canonical segment types and compilation rules.
- `audio/` owns the synthesis protocol, dependency-free fixture PCM, WAV assembly, manifests, and read-back checks.
- `ports/` owns typed behavior contracts and shared adapter errors.
- `adapters/` implements ports for concrete models and infrastructure.
- `pipeline/` coordinates domain objects and ports; it does not inspect model names.
- `qc/` evaluates artifacts and returns structured results.
- `api/` translates local HTTP/queue input into application calls; it does not run models in-process.
- `hardware.py` detects native resources and selects a safe user-facing runtime profile.
- `control.py` submits prompts and reads run state without performing worker work.
- `pipeline/runs.py` owns durable records, UUID-safe paths, and atomic artifact writes.
- `pipeline/cache.py` owns content-addressed reusable speech and its integrity metadata.
- `pipeline/checkpoints.py` owns per-run segment progress and verified PCM checkpoints.
- `pipeline/worker.py` owns lifecycle transitions, retry, resume, and processing behind the worker boundary.

## Replaceable Models

A model replacement has three controlled surfaces:

1. the model implements an existing typed port through an adapter;
2. its registry entry declares an immutable model identifier, license, runtime, and publication policy;
3. contract and quality tests establish whether it is a supported replacement.

Changing a registry key must not change timeline, pipeline, API, or UI code. If a backend requires special prompt syntax, delivery controls, sample-rate handling, or error recovery, that behavior belongs inside its adapter.

The default path resolves `auto` to llama.cpp/GGUF and sherpa-onnx/Kokoro. Optional MLX, CUDA-specific, or future runtimes implement the same ports and cannot become requirements of the domain package.

## Runtime Data

The current phases create these ignored runtime paths:

- `runs/` for per-generation records, segment checkpoints, and final artifacts;
- `runs/.cache/segments/` for content-addressed reusable speech;
- `models/` for optional repository-local weights;
- SQLite database and sidecar files.

Tests must use temporary directories. Generated media enters Git only when a later golden-fixture PR explicitly documents why the file is small, stable, and license-safe.

## Why Empty Areas Have READMEs

Git does not track empty directories. Phase 0 uses small README files instead of `.gitkeep` markers so contributors can see each directory's responsibility and deferrals while reviewing the tree.
