# Phase 3.5 PR 5: One Offline Command

Whoopy can now create a real spoken meditation from either a short prompt or an
authored script. Both paths end at the same saved script, canonical timeline,
Kokoro speech adapter, cache, checkpoints, deterministic renderer, and quality
gate.

## Generate From A Prompt

After the Standard artifacts are installed:

```bash
uv run --offline whoopy generate \
  "A gentle three-minute grounding meditation after a stressful day." \
  --minutes 3 \
  --profile auto
```

The command shows three stages on standard error:

1. local drafting, including its resumable UUID;
2. schema and safety validation; and
3. speech synthesis, assembly, and quality checks.

Its final standard output identifies every durable artifact. `--json` suppresses
human progress and emits only the machine-readable run record for the future UI.

## Generate From Your Own Script

Basic mode skips the language model:

```bash
uv run --offline whoopy generate \
  --script-file examples/first-meditation.md
```

This is still the right path for a weak laptop, an exact authored script, or a
person who does not want generative writing. The two modes share audio
behavior; prompt mode does not get a privileged shortcut around validation.

Passing both a prompt and `--script-file`, or passing neither, is an error.

## The Complete Boundary

```text
prompt mode                         script-file mode
-----------                         ----------------
Qwen plan JSON                      authored UTF-8/Markdown
  -> strict validation                 |
deterministic budgets                  |
  -> section JSON                       |
  -> strict validation                  |
  -> saved script.md <------------------+
          |
canonical SPEECH/SILENCE timeline
          |
replaceable Kokoro adapter
          |
trim/fade/normalization
          |
verified cache + run checkpoints
          |
deterministic WAV assembly
          |
read-back quality gate
```

This is why the model remains replaceable. The language model cannot write
audio or bypass the timeline. The speech model cannot decide pauses or run
state. Each adapter has a small typed job.

## Schema-v5 Generated Runs

A prompt run contains:

```text
runs/<UUID>/
├── run.json
├── plan.json
├── raw-model-output/
├── draft-sections/
├── script.md
├── resolved-config.json
├── model-metadata.json
├── timeline.json
├── segments/
├── narration.wav
├── audio-manifest.json
└── quality.json
```

Schema v5 distinguishes `generated_prompt` from schema-v4 `script_file` runs.
It requires all plan, model-output, validated-section, configuration, and model
metadata references. Older schema-v1 through schema-v4 records remain readable.

`model-metadata.json` records both the LLM and TTS adapter identities.
`resolved-config.json` records prompt versions, duration, seed, section
parallelism, preflight estimate, platform, profile, voice, speed, and audio
processing settings. A reviewer can therefore explain which code and model
choices produced the sound.

## Cancellation And Recovery

The resumable UUID is printed before the first model call. Pressing Ctrl-C exits
with code 130 and leaves safe completed work in place.

- If drafting was interrupted before `run.json` exists, repeat the same command
  with `--draft-id UUID`. Valid plan and section checkpoints are reused.
- If audio work had created `run.json`, use
  `whoopy run resume UUID --models-dir models/managed`. Valid speech
  checkpoints and shared cache entries are revalidated and reused.

The command never marks an interrupted run completed. It never assumes a file
is healthy merely because it exists.

## Real Offline Evidence

The development Mac test ran Qwen3-4B and Kokoro with networking disabled. For
a requested one-minute grounding meditation:

- Qwen produced four validated sections;
- Kokoro synthesized all four speech segments;
- the canonical timeline contained eight speech/silence segments;
- the preflight duration estimate was 65.64 seconds;
- the actual WAV duration was 57.62 seconds, 2.38 seconds under the request;
- every format, timing, silence, audibility, digest, boundary, headroom, and
  clipping check passed; and
- the run preserved the plan, raw output, section drafts, script, timeline,
  configuration, model metadata, checkpoints, manifest, WAV, and report.

Real-model tests are manual/local because CI must stay fast and cannot download
multi-gigabyte artifacts. Normal CI replaces only the adapter implementations;
it exercises the same schemas, pipeline, storage, cache, worker, and renderer.

## Still Intentionally Missing

The permanent browser UI is Phase 4. This PR does not add ambience, music,
advanced mastering, a background queue, cloud fallback, or public sharing.
PR 6 evaluates model and voice defaults; “it works” does not automatically mean
“it is the best supported default.”
