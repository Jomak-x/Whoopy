# Phase 3.5 PR 3: Real Speech From A Script

This PR is Whoopy's first genuinely useful audio path. A person writes or
pastes a meditation, marks deliberate pauses, and Whoopy produces locally
spoken narration with Kokoro. No language model and no network connection are
needed after the Basic artifacts have been installed.

## Try It

Install the verified Basic stack once while online:

```bash
uv run whoopy models install --profile basic
```

Then render the included example, even while offline:

```bash
uv run --offline whoopy generate \
  --script-file examples/first-meditation.md
```

The command prints a UUID and the paths beneath `runs/<UUID>/`. Play
`narration.wav` with the normal audio player on your laptop.

## The Script Format

The input is ordinary UTF-8 text or Markdown. Each prose paragraph becomes one
or more `SPEECH` segments. A marker on its own line creates exact silence:

```text
Settle into a comfortable position.

[pause: 2.5s]

Notice the support beneath you.
```

Seconds (`s`) and milliseconds (`ms`) are accepted. Adjacent pause markers are
combined. Pauses cannot exceed ten minutes, a script cannot create more than
500 timeline segments, and very long paragraphs are split at sentence
boundaries. Markdown headings, front matter, and fenced code do not become
spoken words. The compiler rejects an empty or malformed script before it
creates a durable run.

The pause is not punctuation sent to Kokoro. It becomes a canonical `SILENCE`
segment with an exact frame count. At 24,000 samples per second, a 2.5-second
pause is exactly 60,000 zero-valued samples. This is why pause timing is stable
across computers and repeated renders.

## What Happens During Generation

```text
script.md
  -> Markdown compiler
  -> timeline.json (SPEECH and SILENCE)
  -> Kokoro adapter (speech only)
  -> edge trim, fade, and peak normalization
  -> verified segment cache/checkpoints
  -> deterministic renderer
  -> narration.wav + manifest + quality report
```

Kokoro may place extra quiet samples around an utterance. Whoopy removes only
silence below the documented threshold, deliberately keeps a 25 ms margin,
applies a short edge fade to avoid clicks, and normalizes the peak to -6 dBFS.
These operations do not alter canonical pause segments.

Every sound-changing value is part of the cache identity: model revision,
runtime, voice, speed, language, raw adapter settings, trimming threshold,
residual margin, fade length, and peak target. A cached segment created under
different settings therefore cannot be mistaken for the current sound.

## Durable Run Files

Schema-v4 runs contain:

```text
runs/<UUID>/
├── run.json
├── script.md
├── resolved-config.json
├── model-metadata.json
├── timeline.json
├── segments/
├── narration.wav
├── audio-manifest.json
└── quality.json
```

- `run.json` records lifecycle state, progress, retry, cache, and resume data.
- `script.md` is the exact source input, not a pointer to a file that may move.
- `resolved-config.json` freezes the profile, platform, voice, and processing
  choices needed to reconstruct the worker.
- `model-metadata.json` records the concrete TTS model, runtime, version,
  license, device, and generation settings.
- `timeline.json` is the validated source of truth for timing.
- `segments/` contains verified per-run speech checkpoints.
- `audio-manifest.json` maps every segment to exact output frames and digests.
- `quality.json` records the read-back integrity checks.

The initial files are written in a temporary sibling directory and atomically
renamed into place. A crash cannot expose a half-created durable run. UUID
validation still prevents a run identifier from escaping the run directory.

## Cache, Failure, And Resume

Speech is expensive; silence is cheap and deterministic. Each completed speech
segment is stored in the shared content-addressed cache and in the run's own
checkpoint directory. A repeated script with the same settings reuses verified
PCM instead of asking Kokoro to speak again.

If generation fails, resume it with:

```bash
uv run --offline whoopy run resume <UUID>
```

Schema-v4 runs do not depend on process memory. Resume reads the saved script,
resolved configuration, and model metadata, reconstructs the Kokoro adapter,
revalidates every checkpoint, and continues at the first missing speech
segment.

## How The Native Dependency Works

The artifact manager verifies the two sherpa-onnx wheels as normal locked
artifacts. This PR safely extracts those wheels into an ignored, isolated
Python directory beneath `models/managed/installed/`. The adapter adds that
specific directory to Python's import search path only when speech is actually
requested. Merely importing Whoopy, asking for help, or inspecting models still
does not load the native library or Kokoro model.

This avoids a machine-specific manual `pip install`. The same artifact lock
resolves the correct wheel for supported macOS, Linux, and Windows machines.

## What This PR Does Not Do

It does not write a meditation from a prompt. That is PR 4. It also does not
add the permanent UI, ambience, mastering, publishing, or a background job
queue. Its narrow job is to make authored text become reliable real speech
through the same replaceable port, cache, recovery, renderer, and quality
boundaries that later generated scripts will use.
