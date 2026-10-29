# Optional Runtime Bundle Release Process

Fish, MOSS, and Qwen adapters do not make a Python checkout safe to ship. A production runtime
requires an exact source revision, an exact Python patch release, a complete target-specific wheel
set, hashes for every dependency, and a reproducible release archive. Whoopy intentionally reports
the runtime as `unavailable` until all of those inputs exist.

## Current blockers

Run the release audit without downloading anything:

```bash
uv run python scripts/validate_runtime_releases.py
```

The checked-in recipes pin the locally inspected Fish, MOSS, and Qwen Git revisions. Fish and MOSS
now have reviewed macOS ARM64 production-inference closures, exact locks, deterministic offline
bundles, and clean managed installs. Qwen now has the same pinned source, complete declared Python
dependency closure, deterministic bundle, clean offline install, and import proof. These are
explicit release states, not evidence from copied development environments.

The validator checks declared locks even before a bundle is published. It also binds the registry
to the recipe's exact repository, revision, Python patch release, target, and deterministic builder
filename. This prevents a placeholder lock path or a same-revision checkout from another repository
from making an empty runtime declaration look closer to release-ready than it is.

Recipe targets are capability claims, not build aspirations. Fish is currently declared only for
macOS arm64; MOSS is declared for macOS arm64 and Linux x64. Add another target only after its
production model-pack and adapter path have been validated there.

| Runtime | Reviewed source | Remaining release input |
| --- | --- | --- |
| Fish Speech 1.4 | `1359896b0648c688dee901794969d976a06c86be` | macOS ARM64 published; clean offline install and real Metal synthesis passed |
| MOSS-TTS | `58b20a0d5fcc6766658d50967a90a9d890009a46` | macOS ARM64 published; Linux x64 still needs an exact lock, wheelhouse, bundle, and real smoke |
| Qwen3-TTS | `022e286b98fbec7e1e916cb940cdf532cd9f488e` | macOS ARM64 runtime published; checkpoint acquisition and real production audio smoke remain |

The first Qwen model-pack scope is `Qwen3-TTS-12Hz-0.6B-CustomVoice` at Hugging Face revision
`85e237c12c027371202489a0ec509ded67b5e4b5`. Its 11 runtime-required files total 2,498,383,610
bytes and are individually pinned by Git object ID or LFS SHA-256. The 320,623,827-byte runtime
bundle contains 93 exact wheel hashes and installs on CPython 3.12.12 with no network. The model
still cannot become `ready` without verified checkpoint acquisition and the real offline smoke path.

The Qwen wheelhouse is validated against CPython 3.12 and macOS 12 ARM64 tags; all 93 wheels have
at least one compatible tag. Importing upstream's package also imports its legacy 25 Hz tokenizer,
whose Python `sox` wrapper warns when the external SoX executable is absent. Whoopy's reviewed
CustomVoice checkpoint uses the 12 Hz tokenizer and does not call that legacy executable; real
12 Hz model synthesis remains the authoritative later acceptance gate.

Fish source and model usage are non-commercial under CC BY-NC-SA 4.0. The runtime registry uses
the same license rather than inheriting an incorrect generic Python-package assumption.

The reviewed MOSS macOS bundle uses 37 exact wheels and supports macOS 14 or newer because its
pinned NumPy 2.1.0 wheel has a macOS 14 ARM64 deployment target. It deliberately excludes upstream
UI, training, CUDA, TorchCodec/FFmpeg, and audio-analysis dependencies that Whoopy's bounded PCM16
worker path does not import. A clean offline install plus real Local Transformer and 8B synthesis
proved this smaller closure. The 8B adapter remains on its guarded CPU path on macOS.

## Build one target bundle

Do this on the target architecture in a clean release workspace. Do not point the command at a
copied `.venv`; the builder reads tracked bytes from the exact Git object and ignores dirty or
untracked development files.

1. Resolve every transitive dependency to a wheel compatible with the recipe's exact Python and
   target. Store only those wheels in a target wheelhouse.
2. Produce a pip requirements file with exact `==` pins and `--hash=sha256:...` for every wheel.
   Network URLs, editable installs, recursive requirements, ranges, and unhashed files are rejected.
   `scripts/lock_runtime_wheelhouse.py` generates the canonical one-wheel-per-package lock and
   rejects ambiguous or non-wheel artifacts.
3. Review the dependency licenses and run the builder:

```bash
uv run python scripts/build_runtime_bundle.py fish-speech \
  --target darwin-arm64 \
  --source /reviewed/fish-speech \
  --lock /reviewed/locks/fish-speech-darwin-arm64.txt \
  --wheelhouse /reviewed/wheels/fish-speech-darwin-arm64 \
  --output /reviewed/releases
```

The command fails unless `HEAD` is the recipe revision, required tracked source markers exist, the
lock is exact and fully hashed, and every wheel name, version, and digest is bound to its matching
requirement. It emits
a deterministic `.tar.gz`, embedded `build.json`, archive size/SHA-256, and the exact YAML bundle
entry. Two builds from identical inputs must have the same archive digest.

Review the emitted entry, add the reviewed lock under a repository-controlled release-lock path,
declare that path for the target in `config/runtime_build_recipes.yaml`, and add the bundle entry to
`config/runtime_packs.yaml`. CI then rejects publication if the recipe/revision/Python disagree, the
lock is missing, or the new bundle does not use `locked-wheelhouse` installation.

## Clean install and verification

A user can copy the released archive into an offline directory and install without network access:

```bash
uv run whoopy models runtime install fish-speech \
  --offline-dir /media/whoopy-runtimes \
  --no-network \
  --python /opt/python-3.10.19/bin/python
uv run whoopy models runtime verify fish-speech
```

The installer verifies the archive size and SHA-256 before extraction, validates the embedded
source/lock/wheel inventory against the registry, verifies the exact Python patch version, creates
a new `.venv`, installs with `--no-index --require-hashes`, installs the pinned source without
dependency resolution, and runs the reviewed imports. Build-only wheels and locks are removed; the
installed tree is hashed into `.whoopy-runtime.json`. Any byte change makes subsequent verification
`corrupt`.

Downloads resume from the verified partial byte count. If the process is interrupted while creating
the venv, an exact install-intent record lets the next invocation discard only that unreceipted
managed directory and rebuild it from the already verified cached archive. A directory with a valid
receipt, a mismatched intent, or unknown user files is never silently replaced.

Checkpoint installation and a real current-machine audio smoke test are still separate gates. A
successfully installed runtime alone never makes a voice selectable in Studio.
