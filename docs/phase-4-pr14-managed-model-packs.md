# PR 14: Managed Local Model Packs

PR 14 turns optional voice experiments into named, inspectable local packs.
It does not choose Whoopy's final voice. It gives you a safe way to compare
the available voices on your own laptop.

## The idea in plain language

A **model pack** is the complete set of files one voice model needs: its
runtime, checkpoint files, codecs, and any declared reference material. A pack
is not considered ready merely because one large download exists. Whoopy checks
that all declared files are present and valid before it lets a pack be selected.

The pack declaration is `config/model_packs.yaml`. It is a YAML file: a
human-readable list of settings and facts. It records each pack's ID, license,
required files, and local resource expectations. The actual multi-gigabyte
downloads live under `models/`, which is deliberately not committed to Git.

Voice cloning has a separate safety declaration in
`config/voice_references.yaml`. It names one user-provided recording and
transcript, records explicit local-experiment consent, and pins both byte sizes
and SHA-256 digests. Generation, resume, one-segment regeneration, the Studio,
and smoke tests all resolve that declaration. They reject missing, changed, or
symlinked files and never search the laptop for a convenient recording.

The declared comparison set is:

| Pack ID | Purpose | License in this pinned pack |
| --- | --- | --- |
| `kokoro` | small portable baseline | Apache-2.0 |
| `fish-speech-1.4` | expressive Fish experiment | CC-BY-NC-SA-4.0; **non-commercial only** |
| `moss-audio-tokenizer-v2` | codec required by both MOSS voices | Apache-2.0 |
| `moss-local-5b` | MOSS Local Transformer 5B | Apache-2.0 |
| `moss-8b` | larger MOSS delay model | Apache-2.0 |

Open source does not automatically mean “commercial use is allowed.” Fish
1.4 permits local non-commercial experimentation under its pinned Creative
Commons license, including its attribution and ShareAlike conditions. Whoopy
shows that restriction in both terminal and Studio status rather than hiding
it. The MOSS checkpoints and tokenizer are published under Apache-2.0. Read
the source model cards when changing a revision: [Fish Speech 1.4](https://huggingface.co/fishaudio/fish-speech-1.4),
[MOSS Local 5B](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5),
[MOSS 8B](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-v1.5), and
[MOSS Audio Tokenizer v2](https://huggingface.co/OpenMOSS-Team/MOSS-Audio-Tokenizer-v2).

## What every state means

`list`, `verify`, the Studio, and automation use the same seven states:

| State | Meaning |
| --- | --- |
| `missing` | none of the pinned pack files were found |
| `partial` | only some pinned files exist; it cannot be selected |
| `corrupt` | a size, digest, or shard index does not match |
| `installed` | bytes are exact, but runtime/synthesis proof is still missing |
| `resource_blocked` | files fit, but current RAM, disk margin, or accelerator does not |
| `incompatible` | this platform is unsupported or the current runtime/smoke probe failed |
| `ready` | files, shards, platform, hardware, isolated runtime, and an offline synthesis tied to this machine and revision all passed |

This distinction is intentional. “Installed” is not a softer spelling of
“ready.” A successful smoke result is invalidated when the model revision,
runtime revision, machine identity, runtime interpreter, installed packages,
runtime markers, lockfiles, or Whoopy's worker/adapter code changes.

## Commands

First see what this copy of Whoopy knows about:

```sh
uv run whoopy models pack list
```

Each row tells you whether the pack is missing, incomplete, exactly installed,
smoke-tested and ready, resource-blocked, or incompatible with this laptop.
Nothing is loaded by this command.

Install one named pack:

```sh
uv run whoopy models pack install moss-local-5b
```

This command installs the pack's pinned checkpoint/processor files. It also
checks for the exact isolated runtime revision, but PR 14 does not build a
Fish or MOSS Python environment from mutable package indexes. On this
development laptop those runtimes were provisioned and version-checked during
the earlier voice experiment. On another laptop, installed model bytes remain
`installed`—never `ready`—until the matching isolated runtime and any consented
reference recording exist. The clean-machine runtime installer is still an
explicit Local V1 exit-gate item; do not copy a `.venv` between operating
systems.

Kokoro uses Whoopy's existing cross-platform baseline artifact installer
because the official sherpa-onnx bundle is a release archive rather than a set
of individually downloadable files. The pack registry verifies both its three
runtime entry files and the digest of the complete extracted bundle, so missing
language data cannot be mistaken for a ready model:

```sh
uv run whoopy models install --profile basic
uv run whoopy models pack smoke-test kokoro
```

`models pack install kokoro` reuses that verified installation; it deliberately
refuses to invent per-file GitHub download URLs for the archive.

If you already downloaded files while you had fast internet, point Whoopy at
them. It will verify them before using them:

```sh
uv run whoopy models pack install moss-local-5b --offline-dir /path/to/downloads --no-network
```

Useful checks and recovery commands are:

```sh
uv run whoopy models pack verify moss-local-5b
uv run whoopy models pack smoke-test moss-local-5b
uv run whoopy models pack unload
uv run whoopy models pack remove moss-local-5b --confirm
uv run whoopy models pack restore RECEIPT_ID
```

`verify` checks the declared files without loading a model. `smoke-test` is the
only command here that temporarily loads the selected pack and makes a tiny
local test clip. It forces the supported model libraries into offline mode,
uses the same adapter as a real render, and unloads afterward. It records
startup time, render time per audio second, peak process-tree memory,
accelerator, unload time, and memory after unload under
`models/managed/model-packs/PACK_ID/records/`. For MOSS, the real synthesis
also proves the Audio Tokenizer v2 reference-encode and output-decode paths.

`unload` reports the OS-backed heavyweight slot owner. A live adapter owns that
slot for its entire process lifetime; Fish, MOSS, and future Qwen runtimes
cannot load over one another. The kernel releases the lock even after a crash.

`remove` requires `--confirm` and moves only the managed files for that pack to
Whoopy's recoverable trash. It never accepts an arbitrary path, so it cannot
delete unrelated files. Keep the receipt ID printed by the command if you may
want to restore the pack later.

Add `--json` to any command when a script or the local Studio needs structured
results instead of prose.

## Local Studio API

The same local-only web server exposes pack status at `GET /api/model-packs`.
Its actions live below `/api/model-packs/`; the browser sends only declared pack
IDs, never filesystem paths. The Studio uses these endpoints so its model
chooser and the terminal always report the same source of truth.

## Safety boundaries

- Installation can use the network only when you explicitly ask for it.
- Once installed, synthesis remains local; no prompt or audio is sent away.
- The inference workers set offline mode and never download missing weights.
- A partial or failed download is never selectable as a voice.
- Optional generation backends must resolve to a registry `ready` state; there
  is no fallback to an old hard-coded experimental path.
- Reference-based voices use only the explicit consent declaration above;
  recursive filename searches are forbidden.
- A heavyweight-model slot prevents two large TTS models from silently staying
  in memory together.
- Licenses and model controls stay attached to the pack and later run records.

## Preparing for travel or unreliable internet

While online, intentionally install and verify the packs you want, then run a
smoke test for each one. Before leaving, disconnect networking and repeat the
status/smoke commands with `uv run --offline`:

```sh
uv run --offline whoopy models pack list
uv run --offline whoopy models pack verify moss-local-5b
uv run --offline whoopy models pack smoke-test moss-local-5b
```

The Python `--offline` switch and Whoopy's inference offline mode are separate
safety layers: the first prevents dependency resolution; the second prevents
model libraries from trying the internet. Keep `models/` intact while
travelling. Multi-gigabyte weights are never placed in Git.

## Real validation performed for this PR

The complete pinned MOSS 8B download was verified shard-by-shard. On the
48 GB Apple-Silicon development Mac, a direct offline recovery run using the
8B checkpoint rendered a 9.44-second WAV at 24 kHz, passed every timing/hash/
headroom check, and contained zero clipped samples. That proves this one
machine can execute the model; it does not promise that a smaller laptop can.
The managed `smoke-test` command is the reproducible readiness gate on every
other machine.

The production smoke gate then measured the current paths on that Mac:

| Pack | Startup | Render / audio | Peak worker memory | Runtime device |
| --- | ---: | ---: | ---: | --- |
| Fish 1.4 | 9.15 s | 2.52× | 2.35 GB | Apple Metal |
| MOSS Local 5B | 18.60 s | 2.18× | 8.29 GB | Apple Metal |
| MOSS 8B | 36.36 s | 12.01× | 33.09 GB | CPU |

MOSS 8B deliberately uses CPU on this pinned PyTorch/macOS combination.
Float16 produced invalid probabilities on Metal and float32 reached an MPS
backend assertion; both failures were retained during validation rather than
silently called successful. CPU completed reliably and unloaded cleanly. This
is why MOSS 8B remains an optional high-memory comparison, not the portable
default.

## What comes next

PR 15 adds the Qwen3-TTS comparison family. PR 16 then uses these managed packs
to make blind, like-for-like voice samples. Do not pick a final default from a
single casual test: compare the same words, timing, and loudness across packs.
