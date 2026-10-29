# Local V1 Setup And Offline Operation

This guide installs Whoopy's permanent local browser product. The target-specific offline
application bundle is the end-user path; the source checkout remains available to contributors.
Docker, a cloud account, and a separate frontend server are not required.

## Supported Release Targets

Local V1 defines five desktop targets:

| Operating system | Architecture                               |
| ---------------- | ------------------------------------------ |
| Windows          | x64                                        |
| macOS            | Apple Silicon (ARM64), Intel (x64)         |
| Linux            | x64, ARM64 where pinned dependencies exist |

Kokoro and authored-script Basic mode are the portable fallback. Optional voices can support fewer
targets. Windows ARM64 is not a Local V1 target.

## 1. Install Prerequisites

Install:

- Python 3.11 or 3.12 with `venv`/`ensurepip` support for a product-bundle install;
- Git and [`uv`](https://docs.astral.sh/uv/getting-started/installation/) for a source install;
- FFmpeg, including `ffprobe`, if you need finished FLAC, AAC/M4A, and Opus exports.

Use an FFmpeg package from your operating system's trusted package manager or the official FFmpeg
download references. Confirm both executable names resolve:

```bash
ffmpeg -version
ffprobe -version
```

Platform notes:

- **Windows x64:** use a native x64 PowerShell session (not WSL for Windows acceptance), install
  Git and `uv` from their official Windows installers, and confirm `where.exe ffmpeg` and
  `where.exe ffprobe` both resolve. Windows ARM64 is intentionally refused by `whoopy doctor`.
- **macOS ARM64/x64:** use Terminal and confirm `uname -m` reports `arm64` or `x86_64`. Apple
  Silicon can select Metal automatically; Intel Macs use the portable CPU path unless another
  reviewed backend is actually probed.
- **Linux x64/ARM64:** confirm `uname -m`, `command -v ffmpeg`, and `command -v ffprobe`. ARM64 is a
  release target only where the pinned runtime dependency declares that target; an optional model
  may still be unavailable while Basic/Kokoro remains the required portable route.

CUDA is selected only when `nvidia-smi` completes successfully. Vulkan is selected only when
`vulkaninfo --summary` completes successfully. Otherwise Whoopy keeps the CPU fallback; users do
not pass backend flags.

## 2. Install The Product Bundle

Use the archive matching the exact target in the table above and Python 3.11 or 3.12 named by its
filename. The archive contains Whoopy, the built Studio/Lab frontend, installed defaults, worker
scripts, and every Python dependency as a target-native wheel. It does not contain Python, FFmpeg,
or model checkpoints.

Compare the archive to its separately published `.zip.sha256` file before unpacking it. A checksum
proves integrity only when it was obtained through a trusted release channel; the self-contained
manifest is not a code signature. Then
disconnect package-index access if desired and install into a new directory:

```bash
python install.py --verify-only
python install.py --prefix /path/to/whoopy-local
```

The verifier rejects a wrong operating system, architecture, or Python family; missing, extra,
symlinked, resized, or checksum-mismatched files; and an inconsistent product-wheel identity. The
installer creates an isolated environment at its stable final path, invokes pip with `--no-index`,
`--require-hashes`, and binary wheels only, checks the dependency graph, verifies the launcher, and
only then atomically writes its completed receipt and removes the incomplete marker. A failed or
process-killed attempt is never recursively deleted: it keeps an exact bundle, destination, and
interpreter marker and can be resumed by repeating the same command. Arbitrary existing directories
and malformed or legacy markers are refused. It never copies a development virtual environment. The
receipt distinguishes hash/inventory verification and package-index isolation from authenticity
and operating-system network isolation; those latter properties require separately trusted release
metadata and an external network control.

Launch the installed product on macOS/Linux:

```bash
/path/to/whoopy-local/bin/whoopy app
```

Or on Windows PowerShell:

```powershell
C:\path\to\whoopy-local\Scripts\whoopy.exe app
```

After activating that environment, the command is simply `whoopy app`. Continue below using
`whoopy` in place of `uv run whoopy`.

### Contributor source installation

From the repository root, use the same command in PowerShell, Terminal, or a Linux shell:

```bash
uv sync --extra dev --locked
```

`uv` reads `.python-version`, installs the compatible Python 3.11 runtime when needed, and
reproduces `uv.lock`. Verify the command surface:

```bash
uv run whoopy --help
uv run whoopy app --help
uv run whoopy config show
```

Maintainers acquire a native bundle while networking is available with:

```bash
uv build --wheel
uv run python scripts/build_app_offline_bundle.py \
  --target darwin-arm64 --python 3.11 \
  --wheel-dir dist --output-dir dist/offline
```

Acquisition exports the committed lock without the project or development dependencies, downloads
only hash-matching wheels for the current native target, and produces a reproducible ZIP plus a
complete SHA-256 manifest. Cross-target wheel guessing is refused: each archive is built on its
declared native target.

## 3. Inspect The Current Machine

```bash
uv run whoopy doctor
uv run whoopy doctor --json
```

The doctor reports the normalized platform, CPU count, total and live available memory, disk space,
and probed accelerators. It selects a friendly quality profile rather than asking the user for
Metal/CUDA/Vulkan flags.

- **Basic:** authored scripts with local TTS; no LLM.
- **Lite:** experimental lower-resource prompt generation where explicitly requested.
- **Standard:** the dependable Qwen3-4B prompt path plus Kokoro.
- **High/Studio:** optional larger candidates only when measured and safe.

Selection is a resource preflight, not a model readiness claim.

## 4. Acquire And Prove The Basic Voice

List current declarations and honest states:

```bash
uv run whoopy models pack list
```

Install Kokoro, then run real local synthesis:

```bash
uv run whoopy models pack install kokoro
uv run whoopy models pack smoke-test kokoro
```

The distinction matters:

- `missing`, `partial`, or `corrupt` describes file integrity;
- `installed` means pinned files and runtime markers exist;
- `resource_blocked` or `incompatible` prevents a load and removes that model from the Studio creation selector;
- `ready` means this exact model/runtime revision produced non-empty, playable offline audio on this
  machine.

`whoopy models pack verify kokoro` rechecks files without loading the model. The app performs the
same production smoke test when its model card says **Test**. A historical smoke file for a previous
revision or another machine cannot make a selector usable.

The older profile artifact installer is still used by the Standard LLM path:

```bash
uv run whoopy models doctor
uv run whoopy models install --profile standard
```

All managed downloads are versioned and verified. Installation can resume after interruption.

## 5. Start The Product

```bash
uv run whoopy app
```

Whoopy starts the versioned FastAPI control plane, reconciles expired runs, serves the bundled
Svelte application, supervises native work, and opens `http://127.0.0.1:8765`.

Useful launch options:

```bash
uv run whoopy app --no-open
uv run whoopy app --port 8877
uv run whoopy app --config-dir config --models-dir models/managed --runs-dir runs
```

Only `127.0.0.1` and `localhost` are accepted host values. The old `whoopy web` command remains a
legacy alias; new instructions use `whoopy app`.

## 6. Create A First Meditation

In Studio:

1. Select **From my script**.
2. Paste a short script containing ordinary prose and optional `[pause: 4s]` markers.
3. Keep Kokoro and a ready preset voice selected.
4. Select **Test this voice** for a short production sample, or **Create meditation** for the full
   script.
5. Follow the durable stage message. Cancel preserves verified checkpoints.
6. Open the completed library item and listen.

The equivalent CLI path is:

```bash
uv run --offline whoopy generate \
  --script-file examples/first-meditation.md \
  --profile basic
```

Prompt mode requires Lite or Standard model artifacts:

```bash
uv run --offline whoopy generate \
  "A three-minute grounding practice after an overstimulating day." \
  --minutes 3 \
  --meditation-kind grounding \
  --profile standard
```

Automatic prompt mode will not silently use the Lite model because its recorded V1 bake-off did not
qualify it as a dependable default. Lite remains an explicit experiment.

## 7. Finished Exports

The generation master is `runs/<run-id>/narration.wav`. From a completed run's **Listen** view,
select **Create all exports** to build:

```text
runs/<run-id>/exports/
├── finished.wav
├── finished.flac
├── finished.m4a
├── finished.opus
└── export-manifest.json
```

Whoopy safely discovers FFmpeg/FFprobe, writes to a sibling staging directory, decodes every output,
checks codec/channel/duration/metadata, hashes every file, and then activates the complete export
directory. A failed encode does not replace the previous export set. The manifest records title,
model, creation time, duration, tool version, codecs, and SHA-256 evidence.

## 8. Prepare For No-Network Use

While networking is available:

```bash
uv sync --extra dev --locked
uv run whoopy models pack install kokoro
uv run whoopy models pack smoke-test kokoro
# Install and smoke-test any optional pack you intend to use.
```

Then disconnect networking and launch:

```bash
uv run --offline whoopy app
```

Whoopy sets no cloud fallback by default (`hardware.allow_remote_fallback: false`). Model adapters
are configured for offline inference. The `--offline` option above belongs to `uv` and proves that
Python packages are not fetched; it does not itself disable the operating system's network device.

For release evidence, disable networking outside Whoopy and run the acceptance harness with
`--network-disabled` plus a descriptive `--network-isolation-note`. These fields are an explicit
operator attestation. On macOS, `--macos-sandbox-network-isolation` is a stronger alternative: it
enforces external-network denial for the complete acceptance process, preserves localhost, and
records before/after network probes rather than relying on an assertion.

After producing corpus, soak, interruption, Studio, Lab, platform, and packaging evidence, generate
the single fail-closed status report described in
[Consolidated Local V1 Acceptance Status](local-v1-acceptance-status.md). CI portability declarations
remain informational and cannot substitute for reports observed independently on each target.

## 9. Optional Voice Packs

Optional checkpoints and their isolated Python runtime are separate verified artifacts. Inspect
both before downloading large files:

```bash
uv run whoopy models runtime list
uv run whoopy models pack list
```

`runtime list` may truthfully report `unavailable`. That means this Whoopy revision has no
checksum-reviewed bundle for the current target. Do not copy a development `.venv` into the models
directory; use Kokoro until a reviewed bundle is declared. When a target-specific runtime bundle is
published, its clean resumable install path is:

```bash
uv run whoopy models runtime install fish-speech
uv run whoopy models runtime verify fish-speech
```

Locked-wheelhouse releases may require the exact Python patch interpreter declared by the runtime:

```bash
uv run whoopy models runtime install fish-speech \
  --offline-dir /path/to/release-bundles --no-network \
  --python /path/to/python3.10

uv run whoopy models runtime install moss-tts \
  --offline-dir /path/to/release-bundles --no-network \
  --python /path/to/python3.12
```

Whoopy creates a fresh isolated environment from the verified offline wheelhouse; it never copies a
development virtualenv. Maintainers can see the complete publication process and current blockers
in [Optional Runtime Bundle Release Process](runtime-bundle-release.md).

Only after both the runtime and checkpoint are declared available should you install and smoke-test
the checkpoint:

```bash
uv run whoopy models pack install fish-speech-1.4
uv run whoopy models pack smoke-test fish-speech-1.4

uv run whoopy models pack install moss-local-5b
uv run whoopy models pack smoke-test moss-local-5b
```

MOSS packs are very large and require their tokenizer dependency plus substantial free memory. All
heavyweight runtimes execute sequentially through one model slot. If a prior process owns it:

```bash
uv run whoopy models pack unload
```

This reports the owner; it does not kill an unrelated process. See
[model capabilities](model-capabilities.md) before using a reference voice or a non-commercial
model.

## 10. Configuration And Data Locations

Configuration precedence remains:

```text
config/default.yaml < config/local.yaml < WHOOPY_* environment < CLI flags
```

Put machine-local overrides in `config/local.yaml`. Nested environment settings use two
underscores:

```bash
WHOOPY_TTS__VOICE=af_bella uv run whoopy config show
```

The defaults use `./runs`, `./models`, and `./whoopy.db` relative to the launch directory. Studio's
**Local data and settings** disclosure shows the resolved paths. Exported settings do not include
model weights or meditation artifacts.

That disclosure also provides the shipped voice-reference manager. Import an existing PCM16 WAV,
or record up to 60 seconds through the browser microphone, enter its exact transcript and a display
name, and confirm the visible rights statement. Studio converts browser recordings locally to
PCM16 WAV, previews the managed reference, and can move it to recoverable trash. The reference
alone does not make a Qwen cloning model ready.

Read [privacy and local data](privacy.md) before moving or backing up these directories.

## 11. Recovery And Cleanup

The app reconciles expired leases at startup. CLI equivalents are:

```bash
uv run whoopy run reconcile
uv run whoopy run show <run-id>
uv run whoopy run resume <run-id>
uv run whoopy run cancel <run-id>
uv run whoopy run regenerate-segment <run-id> <segment-id>
uv run whoopy cache stats
```

Deleting a stopped run in Studio moves it to `runs/.trash/`; model removal requires explicit
confirmation and uses managed trash. Do not manually delete an active run or the shared cache while
the app is running.

## 12. Release Acceptance Harness

Use the checked-in, versioned scenario:

```text
config/acceptance/local-v1-primary-v1.json
```

It declares four fresh production prompt-to-audio runs at each of 1, 3, 5, 10, and 20 minutes.
The case driver verifies completed durable state, required artifacts, PCM WAV readability, QC, and
the 5% duration gate, then writes a per-run receipt with paths, sizes, and hashes. Run it from the
repository root after installing the development environment and required model packs:

```bash
uv run --extra dev python scripts/local_v1_acceptance.py \
  --scenario config/acceptance/local-v1-primary-v1.json \
  --output evaluations/local/acceptance.json \
  --required-pack kokoro \
  --network-disabled \
  --network-isolation-note "Wi-Fi disabled and no wired interface connected"
```

On macOS, the harness can instead enforce and verify process-level OS isolation itself without
turning off networking for unrelated applications:

```bash
uv run python scripts/offline_bundle_acceptance.py \
  --bundle dist/offline/whoopy-app-0.1.0-darwin-arm64-py311 \
  --output evaluations/local/offline-bundle-darwin-arm64.json \
  --models-dir models/managed \
  --lifecycle-output evaluations/local/offline-installed-studio-lifecycle.json \
  --macos-sandbox-network-isolation
```

This mode first proves that at least one fixed external TCP endpoint is reachable. It then
re-executes the complete fresh installation and Studio lifecycle under `/usr/bin/sandbox-exec`
with external outbound networking denied and `localhost` explicitly allowed. Inside the sandbox
it requires the same formerly reachable endpoints to fail with `EPERM` or `EACCES`, performs a
real 127.0.0.1 round trip, and stores the control probes, denial probes, and exact sandbox-profile
digest in the receipt. If the pre-sandbox control cannot reach an endpoint, the harness refuses to
claim isolation. The macOS mode and operator attestation are mutually exclusive.

The suite also declares runnable planning, drafting, synthesis, assembly, encoding, and QC
interruption cases. The production process commits the requested stage to `run.json` before
atomically publishing a structured marker with its PID, run UUID, and durable stage. The harness
removes stale markers, validates the new marker against the exact child, terminates it, and then
runs the checked-in recovery verifier. A forced case passes only if the child exits non-zero, the
durable interrupted stage matches, recovery produces playable QC-passing audio, all preexisting
healthy generation/checkpoint artifacts keep their verified content, and completed speech
checkpoints are actually reused. A timed stop alone is not stage evidence. To run only selected
cases during development, repeat `--case interrupt-planning` (or another declared case ID); a
subset does not prove the full suite. Other release targets remain `not_run` in this machine's
report. Every case uses the portable `{python}` placeholder, resolved to the current interpreter on
Windows, macOS, and Linux.

Each successful soak subprocess must emit a versioned production receipt. The harness validates its
duration, QC result, complete artifact inventory, sizes, and SHA-256 values, then embeds the receipt
and its own digest in the platform report. A successful process exit by itself is not acceptance.

The checked-in suite is a release requirement declaration, not proof that its commands have run.
Do not claim the 20-run soak, six-stage recovery matrix, or another operating system from the file
alone; cite the generated acceptance report and production-case receipts. Passing platform reports
are bound to the canonical SHA-256 of every suite command, timeout, repetition, and recovery
control plus the current Whoopy product version. Changing the suite invalidates earlier reports.

On each independently operated target, disable networking after acquisition and run the bounded
platform suite (replace the output target name and describe the actual external control):

```bash
python scripts/local_v1_acceptance.py \
  --scenario config/acceptance/local-v1-platform-v1.json \
  --output acceptance/windows-x86_64.json \
  --product-wheel release-input/whoopy-0.1.0-py3-none-any.whl \
  --required-pack kokoro \
  --network-disabled \
  --network-isolation-note "All host network adapters disabled for the complete run"
```

On macOS, use the same canonical platform suite with verified process isolation instead of the
operator flags:

```bash
python scripts/local_v1_acceptance.py \
  --scenario config/acceptance/local-v1-platform-v1.json \
  --output acceptance/darwin-arm64.json \
  --product-wheel release-input/whoopy-0.1.0-py3-none-any.whl \
  --required-pack kokoro \
  --macos-sandbox-network-isolation
```

The platform receipt stores the same pre-sandbox reachability, in-sandbox `EPERM`/`EACCES`,
loopback, and profile-digest evidence as the installed-bundle harness. This proves only the current
Mac target; it does not stand in for a Windows, Linux, or second-architecture report.

Copy each independently generated report without editing it, then aggregate them on the release
machine:

```bash
uv run --extra dev python scripts/aggregate_local_v1_evidence.py \
  --scenario config/acceptance/local-v1-platform-v1.json \
  --report evidence/windows-x86_64.json \
  --report evidence/darwin-arm64.json \
  --report evidence/darwin-x86_64.json \
  --report evidence/linux-x86_64.json \
  --report evidence/linux-arm64.json \
  --output evaluations/local/cross-platform-release.json
```

The platform suite runs one real Standard/Kokoro prompt-to-audio smoke on each target and embeds its
verified production receipt. The separate primary-machine suite retains the twenty-run duration
soak and six forced-interruption cases, avoiding an unnecessary 20-run soak on every operating
system. The aggregator hashes every imported report and requires its exact suite digest, product
version, target identity, validated network-isolation evidence, migration audit, and embedded soak
evidence.
Missing targets stay `not_run`; partial or mismatched reports are rejected.

Hosted CI separately exercises a fresh built wheel on the exact Windows x64, macOS ARM64/x64, and
Linux x64/ARM64 runner labels. Each job acquires a target-native application wheelhouse, installs it
into a new environment with indexes disabled, starts `whoopy app` from an unrelated temporary
directory without a checkout config path, checks the versioned API and bundled frontend, and
uploads the bundle plus a hashed packaging receipt. `scripts/aggregate_ci_portability.py` requires
all five launcher receipts but always writes `local_v1_release_ready: false`: hosted packaging
checks are not real-model, OS-network-disabled, or complete independent acceptance evidence.

For a real post-acquisition packaging receipt, disable networking through an external operating
system control and describe that control explicitly:

```bash
uv run python scripts/offline_bundle_acceptance.py \
  --bundle dist/offline/whoopy-app-0.1.0-darwin-arm64-py311 \
  --output evaluations/local/offline-bundle-darwin-arm64.json \
  --models-dir models/managed \
  --lifecycle-output evaluations/local/offline-installed-studio-lifecycle.json \
  --network-disabled \
  --network-isolation-note "Wi-Fi disabled and no wired interface connected"
```

The harness installs from scratch, launches the installed console script without repository
configuration, probes the bundled API/frontend, then uses acquired Kokoro files to generate, play,
edit, export, and recoverably delete through production localhost APIs. A health-only run records
`offline_launch_passed` but deliberately keeps `offline_acceptance_passed: false`; the latter also
requires the complete Studio lifecycle. Without `--network-disabled` or the verified macOS
isolation mode, it may prove packaging but writes both offline flags false. With either valid
isolation proof and the lifecycle it records real voice
synthesis but keeps `llm_prompt_generation_tested: false`,
`independent_platform_evidence: false`, and
`local_v1_release_ready: false`; the complete platform suite and real acquired models remain
separate required evidence.

Repeat this installed-bundle lifecycle on all five release targets and pass each resulting receipt
to the consolidated status command with repeatable `--installed-bundle-report` arguments. This is
a separate required gate from `--platform-report`: platform network flags alone cannot prove that
the installed artifact serves its bundled UI and completes the production Studio workflow without
the checkout. The receipt embeds and hashes the lifecycle evidence and binds it to the bundle,
wheel, dependency lock, interpreter target, product version, and loopback-only launch.

To exercise the user-facing lifecycle against an already running Studio with ready Kokoro and
FFmpeg, run:

```bash
uv run --extra dev python scripts/localhost_studio_lifecycle.py \
  --base-url http://127.0.0.1:8765 \
  --output evaluations/local/studio-lifecycle.json
```

The driver accepts only loopback HTTP, generates real Kokoro audio from an authored script, fetches
playable WAV, changes an exact silence event, waits for assembly, creates/downloads all four export
formats, and moves the run to recoverable trash. Missing readiness is `not_run`; a failed run is
preserved for diagnosis. This report proves that one running machine's Studio lifecycle only.

After all 20 soak runs have exports, verify their receipts and activated files with:

```bash
uv run --extra dev python scripts/verify_soak_exports.py \
  --receipts-dir evaluations/local/production-cases \
  --runs-dir runs \
  --output evaluations/local/soak-exports.json
```

That verifier requires exactly four cases at 1, 3, 5, 10, and 20 minutes and checks the narration
source hash plus WAV/FLAC/AAC/Opus manifest membership, byte counts, SHA-256 hashes, and successful
decode evidence for every run.

## 13. Contributor Verification

```bash
uv run --extra dev python scripts/check.py
```

This runs Ruff lint and formatting, strict mypy, and pytest.

If setup fails, continue with [troubleshooting](troubleshooting.md).
