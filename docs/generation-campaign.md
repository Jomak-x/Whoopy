# Serial local generation campaigns

`scripts/generation_campaign.py` freezes a reviewed CLI comparison and plans it by default. It never installs a model, requests authentication, calls a paid API, or starts inference without `--execute`. It uses the existing `whoopy generate` command and keeps verification separate: a successful process exit does **not** establish writing or audio quality. Use the external ASR/listening workflow against retained run directories.

The initial JSON suite has six typed requests: three-minute pre-meeting grounding without breath guidance; three-minute contact grounding without sound or breath; five-minute sleep without reflection or a body scan; five-minute loving-kindness; five-minute body scan; and three-minute observation of the natural breath. The default selects only the first case with Standard, `plan_first`, and Kokoro `af_heart`.

## Plan before inference

Use an existing environment and installed model store. Substitute canonical absolute paths (on macOS use `/private/tmp`, rather than its `/tmp` symlink, for output roots).

```sh
PYTHONPATH=src /path/to/existing/.venv/bin/python scripts/generation_campaign.py \
  --source "$PWD" --python /path/to/existing/.venv/bin/python \
  --models-dir /path/to/installed/models/managed \
  --output /private/tmp/whoopy-generation-plan-unique
```

Planning exclusively creates the output directory, writes `*` to its `.gitignore`, freezes `src`, `scripts`, and `config`, and hashes the frozen files before constructing runnable evidence. `local.yaml`, bytecode, and bytecode cache directories are excluded. Symlinked inputs/output paths and an existing output root are refused. Outputs cannot be inside the three frozen trees or the installed model store. The source checkout and models are never rewritten. An optional `--artifact-lock /path/to/reviewed.yaml` replaces only the frozen `config/artifacts.yaml`; the original path and effective hash remain in the manifest.

Inspect `manifest.json`: it contains the complete typed configuration, selected queue, allocated run UUIDs, exact argv and full allowlisted offline environment, frozen SHA-256 inventory, separate Git HEAD/tracked-diff/status hashes only when the source itself is the exact repository root, and an explicit unverified status. Nested frozen snapshots explicitly omit enclosing-repository Git metadata and retain their canonical content inventory as authority. A plan is evidence only; it is not a resumable job. To run, invoke the same selection with **a fresh output path** and add `--execute`. This freezes the then-current source again, so preserve the planned checkout when comparing runs.

## Select a bounded matrix

Repeat selectors to request a cross product. Defaults apply independently to selectors not provided. `--all-cases` selects the six authored requests. `--max-cases` is a hard matrix-size limit, not truncation; default six. Queue order follows the configuration's case order and the provided profile/method/voice order, with duplicate selections collapsed.

```sh
PYTHONPATH=src /path/to/existing/.venv/bin/python scripts/generation_campaign.py \
  --models-dir /path/to/installed/models/managed \
  --python /path/to/existing/.venv/bin/python \
  --output /private/tmp/whoopy-generation-comparison-unique \
  --case meeting-grounding --case contact-only-grounding \
  --profile high --profile standard \
  --method recipe_whole_session --method plan_first \
  --voice af_heart --max-cases 8
```

Add `--execute` only when ready to start local inference. Choose High for the installed Ministral 14B writer, Standard for Qwen3 4B, or Lite for the experimental Qwen3 1.7B. The frozen artifact registry and CLI retain responsibility for exact pinned writer resolution; explicit profiles are never silently changed by this harness. Lite remains an experiment, not a passing product claim. Basic supports an authored `script` string in a custom typed case and refuses prompt drafting. The available writing methods are `plan_first`, `blueprint_first_sparse`, `three_candidate_structural_selection`, and `recipe_whole_session`; Kokoro voices are `af_heart`, `af_bella`, `af_nicole`, and `am_michael`.

`--tts-model` also accepts native `qwen3-custom-voice-1.7b` and `qwen3-voice-design-1.7b`, plus the existing `mlx-qwen3-custom-voice-1.7b-8bit` and `mlx-qwen3-voice-design-1.7b-8bit` backends. For CustomVoice, `--qwen-speaker` selects Ryan, Aiden, or Serena (default Serena). For VoiceDesign, `--qwen-voice-design-preset` selects `warm_meditation`, `grounded_meditation`, or `sleep_meditation` (default `warm_meditation`). The selected backend receives only its matching voice control, and the queue records the actual speaker/preset identity. `--voice`, `--qwen-speaker`, and `--qwen-voice-design-preset` are mutually exclusive; controls for a different backend fail planning. Kokoro numeric speed must be between 0.5 and 1.2; Numeric speed for all Qwen backends is refused at planning. MLX requires its installed local runtime and supported host. These options do not install or download optional runtimes.

## Resource and evidence boundaries

Cases execute serially with `--parallel-sections 1`, isolated run/cache roots, and no retry overwrite. A separate nonblocking `/tmp/whoopy-generation-campaign-owner.lock` spans the campaign. Kokoro and native Qwen cases retain the shared `/tmp/whoopy-benchmark-gpu.lock` while running. MLX cases only probe GPU idleness and release that probe before launching, because native MLX workers acquire the same GPU lock themselves; retaining it in the campaign parent would deadlock the child. `--owner-lock-file` and `--lock-file` can override these paths and must name different files; an MLX GPU lock override must match the worker's actual lock path. Existing heavyweight runtime ownership and known inference/ASR processes are checked before each case. The runtime's own guards still apply. Run only one inference campaign on the host; cooperative locks cannot exclude arbitrary unrelated programs.

Admission requires available RAM of at least High 16 GiB, Standard 8 GiB, Lite 4 GiB, or Basic 2 GiB. Native Qwen 1.7B requires at least 12 GiB and MLX at least 16 GiB, even with a smaller writer; a higher writer-profile floor still applies. During each case, available RAM below 2 GiB for two seconds, swap growth above 4 GiB, or a case/campaign deadline stops the subprocess tree gracefully, then kills remaining observed descendants. Pressure at process exit also prevents a completed status. Default limits are 1,800 seconds per case and 7,200 seconds per campaign; use positive `--timeout-seconds` and `--total-seconds` values to tighten them. SIGINT/SIGTERM interruption retains available evidence and stops children. A watchdog stop/admission refusal ends the queue; an ordinary nonzero CLI result is retained and permits the next case.

Each attempted case keeps its allocated run UUID, exact command/environment, stdout/stderr, resource samples, exit code, UTC timestamps, real monotonic wall time, and receipt. Failed/interrupted model runs and CLI artifacts remain under `attempts/.../runs`. `manifest.json` records terminal campaign state and each finished attempt. Inspect an interrupted attempt's `receipt.json` as well, since interruption can occur before its receipt is appended to the campaign record. No verification is implied by `completed`; run external text/audio checks and listening review before drawing quality conclusions.

The environment allowlist omits provider tokens and disables Hugging Face/Transformers online lookup, telemetry, pip indexes, and application Keychain integration. This is an offline configuration, not an operating-system network sandbox. The generation command does not install assets; missing local assets fail normally. No authentication or network changes are made.

## No-inference checks

```sh
PYTHONPATH=src /path/to/existing/.venv/bin/python -m pytest tests/test_generation_campaign.py
```

Tests use ordinary short Python child processes and injected resource observations. They do not load model weights, open provider credentials, generate speech, or run ASR.
