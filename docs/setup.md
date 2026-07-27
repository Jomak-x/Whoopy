# Setup And Build Guide

Whoopy uses the same locked developer workflow on Windows, macOS, and Linux. Docker is not required. Actual end-user installers arrive after the native runtimes exist; Phase 0 proves that setup, hardware inspection, configuration, and quality checks are portable.

## Phase 0: Run The Foundation

### 1. Install Git And uv

Install Git through the operating system's normal installer or package manager. Install `uv` using its [official cross-platform instructions](https://docs.astral.sh/uv/getting-started/installation/).

`uv` is the bootstrap dependency. It can install the Python version requested by `.python-version`, create the isolated environment, and reproduce `uv.lock`. A contributor does not need to configure a system Python manually.

### 2. Install The Locked Environment

Run the same command in PowerShell, Command Prompt, Terminal, or a Linux shell:

```bash
uv sync --extra dev --locked
```

This phase installs only lightweight configuration and development packages. It does not download a language model, TTS model, FFmpeg, CUDA toolkit, or MLX.

### 3. Inspect The Laptop

```bash
uv run whoopy doctor
```

The command reports the operating system, architecture, CPU threads, total and currently available RAM, free disk, detectable acceleration, and highest safe profile. JSON output is available to installers and the future UI:

```bash
uv run whoopy doctor --json
uv run whoopy doctor --profile lite
```

Possible profiles:

- Basic: templates or pasted scripts with local speech, no local LLM;
- Lite: a 1–2B-class quantized local model;
- Standard: a 3–8B-class quantized local model;
- High: an 8–14B-class quantized local model;
- Studio: a 30B-class quantized local model.

Phase 0 makes no download based on this result. Later model management must repeat the live check and run a short runtime benchmark before selecting a precise artifact.

### 4. Verify The Repository

```bash
uv run whoopy --help
uv run whoopy config show
uv run --extra dev python scripts/check.py
```

Unix contributors can optionally use:

```bash
make setup
make check
```

Make is not required on Windows. The Python check script is the shared contract used by every operating system in CI.

## Phase 1: Run The Local-Core Skeleton

Create a durable queued run:

```bash
uv run whoopy run create "A short grounding meditation."
```

Copy the printed run ID and process it with the separate worker command:

```bash
uv run whoopy worker process <run-id>
uv run whoopy run show <run-id>
```

The first command writes `runs/<run-id>/run.json`. The worker changes its state
from `queued` to `running`, writes `timeline.json`, then marks the record
`completed`. Add `--json` for machine-readable command output or
`--runs-dir PATH` to use a temporary artifact root.

The timeline is intentionally a one-segment prompt passthrough. This phase tests
the control-plane, persistence, worker, and artifact boundaries; it does not
generate a script or audio. See
[`phase-1-local-core.md`](./phase-1-local-core.md) for the complete walkthrough.

## Phase 2: Render Deterministic Fixture Audio

The same worker command now writes:

```text
runs/<run-id>/
├── run.json
├── timeline.json
├── narration.wav
├── audio-manifest.json
└── quality.json
```

Open `narration.wav` in any normal audio player. You will hear deterministic
tones, not speech, separated by an exact 1.5-second silence. Inspect
`audio-manifest.json` for frame ranges and `quality.json` for the read-back
checks.

No FFmpeg or model is required in this phase. See
[`phase-2-deterministic-audio.md`](./phase-2-deterministic-audio.md).

## Phase 3: Inspect Cache And Resume

Phase 3 adds two ignored storage layers:

```text
runs/.cache/segments/<key>/       shared verified speech
runs/<run-id>/segments/<id>/      this run's verified checkpoint
```

Render the same prompt twice and inspect reuse:

```bash
uv run whoopy run create "A short grounding meditation."
uv run whoopy worker process <first-run-id>
uv run whoopy run create "A short grounding meditation."
uv run whoopy worker process <second-run-id>
uv run whoopy cache stats
```

The second run's `run.json` reports two cache hits for the current two-speech
fixture timeline. Recover a run left in `failed` or `running`:

```bash
uv run whoopy run resume <run-id>
```

Whoopy revalidates completed segment PCM before reusing it. Corrupt cache or
checkpoint bytes are never trusted. See
[`phase-3-quality-caching-recovery.md`](./phase-3-quality-caching-recovery.md)
for retry classification, directory layout, and integrity checks.

### Prepare For Offline Development

No model or new dependency is needed for Phase 3. While internet is available,
run:

```bash
uv sync --extra dev --locked
uv run --offline --extra dev python scripts/check.py
```

The second command proves the current environment and local `uv` cache can run
the full quality gate without a network request. Do not delete `.venv` or clear
the `uv` cache before traveling.

## Local Configuration

Defaults work without local files. Use process environment variables for temporary or secret overrides:

```bash
export WHOOPY_TTS__VOICE=af_heart       # macOS/Linux
$env:WHOOPY_TTS__VOICE = "af_heart"    # PowerShell
```

Create `config/local.yaml` for durable non-secret machine overrides:

```yaml
tts:
  voice: af_heart
hardware:
  profile: auto
pipeline:
  checkpoint_dir: ./runs
```

Do not commit `.env` or `config/local.yaml`. `.env.example` documents variable names, but Phase 0 does not automatically load `.env`.

## Native Runtime Strategy

The future normal path is:

- llama.cpp/GGUF for cross-platform local script generation;
- sherpa-onnx/Kokoro for cross-platform local speech;
- FFmpeg for deterministic rendering;
- optional MLX or other accelerators behind the same typed ports.

Users will not select Metal, CUDA, Vulkan, or CPU manually. Runtime inspection and benchmarks resolve the best available implementation. Apple Silicon remains well accelerated, but it is no longer a prerequisite.

## Later Native Requirements

The adapter and audio PRs will add verified downloads for the correct platform binaries. Developers working on those adapters may need native build tools, but normal users should not.

The intended release artifacts are:

- a signed macOS application or installer;
- a signed Windows installer;
- a Linux AppImage or equivalent package.

Each artifact will bundle the application runtime and correct platform binaries. Model files download separately only after compatibility and license checks.

## Build Order

1. portable repository, configuration, hardware doctor, and CI;
2. canonical timeline types and validation;
3. deterministic compiler and placeholder audio;
4. renderer, cache, and quality checks;
5. typed runtime ports and fixture adapters;
6. universal llama.cpp and sherpa-onnx adapters;
7. measured model profiles and artifact management;
8. script generation and recovery;
9. local API, worker, database, and PWA;
10. native installers;
11. optional Commons platform.

## Troubleshooting

### `uv` is not found

Restart the terminal after installation or follow the PATH instructions printed by the official installer.

### The lockfile is out of date

`uv sync --locked` refuses dependency drift deliberately. Contributors changing `pyproject.toml` must run `uv lock` and commit the resulting lockfile.

### Doctor selects a lower profile than total RAM suggests

Selection uses currently available RAM as well as total RAM. Close memory-heavy applications and run it again. This prevents a model that normally fits from causing an out-of-memory failure under current load.

### Doctor selects Basic

Basic is a supported product mode, not an error. It avoids a local LLM while retaining templates, pasted scripts, timeline compilation, local speech, and rendering once those features are implemented.

### Doctor reports unsupported

The laptop does not currently satisfy Basic's RAM or disk margin. Free resources and rerun the command. Whoopy deliberately refuses to attempt a model load.

### A model named in `models.yaml` cannot run

Expected in Phase 0: entries remain `status: planned`. Hardware detection is active; model adapters and downloads are not.
