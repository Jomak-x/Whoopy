# Troubleshooting Local V1

Start with these read-only checks:

```bash
uv run whoopy doctor --json
uv run whoopy models pack list --json
uv run whoopy run reconcile --json
uv run whoopy cache stats
```

## The App Does Not Open

Run without automatic browser launch:

```bash
uv run whoopy app --no-open
```

Open `http://127.0.0.1:8765` manually. If the port is occupied, choose another:

```bash
uv run whoopy app --port 8877 --no-open
```

Whoopy deliberately refuses non-loopback hosts. Do not replace `127.0.0.1` with a LAN address.

If startup reports missing FastAPI/Uvicorn or frontend files, rerun the locked environment setup:

```bash
uv sync --extra dev --locked
```

On Windows, make sure the shell is native x64 PowerShell and use `where.exe whoopy`; on macOS or
Linux use `command -v whoopy`. The product serves its packaged `whoopy.webui/static` files and never
starts Node. Missing `index.html`, `app.js`, or `styles.css` indicates an incomplete Python package
or checkout, not a reason to run a frontend development server.

## Automatic GPU Selection Falls Back To CPU

This is safe behavior. Check `uv run whoopy doctor --json`:

- Apple Silicon advertises Metal from the supported OS/architecture pair;
- CUDA appears only when `nvidia-smi` succeeds;
- Vulkan appears only when `vulkaninfo --summary` succeeds;
- CPU is always retained as the portable fallback.

Install or repair the vendor runtime outside Whoopy, restart the shell, and run the doctor again.
Do not add raw backend flags to generation commands. A detected accelerator does not override model
pack compatibility or the live-memory check immediately before a heavyweight load.

## A Cross-Platform Report Is Rejected

Do not edit an acceptance JSON file or combine case subsets manually. Run the entire checked-in
suite on that target and pass the resulting file to `scripts/aggregate_local_v1_evidence.py`. The
aggregator rejects duplicate targets, a mismatched suite ID, missing cases or repetitions,
incomplete interruption recovery, foreign-platform claims, and a pass without offline/migration
evidence. A platform with no accepted report remains `not_run` by design.

## A Voice Is Installed But Disabled

`installed` is not `ready`. Run a real production smoke test:

```bash
uv run whoopy models pack verify <pack-id>
uv run whoopy models pack smoke-test <pack-id>
```

Interpret the state shown by `models pack list`:

- **missing/partial:** finish or resume installation;
- **corrupt:** do not load it; verify and reinstall the exact pinned bytes;
- **installed:** run the live smoke test;
- **resource_blocked:** close memory-heavy applications or select a smaller model;
- **incompatible:** that pack has no reviewed path for this platform;
- **ready:** refresh Studio; if it changes after a revision update, rerun the smoke test.

Do not copy or edit `runtime.json`/`smoke.json`. Readiness fingerprints the actual runtime.

## Model Installation Is Interrupted

Reissue the same installation. Managed progress and partial downloads are resumable:

```bash
uv run whoopy models pack install <pack-id>
```

For an offline file set:

```bash
uv run whoopy models pack install <pack-id> \
  --offline-dir /absolute/path/to/downloads \
  --no-network
```

A checksum mismatch is not recoverable by ignoring verification. Acquire the exact declared file.

## Legacy Experimental Runtime Directories Still Use Disk

Older development installs can remain under `models/experimental/fish-speech-1.4-runtime/` and
`models/experimental/moss-tts-runtime/`. Do not remove either parent directory: the current managed
pack registry can still select verified checkpoints below each `checkpoints/` child even though it
will not trust the copied `.venv` or source tree as an isolated runtime.

Inspect the checked-in cleanup allowlist without changing anything:

```bash
uv run whoopy models cleanup inspect --json
```

The report hashes every candidate, gives its exact byte count, reports active worker/model blocking,
and prints a confirmation such as `CLEANUP <plan-id>`. Review the candidate paths, then pass that
whole quoted token to apply:

```bash
uv run whoopy models cleanup apply --confirm "CLEANUP <plan-id>" --json
```

Apply re-inspects the filesystem, refuses a stale confirmation, reserves the heavyweight-model
slot, and moves only allowlisted paths into `models/.whoopy-cleanup/quarantine/`. It does not delete
bytes. Every file is hashed before and after its move, and the durable receipt remains under
`models/.whoopy-cleanup/receipts/`. Checkpoint directories are never exact cleanup targets; only
stale Hugging Face `*.incomplete` download files have a constrained checkpoint glob.

List receipts and restore one with its explicit token:

```bash
uv run whoopy models cleanup receipts --json
uv run whoopy models cleanup restore <receipt-id> \
  --confirm "RESTORE <receipt-id>" --json
```

Quarantine is recoverable but occupies the same disk. Only after testing that Whoopy no longer
needs the legacy files, permanently reclaim the receipt's reported bytes with a second, deliberately
irreversible confirmation:

```bash
uv run whoopy models cleanup purge <receipt-id> \
  --confirm "PURGE <receipt-id>" --json
```

Purge verifies the complete receipt before deleting its first byte, resumes safely if interrupted,
and keeps the final receipt with the purged byte total. A purged receipt cannot be restored.

Cleanup refuses selected, ancestor, or contained symlinks, path escapes, changed candidates,
tampered quarantine content, active generation workers, and an active heavyweight voice model.
The virtual-environment allowlist therefore targets regular `.venv/lib`, `.venv/share`, and metadata
entries rather than link-bearing `.venv/bin`. Do not edit the receipt or quarantine tree manually.

## The Heavyweight Model Slot Is Busy

```bash
uv run whoopy models pack unload
```

This safely reports whether the kernel lock is free and identifies a known owner. Whoopy does not
kill unrelated processes. Finish or stop the exact owning Whoopy process, then retry. A stale JSON
owner record alone cannot hold the kernel lock and is cleaned when safely detected.

## MOSS Loads But Produces No Audio

Treat empty output as a failed smoke, never readiness. Confirm:

- both the selected MOSS pack and MOSS Audio Tokenizer v2 verify;
- the current target is macOS ARM64 or Linux x64 as declared by the managed pack;
- live available RAM meets the pack requirement immediately before loading;
- the runtime is not owned by another process;
- reference/direct voice mode, language, instruction, and pronunciation inputs are valid.

Then rerun:

```bash
uv run whoopy models pack smoke-test moss-local-5b
# or
uv run whoopy models pack smoke-test moss-8b
```

Inspect the actionable failure and bounded runtime diagnostics. Do not rely on an old badge.

## A Run Is Stuck In Running

The app reconciles expired leases at startup. From the CLI:

```bash
uv run whoopy run reconcile <run-id>
uv run whoopy run show <run-id>
uv run whoopy run resume <run-id>
```

Do not resume an actively leased run. Cancellation preserves healthy checkpoints:

```bash
uv run whoopy run cancel <run-id>
```

If one speech segment is unhealthy:

```bash
uv run whoopy run regenerate-segment <run-id> <segment-id>
```

The operation archives previous evidence and bypasses the affected checkpoint/cache only.

## An Edit Regenerates More Than Expected

- Silence duration changes should require assembly only.
- Delivery/intensity/pace changes invalidate affected speech segments.
- Script changes are recompiled and compare segment identities.
- Restoring a revision rerenders its activated timeline.

Check the revision manifest's `change_kind` and `affected_speech_segment_ids`. All output-affecting
controls enter cache identity, so changing one cannot reuse incompatible speech. When a script edit
changes only wording, accepted controls on otherwise matching segments are preserved from the
canonical timeline; an explicit new control in the edited script takes effect.

## Finished Exports Fail

Confirm both tools are on `PATH`:

```bash
ffmpeg -version
ffprobe -version
```

Whoopy reports a specific missing-tool, encoder, codec, metadata, decode, duration, or activation
failure. The existing export directory remains authoritative when staging fails. Check free disk and
write permission on the run directory; then repeat the versioned API export request described in
[setup](setup.md#7-finished-exports). The retained Studio frontend does not expose this action.

Opus normally reports a 48 kHz decode rate even when the narration master is 24 kHz. That is part of
the Opus format and is not a sample-rate failure.

## Audio Is Silent, Clipped, Or The Wrong Length

Open `quality.json` and `audio-manifest.json`. A completed run must prove non-empty frames, audible
signal, exact silence timing, duration, hashes, headroom, clipping, and joins. If an artifact was
manually changed, restore a revision or resume/regenerate instead of editing manifests.

Requested prompt duration is accepted within the meditation engine's tolerance; final timeline
silence remains exact even though natural TTS speech length varies.

## Typed Cues Are Spoken Aloud

Supported controls must be standalone lines with valid values:

```markdown
[delivery: calm]
[intensity: subtle]
[pace: slow]
[voice-mode: preset]
[pause: 4s]
```

Unknown bracket controls should fail compilation. Ordinary brackets inside prose can be ambiguous;
remove them or rewrite as speech. If a valid control reaches audio, preserve the script/timeline and
report it as a compiler regression.

## Prompt Mode Refuses Lite

This is intentional for `--profile auto`: the recorded V1 evaluation did not qualify the 1.7B Lite
model as a dependable automatic fallback. Use Standard, use Basic with an authored script, or
explicitly request Lite only for an experiment.

## An Evaluation Experiment Produces Failed Trials

The versioned evaluation API uses a real production executor, so an accepted experiment creates
durable ordinary generation runs. A candidate can still fail when its exact condition is not exposed
by the production pipeline, its model is not ready, resources are insufficient, or generation/QC
fails. Inspect the saved report and source run rather than treating queue completion as candidate
success. The retained Studio frontend does not expose an evaluation surface.

Current production comparisons accept the implemented Qwen3-4B/1.7B LLM profiles, the supported
`plan_first`, `blueprint_first_sparse`, `three_candidate_structural_selection`, and
`recipe_whole_session` writing methods, exposed speech contracts, and reviewed pacing IDs. An
unsupported condition produces explicit failure evidence; fixture output is never substituted.

Experiments run sequentially. API pause, cancel, and resume operations preserve completed trials;
an active production run uses the ordinary durable cancellation path so verified work remains
reusable. On restart, stale `queued` or `running` batch control files reconcile to `paused` and never
auto-run. `incomplete` records an execution error, while `cancelled` records an explicit action.

Rejected drafts and validation reasons remain in the run's managed `raw-model-output/` evidence.
Malformed, oversized, linked, or out-of-workspace diagnostic files are ignored. If no attempt was
written, the failure occurred before the generator produced a draft or the workspace is absent.

## Settings Import Fails

Import the exact JSON envelope returned by `/api/v1/settings/export`. The current schema version is
1; unknown envelope fields, invalid keys, oversized values, or non-JSON values are rejected. An API
import replaces catalog settings atomically but does not move runs/models or change immutable run
metadata. The retained Studio frontend does not expose settings import/export.

## Reported Platform Support Looks Wrong

Local V1 release targets are Windows x64, macOS ARM64/x64, and Linux ARM64/x64 where dependencies
exist. A specific optional pack can be narrower. Registry entries or historical plans mentioning
Windows ARM64 do not make it a released target. Use the acceptance report's platform matrix and the
current managed pack status rather than an old document.
