# Architecture Overview

## Big Picture

Serenity is built as a local generation engine plus an optional public distribution layer.

The local engine exists to solve the hard problem first: generate a high-quality, deterministic meditation audio file on one Mac. The public platform comes later because it adds network, moderation, storage, and license complexity that should not block the core experience.

## Why The System Is Split This Way

The split is intentional:

- The local core keeps the first version usable without cloud dependencies.
- The public platform can evolve independently when the core audio pipeline is stable.
- The same canonical timeline contract can power both products, which avoids duplicate logic.

This is a common pattern in system design: keep the expensive, failure-prone, or privacy-sensitive path small and local, then build a broader networked product on top of the same data model once the core behavior is proven.

## Core Components

```mermaid
flowchart LR
  A[Prompt] --> B[SvelteKit UI]
  B --> C[FastAPI control plane]
  C --> D[Huey job queue + SQLite]
  D --> E[Native generation worker]
  E --> F[Canonical timeline JSON]
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

### Native Generation Worker

The worker is where the actual model work happens. It runs natively on Apple Silicon so it can access the local ML stack that Docker cannot reach on macOS.

Why it exists:

- The spec treats Metal and MLX access as a hard constraint.
- A native worker avoids forcing the core pipeline into a container that cannot use the GPU path.
- This separation keeps Docker useful for stateless services without blocking model execution.

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

### 1. Native worker on macOS

The core ML worker is not put inside Docker on macOS.

Reason:

- Docker on macOS does not provide the same access to Metal/MLX GPU execution.
- If the worker cannot access the local acceleration path, the whole point of the local-first design is weakened.

### 2. Timeline first, audio second

The system treats the timeline as the source of truth.

Reason:

- Prose alone is ambiguous.
- Audio files alone are hard to reason about.
- A timeline makes the structure inspectable, resumable, and testable.

### 3. Exact silence segments

Deliberate pauses are represented as explicit silence segments instead of being left to TTS timing.

Reason:

- Meditation quality depends on pause precision.
- TTS is not reliable enough to guess exact timing.
- Explicit silence makes the output reproducible.

### 4. Ports and adapters

LLM, TTS, ambience, renderer, publisher, and moderation are all behind typed interfaces.

Reason:

- The model choice can change without forcing a rewrite.
- Licensing and backend metadata can travel with each adapter.
- Testing becomes easier because each port can be mocked.

### 5. License-aware public sharing

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
- Does it still work on one Apple Silicon Mac without cloud dependencies?

If a proposed change weakens those answers, the burden of proof is on the change.
