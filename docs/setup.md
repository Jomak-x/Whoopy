# Setup And Build Guide

This guide separates what works in the repository today from the native ML, audio, and web tooling required by later phases.

## Phase 0: Run The Foundation

### Required Today

- Git
- Python 3.11 (reference and CI version) or Python 3.12
- Make

Create the environment and run every quality check:

```bash
make setup
source .venv/bin/activate
serenity --help
serenity config show
make check
```

If `python3.11` is installed under another name, override the Make variable:

```bash
make setup PYTHON=/absolute/path/to/python3.11
```

`make setup` is intentionally lightweight in Phase 0. It installs configuration and development dependencies but no model runtime or weights.

### Local Configuration

Defaults work without local files. Use process environment variables for temporary or secret overrides:

```bash
export SERENITY_TTS__VOICE=af_heart
```

`.env.example` documents the variable names. Phase 0 does not automatically load `.env`; if you create one, source it through your shell or process manager.

Create `config/local.yaml` only when YAML is more convenient:

```yaml
tts:
  voice: af_heart
pipeline:
  checkpoint_dir: /absolute/path/to/serenity-runs
```

Do not commit `.env` or `config/local.yaml`. Verify precedence with:

```bash
SERENITY_TTS__VOICE=environment_voice serenity config show
serenity config show --tts-voice cli_voice
```

## Later Native Audio And ML Requirements

The primary local target is an Apple Silicon Mac. Before the first real model and renderer PRs, install or verify:

- Homebrew
- FFmpeg
- Xcode Command Line Tools
- sufficient disk space for model weights and generated audio

Example checks:

```bash
xcode-select -p
brew --version
ffmpeg -version
```

The ML worker must run natively on macOS to use Metal/MLX or PyTorch MPS. Docker Desktop may be used later for web/API and stateless support services, but it is not the macOS ML execution path.

## Later Web Requirements

The SvelteKit phase will add:

- Node 20 or newer
- npm lockfile and scripts under `web/`
- a local API-to-PWA development command

Do not install frontend dependencies during Phase 0; the `web/` directory currently documents ownership only.

## Build Order

Use the detailed PR plan rather than jumping directly to real models:

1. repository and configuration foundation;
2. canonical timeline types and validation;
3. deterministic compiler and placeholder audio;
4. renderer, cache, and quality checks;
5. typed model ports and real adapters;
6. script generation and recovery;
7. local API, worker, database, and PWA;
8. model bakeoff and product hardening;
9. optional Commons platform.

The first meaningful product milestone is one prompt producing one inspectable timeline and one correctly timed audio file through the CLI. A web UI is not required to prove that core.

## Expected Runtime Paths

Later phases create these ignored paths automatically:

- `runs/` — manifests and intermediate artifacts for each generation;
- `cache/` — content-addressed reusable results;
- `models/` — optional repository-local model weights;
- `serenity.db` — local persistence.

Do not create or commit placeholders inside them. Tests use temporary directories so local data cannot influence their results.

## Troubleshooting

### `python3.11` is missing

Install Python 3.11 with your preferred version manager or Homebrew, then pass its absolute path through `PYTHON` if necessary.

### pip tries a user install inside `.venv`

The Makefile passes `--no-user` to neutralize a global `user = true` pip setting. If setup still fails, inspect `python -m pip config list` for additional machine-specific overrides.

### `serenity` is not found

Activate `.venv`, or invoke `.venv/bin/serenity` directly.

### Configuration reports an unknown field

Serenity rejects unknown keys to catch misspellings. Compare the field with `config/default.yaml` and use exactly one `__` separator between the section and field in environment variables, for example `SERENITY_TTS__VOICE`.

### A model named in `models.yaml` cannot run

That is expected in Phase 0: entries are provisional adapter declarations with `status: planned`. Real model loading is added only after typed ports and fixture adapters exist.
