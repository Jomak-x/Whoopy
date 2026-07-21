# Serenity

Serenity is a local-first, timeline-driven system for generating guided meditation audio. The project will first prove a deterministic local CLI on Apple Silicon, then add a local web application, and only later add the optional public Serenity Commons.

> [!IMPORTANT]
> The ML worker will run **natively on macOS**. Docker Desktop on macOS does not expose Metal/MLX acceleration to Linux containers, so Docker will be reserved for stateless support services and web/API processes.

## Current Status

Phase 0 establishes the repository foundation. It includes documentation, the Python package and CLI skeleton, typed layered configuration, ownership markers for future components, tests, and CI. It intentionally does **not** implement timelines, generation, audio, APIs, queues, or user interfaces.

The only functional commands in this phase are:

```bash
serenity --help
serenity config show
```

## Design In One Minute

Serenity turns a prompt into a canonical timeline before it creates audio:

```text
prompt -> plan -> script -> canonical timeline -> segment audio -> mix/master -> outputs
```

That timeline is the source of truth. Deliberate pauses become exact `SILENCE` events rather than timing guesses left to a TTS model. LLM, TTS, ambience, rendering, and publishing capabilities sit behind typed ports so a supported model can be replaced through an adapter and configuration instead of a pipeline rewrite.

Model entries in [`config/models.yaml`](./config/models.yaml) are provisional starting points. A registry entry records its adapter, exact model identifier, license, runtime, and publication policy. Real adapters and their contract tests arrive in later PRs.

## Quick Start

### Requirements

- macOS on Apple Silicon for the primary local ML path
- Python 3.11 (3.12 is supported for foundation tooling)
- Git
- FFmpeg for later audio phases
- Node 20 or newer for the later SvelteKit UI
- Xcode Command Line Tools and Homebrew for later native dependencies

### Install And Verify

```bash
make setup
source .venv/bin/activate
serenity --help
serenity config show
make check
```

`make setup` currently creates a Python environment and installs only lightweight foundation dependencies. It does not download model weights or install an ML runtime.

## Configuration

Configuration precedence is:

```text
config/default.yaml < config/local.yaml < SERENITY_* environment < CLI flags
```

Use `config/local.yaml` for machine-local non-secret overrides and process environment variables for secrets. `.env` is ignored by Git but is not automatically loaded in Phase 0; source it through your shell or process manager. Nested environment variables use two underscores:

```bash
SERENITY_TTS__VOICE=test_voice serenity config show
serenity config show --tts-voice cli_voice
```

See [`config/README.md`](./config/README.md) for the contract.

## Repository Map

```text
config/                versioned settings, model registry, pacing, prompts
src/serenity/          Python domain package and future local control plane
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
```

The `src/` layout prevents tests from accidentally importing an uninstalled working copy. See [`docs/repository-layout.md`](./docs/repository-layout.md) for component boundaries and placement rules.

## Start Here

Read in this order:

1. [`docs/architecture.md`](./docs/architecture.md) — concise system explanation
2. [`system-design.md`](./system-design.md) — canonical design specification
3. [`docs/repository-layout.md`](./docs/repository-layout.md) — where code belongs
4. [`docs/setup.md`](./docs/setup.md) — machine and build sequence
5. [`docs/roadmap.md`](./docs/roadmap.md) — implementation phases
6. [`docs/implementation-pr-plan.md`](./docs/implementation-pr-plan.md) — one bounded PR at a time
7. [`CONTRIBUTING.md`](./CONTRIBUTING.md) — contribution and review workflow
8. [`docs/ai-collaboration.md`](./docs/ai-collaboration.md) — bounded AI-assisted work

[`previous-chat.md`](./previous-chat.md) preserves the original discussion for historical context; it is not a current source of truth.

## Non-Negotiable Design Rules

- The local core must remain useful without a cloud dependency.
- The canonical timeline, not generated prose or audio, is the source of truth.
- Deliberate silence is explicit and deterministic.
- Model-specific behavior stays inside adapters.
- A failed segment must not require regenerating a complete meditation.
- Public artifacts require explicit license and provenance checks.
- Commons must never become a dependency of local generation.

## Development Commands

```bash
make setup          # create .venv and install the package with dev tools
make test           # run unit tests
make lint           # run Ruff lint checks
make format         # format Python files
make format-check   # verify formatting without changing files
make typecheck      # run strict mypy checks
make check          # run the complete local/CI quality gate
```

Commands such as `make worker`, `make dev`, and `serenity generate` are documented target interfaces, not Phase 0 features.

## License

No project-wide license has been selected yet. Do not assume the repository or its future generated artifacts are redistributable until a license is added. Third-party model and asset licenses are tracked separately and must be reviewed before public publishing.
