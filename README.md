# Serenity

Serenity is a local-first, timeline-driven system for generating guided meditation audio. The project will first prove a deterministic native CLI across ordinary Windows, macOS, and Linux laptops, then add a local web application, and only later add the optional public Serenity Commons.

> [!IMPORTANT]
> The model worker runs **natively, without Docker**. Serenity uses one automatic runtime path across Windows, macOS, and Linux: llama.cpp/GGUF for local text generation and sherpa-onnx/Kokoro for speech. Metal, CUDA, Vulkan, and CPU execution are runtime details selected for the user.

## Current Status

Phase 0 establishes the repository foundation. It includes documentation, the Python package and CLI skeleton, typed layered configuration, automatic hardware profiling, ownership markers for future components, tests, and native CI on Windows, macOS, and Linux. It intentionally does **not** implement timelines, model inference, audio rendering, APIs, queues, or user interfaces.

The only functional commands in this phase are:

```bash
serenity --help
serenity config show
serenity doctor
```

## Design In One Minute

Serenity turns a prompt into a canonical timeline before it creates audio:

```text
prompt -> plan -> script -> canonical timeline -> segment audio -> mix/master -> outputs
```

That timeline is the source of truth. Deliberate pauses become exact `SILENCE` events rather than timing guesses left to a TTS model. LLM, TTS, ambience, rendering, and publishing capabilities sit behind typed ports so a supported model can be replaced through an adapter and configuration instead of a pipeline rewrite.

Model entries in [`config/models.yaml`](./config/models.yaml) are provisional starting points. The default `auto` path selects a safe profile from [`config/runtime_profiles.yaml`](./config/runtime_profiles.yaml). A registry entry records its adapter, model identifier, license, runtime, supported platforms, and publication policy. Real adapters and their contract tests arrive in later PRs.

Weak laptops do not need a local LLM. Basic mode supports authored templates or pasted scripts plus local TTS. If even Basic is unsafe, Serenity refuses before downloading or loading a model and explains which resource is insufficient.

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
uv run serenity --help
uv run serenity config show
uv run serenity doctor
uv run --extra dev python scripts/check.py
```

`uv` reads `.python-version`, installs Python 3.11 when needed, and reproduces `uv.lock`. The setup installs only lightweight foundation dependencies; it does not download model weights or install an ML runtime. Unix contributors may use the equivalent `make setup` and `make check` wrappers.

## Configuration

Configuration precedence is:

```text
config/default.yaml < config/local.yaml < SERENITY_* environment < CLI flags
```

Use `config/local.yaml` for machine-local non-secret overrides and process environment variables for secrets. `.env` is ignored by Git but is not automatically loaded in Phase 0; source it through your shell or process manager. Nested environment variables use two underscores:

```bash
SERENITY_TTS__VOICE=test_voice uv run serenity config show
uv run serenity config show --tts-voice cli_voice
```

See [`config/README.md`](./config/README.md) for the contract.

## Repository Map

```text
config/                versioned settings, model registry, pacing, prompts
scripts/check.py        platform-neutral lint, format, type, and test gate
src/serenity/          Python domain package and future local control plane
  hardware.py          native capability inspection and safe profile selection
  ports/               typed capability contracts
  adapters/            model and infrastructure integrations
  timeline/            canonical timeline schema and compiler
  pipeline/            orchestration, checkpoints, cache, recovery
  qc/                  audio and content quality gates
  api/                 future local FastAPI control plane
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

1. [`docs/architecture.md`](./docs/architecture.md) — concise system explanation
2. [`system-design.md`](./system-design.md) — canonical design specification
3. [`docs/repository-layout.md`](./docs/repository-layout.md) — where code belongs
4. [`docs/setup.md`](./docs/setup.md) — machine and build sequence
5. [`docs/roadmap.md`](./docs/roadmap.md) — implementation phases
6. [`docs/native-portability.md`](./docs/native-portability.md) — automatic cross-platform runtime and weak-laptop behavior
7. [`docs/implementation-pr-plan.md`](./docs/implementation-pr-plan.md) — one bounded PR at a time
8. [`CONTRIBUTING.md`](./CONTRIBUTING.md) — contribution and review workflow
9. [`docs/ai-collaboration.md`](./docs/ai-collaboration.md) — bounded AI-assisted work

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
uv run serenity doctor                       # select a safe native profile
uv run --extra dev python scripts/check.py   # complete local/CI quality gate

make setup          # optional Unix wrapper for uv sync
make test           # optional Unix test wrapper
make format         # optional Unix formatting wrapper
make check          # optional Unix wrapper for the same Python check script
```

Commands such as `make worker`, `make dev`, and `serenity generate` are documented target interfaces, not Phase 0 features.

## License

No project-wide license has been selected yet. Do not assume the repository or its future generated artifacts are redistributable until a license is added. Third-party model and asset licenses are tracked separately and must be reviewed before public publishing.
