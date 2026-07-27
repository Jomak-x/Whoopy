# Architecture Overview

## Big Picture

Whoopy is built as a local generation engine plus an optional public distribution layer.

The local engine exists to solve the hard problem first: generate a deterministic meditation audio file natively on an ordinary Windows, macOS, or Linux laptop. The public platform comes later because it adds network, moderation, storage, and license complexity that should not block the core experience.

## Why The System Is Split This Way

The split is intentional:

- The local core keeps the first version usable without cloud dependencies.
- The public platform can evolve independently when the core audio pipeline is stable.
- The same canonical timeline contract can power both products, which avoids duplicate logic.

This is a common pattern in system design: keep the expensive, failure-prone, or privacy-sensitive path small and local, then build a broader networked product on top of the same data model once the core behavior is proven.

## Current Executable Slice

Phase 1 implements a small vertical slice of the future architecture:

```text
CLI -> LocalControlPlane -> run.json (queued)
                              |
CLI -> LocalWorker -----------+
        |
        +-> run.json (running)
        +-> timeline.json
        +-> run.json (completed or failed)
```

The control plane only accepts and records work. The worker alone processes it.
Both use an inspectable filesystem store today; future FastAPI and queue layers
can call the same boundaries. The timeline currently contains one
prompt-passthrough speech segment, not an AI-generated script.

## Core Components

```mermaid
flowchart LR
  A[Prompt] --> B[SvelteKit UI]
  B --> C[FastAPI control plane]
  C --> D[Huey job queue + SQLite]
  D --> E[Native generation worker]
  E --> P[Hardware profile and safe runtime]
  P --> F[Canonical timeline JSON]
  F --> G[TTS per segment]
  G --> H[Audio assembly]
  H --> I[Mix and master]
  I --> J[FLAC / AAC / Opus output]
  J --> K[Optional publish to Commons]
```

### UI

The UI is the human control surface. Its job is to submit prompts, show run status, surface logs, and let a user inspect or restart a generation.

Why it exists:

- Users need visibility into long-running generation jobs.
- A PWA can run close to the system while still feeling lightweight.
- The UI is not responsible for generation logic, which keeps it simpler.

### FastAPI Control Plane

The API is the orchestration layer. It creates jobs, tracks run state, and exposes the pieces the UI needs.

Why it exists:

- It gives the UI a stable contract.
- It keeps business rules out of the frontend.
- It makes the worker replaceable without rewriting the user-facing app.

### Queue And Database

The spec chooses a lightweight local queue and SQLite for the first phase.

Why it exists:

- It is easy to run on one machine.
- It keeps setup simple.
- It captures enough job state to resume, inspect, and debug runs.

### Native Generation Worker And Runtime Selection

The worker is where model work happens. It runs natively so it can use the best local CPU or accelerator without requiring Docker. Before model management acts, hardware inspection selects the highest safe Basic, Lite, Standard, High, or Studio profile.

Why it exists:

- llama.cpp/GGUF provides the universal local text path across CPU, Metal, CUDA, Vulkan, and other supported backends.
- sherpa-onnx/Kokoro provides the universal local speech path.
- Optional platform accelerators remain adapters rather than product requirements.
- Basic mode keeps the product useful without a local LLM.
- Unsafe machines are refused before a model download or load.

### Canonical Timeline

The canonical timeline is the most important data structure in the system. It describes the meditation as a sequence of segments such as speech, silence, breath, and music cues.

Why it exists:

- It makes pauses explicit.
- It gives the renderer a deterministic input.
- It allows partial regeneration without losing the full structure.
- It gives the public platform a stable artifact to share.

### Audio Pipeline

The audio pipeline is intentionally staged:

1. Plan the content.
2. Write the script in small parts.
3. Compile prose and cues into a canonical timeline.
4. Synthesize audio per segment.
5. Trim and normalize the segments.
6. Assemble them with exact pauses.
7. Add ambience, mix, master, and encode.

Why it exists:

- Small units are easier to debug than a single large generation.
- Deterministic silence prevents TTS timing drift from corrupting pacing.
- Per-segment synthesis allows caching and targeted fixes.

## The Most Important Design Decisions

### 1. Native worker on every supported operating system

The model worker is not put inside Docker. Windows, macOS, and Linux receive native processes built for their platform.

Reason:

- Native execution allows llama.cpp and ONNX Runtime to use CPU, Metal, CUDA, Vulkan, and other available providers directly.
- A single logical adapter path prevents users from choosing backends manually.
- Container availability does not decide whether the local product works.

### 2. Hardware profiles before model selection

The system selects capabilities before it selects a precise model artifact.

Reason:

- The same large model cannot run safely on every laptop.
- Live available memory matters, not only installed memory.
- Basic mode can still provide templates, pasted scripts, local TTS, and deterministic rendering.
- Refusing early is safer than downloading a model and discovering an out-of-memory failure later.

### 3. Timeline first, audio second

The system treats the timeline as the source of truth.

Reason:

- Prose alone is ambiguous.
- Audio files alone are hard to reason about.
- A timeline makes the structure inspectable, resumable, and testable.

### 4. Exact silence segments

Deliberate pauses are represented as explicit silence segments instead of being left to TTS timing.

Reason:

- Meditation quality depends on pause precision.
- TTS is not reliable enough to guess exact timing.
- Explicit silence makes the output reproducible.

### 5. Ports and adapters

LLM, TTS, ambience, renderer, publisher, and moderation are all behind typed interfaces.

Reason:

- The model choice can change without forcing a rewrite.
- Licensing and backend metadata can travel with each adapter.
- Testing becomes easier because each port can be mocked.

### 6. License-aware public sharing

Anything that may enter the public platform has to be checked for redistribution rights.

Reason:

- The local product can be permissive.
- The public platform has stronger legal and licensing constraints.
- Separating them prevents accidental license contamination.

## How The Pieces Interact

1. The user submits a prompt in the UI.
2. The control plane creates a job and stores the initial state.
3. The queue hands the job to the native worker.
4. The worker asks the planner model for a structure.
5. The script generator writes the meditation in small sections.
6. The compiler converts prose plus cues into the canonical timeline.
7. The TTS backend generates each speech segment separately.
8. The renderer inserts exact pauses, assembles the bed, mixes, and masters the final audio.
9. The run artifacts are saved locally for inspection and replay.
10. If the result is meant for sharing, a publish step creates the public manifest and uploads the encoded assets.

## Failure Domain Thinking

The architecture tries to keep failures small:

- A bad segment should only require regenerating that segment.
- A model mismatch should be isolated behind a single adapter.
- A rendering failure should not invalidate the script.
- A public platform problem should not stop local generation.

That is a classic resilience pattern: isolate the parts that fail differently.

## What To Challenge If You Disagree

When you question a design choice, ask these questions:

- What problem does this decision solve?
- What breaks if we remove it?
- What gets simpler if we replace it?
- Does the new approach still preserve deterministic pauses and resumability?
- Does it still work natively on supported Windows, macOS, and Linux laptops without cloud dependencies?
- Does Basic mode remain useful when a local LLM is unsafe?

If a proposed change weakens those answers, the burden of proof is on the change.
