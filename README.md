# Whoopy

Whoopy is a local-first, timeline-driven system for generating guided meditation audio. It is named after the creator's first cat, Whoopy. The project will first prove a deterministic native CLI across ordinary Windows, macOS, and Linux laptops, then add a local web application, and only later add the optional public Whoopy Commons.

> [!IMPORTANT]
> The model worker runs **natively, without Docker**. Whoopy uses one automatic runtime path across Windows, macOS, and Linux: llama.cpp/GGUF for local text generation and sherpa-onnx/Kokoro for speech. Metal, CUDA, Vulkan, and CPU execution are runtime details selected for the user.

## Current Status

The current source of truth is
[`docs/local-first-master-plan.md`](./docs/local-first-master-plan.md). It
separates what is merged, implemented locally, machine-verified, and actually
accepted by listening. The project is now deliberately staying local until
crash recovery, managed voice comparisons, meditation quality, pacing, and
reviewed breathing exercises pass one explicit exit gate.

Phases 0–3 established the portable repository, durable worker boundary,
deterministic WAV assembly, caching, retry, and recovery. Phase 3.5 now adds
verified native artifacts and replaceable llama.cpp and sherpa/Kokoro adapters.
Its one `whoopy generate` command now produces real human speech from either a
local prompt or an authored script. Prompt mode preserves the validated plan,
raw attempts, section checkpoints, script, timeline, both model identities,
audio checkpoints, final WAV, manifest, and quality report. The first recorded
bake-off keeps Standard Qwen3-4B for prompt mode, rejects
the current Lite model as a dependable fallback after a 0/6 strict result, and
keeps Basic authored-script mode as the lower-resource path. Human voice review
remains deliberately pending. Phase 3.6 is still an open PR containing slower
sentence-level timing, broader adaptive pacing, Fish/MOSS voice work, and
content changes. None of that work should be described as merged or
human-accepted yet.

The functional commands are:

```bash
whoopy --help
whoopy web --open
whoopy config show
whoopy doctor
whoopy models list
whoopy models doctor
whoopy models install --profile auto
whoopy draft "A three-minute grounding meditation." --minutes 3
whoopy generate "A three-minute grounding meditation." --minutes 3
whoopy generate --script-file examples/first-meditation.md
whoopy evaluate --output-dir evaluations/local/my-bakeoff
whoopy run create "A short grounding meditation."
whoopy run show <run-id>
whoopy run reconcile [<run-id>]
whoopy run resume <run-id>
whoopy run cancel <run-id>
whoopy run regenerate-segment <run-id> <segment-id>
whoopy worker process <run-id>
whoopy cache stats
```

## Design In One Minute

Whoopy turns a prompt into a canonical timeline before it creates audio:

```text
prompt -> plan -> script -> canonical timeline -> segment audio -> mix/master -> outputs
```

That timeline is the source of truth. Deliberate pauses become exact `SILENCE` events rather than timing guesses left to a TTS model. LLM, TTS, ambience, rendering, and publishing capabilities sit behind typed ports so a supported model can be replaced through an adapter and configuration instead of a pipeline rewrite.

Model entries in [`config/models.yaml`](./config/models.yaml) are provisional starting points. The default `auto` path selects a safe profile from [`config/runtime_profiles.yaml`](./config/runtime_profiles.yaml). A registry entry records its adapter, model identifier, license, runtime, supported platforms, and publication policy. Real adapters and their contract tests arrive in later PRs.

Weak laptops do not need a local LLM. Basic mode supports authored templates or pasted scripts plus local TTS. If even Basic is unsafe, Whoopy refuses before downloading or loading a model and explains which resource is insufficient.

## Quick Start

### Requirements

- Windows, macOS, or Linux on x64 or ARM64
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/), which installs the required Python automatically
- Git
- FFmpeg for later audio phases
- Node 20 or newer for the later SvelteKit UI

### Install And Verify

```bash
uv sync --extra dev --locked
uv run whoopy --help
uv run whoopy config show
uv run whoopy doctor
uv run --extra dev python scripts/check.py
```

`uv` reads `.python-version`, installs Python 3.11 when needed, and reproduces `uv.lock`. The setup installs only lightweight foundation dependencies; it does not download model weights or install an ML runtime. Unix contributors may use the equivalent `make setup` and `make check` wrappers.

Model installation is a separate, explicit operation. Inspect the exact plan
without loading anything, then install only if desired:

```bash
uv run whoopy models doctor
uv run whoopy models install --profile auto
```

Every file is pinned by version, size, SHA-256 digest, license, operating
system, and architecture in [`config/artifacts.yaml`](./config/artifacts.yaml).

### Try The Current Flow

The easiest way to try both real flows is the private local tester:

```bash
uv run --offline whoopy web --open
```

It starts at `http://127.0.0.1:8765`, calls the same real CLI pipeline, shows
laptop/model readiness, accepts either a prompt or authored script, lists saved
runs, plays completed WAV files, and exposes their plan, script, timeline, and
quality report. It has no cloud backend and is intentionally a small bridge to
the full Phase 4 product. Read
[`docs/phase-3-5-overall-guide.md`](./docs/phase-3-5-overall-guide.md) for the
from-zero explanation of every Phase 3.5 PR and the tester.

Render the included script with real local speech after installing the Basic
artifacts:

```bash
uv run whoopy models install --profile basic
uv run --offline whoopy generate \
  --script-file examples/first-meditation.md
```

Open the printed `narration.wav`. The run also preserves the source script,
resolved settings, model metadata, canonical timeline, segment checkpoints,
audio manifest, and quality report. Read
[`docs/phase-3-5-real-script-speech.md`](./docs/phase-3-5-real-script-speech.md)
for the beginner-level explanation.

Or use the complete offline prompt flow with the Standard stack:

```bash
uv run whoopy models install --profile standard
uv run --offline whoopy generate \
  "A gentle three-minute grounding meditation." \
  --minutes 3
```

Read [`docs/phase-3-5-end-to-end.md`](./docs/phase-3-5-end-to-end.md) for the
full artifact map, cancellation/recovery paths, and measured real-model result.

The lower-level fixture flow remains available for development:

```bash
uv run whoopy run create "A short grounding meditation."
# Copy the printed UUID into the next command.
uv run whoopy worker process <run-id>
uv run whoopy run show <run-id>
```

This creates `runs/<run-id>/run.json`, `timeline.json`, `narration.wav`,
`audio-manifest.json`, and `quality.json`. See
[`docs/phase-2-deterministic-audio.md`](./docs/phase-2-deterministic-audio.md)
for a beginner-level explanation of the audio format, exact pause calculation,
assembly, and quality gate.

Phase 3 also creates verified speech checkpoints beneath
`runs/<run-id>/segments/` and a shared cache beneath `runs/.cache/segments/`.
Repeat the same prompt to observe cache hits in `run.json`, inspect them with
`whoopy cache stats`, or recover a failed/interrupted run with
`whoopy run resume <run-id>`. See
[`docs/phase-3-quality-caching-recovery.md`](./docs/phase-3-quality-caching-recovery.md).
PR 13 adds durable stages, two-second heartbeats, 15-second leases, exclusive
run locks, bounded logs, cancellation, stale-run reconciliation, and one-segment
regeneration. See
[`docs/phase-4-pr13-durable-recovery.md`](./docs/phase-4-pr13-durable-recovery.md).

## Configuration

Configuration precedence is:

```text
config/default.yaml < config/local.yaml < WHOOPY_* environment < CLI flags
```

Use `config/local.yaml` for machine-local non-secret overrides and process environment variables for secrets. `.env` is ignored by Git but is not automatically loaded in Phase 0; source it through your shell or process manager. Nested environment variables use two underscores:

```bash
WHOOPY_TTS__VOICE=test_voice uv run whoopy config show
uv run whoopy config show --tts-voice cli_voice
```

See [`config/README.md`](./config/README.md) for the contract.

## Repository Map

```text
config/                versioned settings, model registry, pacing, prompts
scripts/check.py        platform-neutral lint, format, type, and test gate
src/whoopy/            Python domain package and future local control plane
  artifacts.py         verified, resumable, platform-aware artifact installer
  audio/               speech processing, synthesis, WAV assembly, and quality checks
  control.py           prompt submission and run inspection service
  hardware.py          native capability inspection and safe profile selection
  ports/               typed capability contracts
  adapters/            model and infrastructure integrations
  timeline/            canonical models and text/Markdown script compiler
  pipeline/            run storage, segment cache/checkpoints, retry, and recovery
  qc/                  audio and content quality gates
  api/                 future expanded local control plane
  webui/               dependency-free local tester and HTTP boundary
assets/                 redistributable, provenance-tracked source assets
db/                     future SQLAlchemy models and migrations
web/                    future local SvelteKit PWA
commons/                future optional public platform
tests/                  unit, contract, golden, and integration tests
docs/                   architecture, setup, roadmap, and contribution docs
uv.lock                 exact cross-platform dependency resolution
```

The `src/` layout prevents tests from accidentally importing an uninstalled working copy. See [`docs/repository-layout.md`](./docs/repository-layout.md) for component boundaries and placement rules.

## Start Here

Read in this order:

1. [`docs/local-first-master-plan.md`](./docs/local-first-master-plan.md) — current state, model inventory, quality contract, and authoritative PR-by-PR path to Local V1
2. [`docs/architecture.md`](./docs/architecture.md) — concise system explanation
3. [`system-design.md`](./system-design.md) — canonical design specification
4. [`docs/repository-layout.md`](./docs/repository-layout.md) — where code belongs
5. [`docs/setup.md`](./docs/setup.md) — machine and build sequence
6. [`docs/roadmap.md`](./docs/roadmap.md) — implementation phases
7. [`docs/native-portability.md`](./docs/native-portability.md) — automatic cross-platform runtime and weak-laptop behavior
8. [`docs/phase-1-local-core.md`](./docs/phase-1-local-core.md) — the first executable flow
9. [`docs/phase-2-deterministic-audio.md`](./docs/phase-2-deterministic-audio.md) — exact audio assembly
10. [`docs/phase-3-quality-caching-recovery.md`](./docs/phase-3-quality-caching-recovery.md) — cache, retry, and resume
11. [`docs/phase-3-5-overall-guide.md`](./docs/phase-3-5-overall-guide.md) — start here for the from-zero explanation and local tester
12. [`docs/phase-3-5-first-local-meditation.md`](./docs/phase-3-5-first-local-meditation.md) — real local models and the first offline meditation
13. [`docs/phase-3-5-runtime-adapters.md`](./docs/phase-3-5-runtime-adapters.md) — ports, metadata, error taxonomy, and native adapters
14. [`docs/phase-3-5-real-script-speech.md`](./docs/phase-3-5-real-script-speech.md) — first real speech, script syntax, processing, and recovery
15. [`docs/phase-3-5-local-generation.md`](./docs/phase-3-5-local-generation.md) — plan-first generation, validation, safety, and draft resume
16. [`docs/phase-3-5-end-to-end.md`](./docs/phase-3-5-end-to-end.md) — one-command prompt/script audio flow and recovery
17. [`docs/evaluations/phase-3-5-model-bakeoff-2026-07-26.md`](./docs/evaluations/phase-3-5-model-bakeoff-2026-07-26.md) — Lite/Standard measurements, failures, and decision
18. [`docs/evaluations/voice-listening-rubric.md`](./docs/evaluations/voice-listening-rubric.md) — anonymous voice samples and human review
19. [`docs/phase-3-6-meditation-pacing-and-fish-evaluation.md`](./docs/phase-3-6-meditation-pacing-and-fish-evaluation.md) — slower narration, deterministic sentence rests, log fix, and Fish trial
20. [`docs/implementation-pr-plan.md`](./docs/implementation-pr-plan.md) — one bounded PR at a time
21. [`CONTRIBUTING.md`](./CONTRIBUTING.md) — contribution and review workflow
22. [`docs/ai-collaboration.md`](./docs/ai-collaboration.md) — bounded AI-assisted work

[`previous-chat.md`](./previous-chat.md) preserves the original discussion for historical context; it is not a current source of truth.

## Non-Negotiable Design Rules

- The local core must remain useful without a cloud dependency.
- Basic mode must remain useful without a local LLM.
- The canonical timeline, not generated prose or audio, is the source of truth.
- Deliberate silence is explicit and deterministic.
- Model-specific behavior stays inside adapters.
- A failed segment must not require regenerating a complete meditation.
- Public artifacts require explicit license and provenance checks.
- Commons must never become a dependency of local generation.
- No installer or model manager may load an artifact that fails the live-resource safety check.

## Development Commands

```bash
uv sync --extra dev --locked                 # exact cross-platform setup
uv run whoopy doctor                         # select a safe native profile
uv run whoopy models doctor                  # inspect its immutable artifact plan
uv run whoopy models install --profile auto  # explicitly install verified artifacts
uv run --offline whoopy web --open           # private browser tester
uv run whoopy draft "A calm pause." --minutes 3
uv run whoopy generate "A calm pause." --minutes 3
uv run whoopy generate --script-file examples/first-meditation.md
uv run whoopy evaluate --output-dir evaluations/local/my-bakeoff
uv run whoopy run create "A calm pause."     # save a queued local run
uv run --extra dev python scripts/check.py   # complete local/CI quality gate

make setup          # optional Unix wrapper for uv sync
make test           # optional Unix test wrapper
make format         # optional Unix formatting wrapper
make check          # optional Unix wrapper for the same Python check script
```

Commands such as `make worker` and `make dev` remain future target interfaces.
The worker still processes one explicitly named run in the foreground; it is
not a polling or concurrent background service.

## License

No project-wide license has been selected yet. Do not assume the repository or its future generated artifacts are redistributable until a license is added. Third-party model and asset licenses are tracked separately and must be reviewed before public publishing.
