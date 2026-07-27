# Phase 3: Quality, Caching, And Recovery

Phase 3 changes Whoopy from “a run either finishes or starts over” into a
pipeline that can preserve verified work.

No language model or voice model is introduced here. Speech is still the
deterministic fixture tone. That is deliberate: cache correctness, retry rules,
checkpoint recovery, and audio-integrity checks can be proven quickly without a
large download or nondeterministic model.

## The Main Idea

A meditation contains several independent speech segments. Whoopy now treats
each speech segment as a small recoverable job:

```text
timeline speech segment
        |
        v
calculate content-addressed cache key
        |
        +--> verified run checkpoint? ---- yes --> reuse it
        |
        +--> verified global cache entry? - yes --> copy into run checkpoint
        |
        +--> synthesize with retry
                  |
                  v
          validate -> checkpoint -> cache
        |
        v
assemble all segments -> validate final WAV
```

There are two reuse layers because they solve different problems:

- The **content-addressed cache** shares identical synthesized speech between
  different runs.
- A **run checkpoint** preserves the exact completed work belonging to one run,
  so that run can resume after failure or interruption.

## Vocabulary From The Beginning

### Cache

A cache stores a result that can be recomputed. It makes repeated work faster,
but it must never be the only copy of irreplaceable user data.

### Content-addressed

Most files are addressed by a human name such as `speech-0001`. A
content-addressed entry is addressed by a hash of every input that can affect
the output.

For the fixture synthesizer, Whoopy hashes canonical JSON containing:

- normalized text;
- segment type;
- synthesizer identity and version;
- sample rate;
- PCM format.

The result is a 64-character SHA-256 digest:

```text
2f4a...<60 more hexadecimal characters>
```

Identical synthesis inputs produce the same key. Changing the text or
synthesizer identity produces a different key.

Future voice, speed, delivery mode, seed, and model-version fields must be
added to this canonical input before they affect synthesis. Otherwise Whoopy
could incorrectly reuse audio made with old settings.

### Checkpoint

A checkpoint is a durable progress record. Each speech segment stores:

- its status: `running`, `completed`, or `failed`;
- its cache key;
- total synthesis attempts;
- whether its completed bytes came from the global cache;
- PCM sample rate, frame count, byte count, and SHA-256 digest;
- start, update, and completion times;
- every retained retry failure and its classification.

### Atomic write

Whoopy writes a temporary file beside the destination and then replaces the
destination. A reader therefore sees either the previous complete file or the
new complete file, not half-written JSON or PCM.

For a completed cache entry or checkpoint, PCM is written first and metadata is
written last. The metadata acts as the commit marker.

## Runtime Directory Layout

Phase 3 adds ignored runtime files beneath the run root:

```text
runs/
├── .cache/
│   └── segments/
│       └── 2f/
│           └── 2f4a.../
│               ├── audio.pcm
│               └── metadata.json
└── <run-id>/
    ├── run.json
    ├── timeline.json
    ├── segments/
    │   ├── speech-0001/
    │   │   ├── audio.pcm
    │   │   └── checkpoint.json
    │   └── speech-0002/
    │       ├── audio.pcm
    │       └── checkpoint.json
    ├── narration.wav
    ├── audio-manifest.json
    └── quality.json
```

The first two characters of a cache key form a shard directory. Sharding keeps
one directory from eventually containing thousands of entries.

`audio.pcm` contains raw mono 16-bit little-endian samples. It has no WAV
header. `narration.wav` is the final playable container.

Everything under `runs/` is excluded from Git.

## Cache Lookup And Corruption

A file existing at the expected path does not make it trustworthy. A cache hit
must pass all of these checks:

1. metadata is valid JSON matching the schema;
2. the metadata key matches the directory key;
3. the synthesis-input digest matches that key;
4. synthesizer identity and sample rate match the current adapter;
5. PCM byte count and SHA-256 match metadata;
6. byte length is valid for 16-bit PCM;
7. frame count matches metadata;
8. the speech PCM is nonempty, audible, and below the headroom limit.

If any check fails, the entry becomes a cache miss. Whoopy synthesizes the
segment again and atomically replaces the corrupt entry. It never uses
questionable bytes merely because they are already on disk.

Inspect the cache without modifying it:

```bash
uv run whoopy cache stats
uv run whoopy cache stats --json
```

Phase 3 deliberately has no automatic pruning. Deletion needs a separately
reviewed, safely scoped command.

## Retry Rules

Not every failure deserves the same response.

### Transient error

A transient error may disappear when attempted again. Examples include a
temporarily unavailable subprocess or accelerator.

The default policy allows three total attempts for one invocation. Delays use
bounded exponential backoff:

```text
attempt 1 fails -> wait 0.25 seconds
attempt 2 fails -> wait 0.50 seconds
attempt 3 fails -> stop and mark the segment failed
```

The maximum delay is capped at two seconds in this local fixture phase.

### Quality error

Output that is silent, malformed, uses the wrong sample rate, or violates peak
headroom is treated as retryable. A later synthesis attempt may produce healthy
bytes.

### Fatal error

A fatal error is deterministic, such as an unsupported voice. Retrying the same
request would waste time, so Whoopy records the reason and stops immediately.

### Unexpected error

An unclassified programming or adapter error is not retried automatically.
Silently retrying unknown defects can hide bugs.

Real TTS adapters will implement the same synthesis protocol and raise the same
classified error types. The worker does not need model-specific retry logic.

## Resume Behavior

Start a normal run:

```bash
uv run whoopy run create "A short grounding meditation."
uv run whoopy worker process <run-id>
```

Resume a failed run:

```bash
uv run whoopy run resume <run-id>
```

The same command can recover a run left in `running` after a process crash or
laptop interruption.

Resume follows these rules:

1. Load the already-saved canonical timeline. Do not silently rebuild it.
2. Revalidate every completed per-run speech checkpoint.
3. Reuse healthy checkpoints without invoking synthesis.
4. Start again at the first missing, failed, changed, or corrupt segment.
5. Reassemble and revalidate the complete WAV.
6. Mark the run complete only after every final artifact is safely written.

If timeline creation itself failed, no timeline exists yet, so resume is
allowed to retry that initial creation step.

`run.json` schema version 3 records:

- processing invocations;
- resume count;
- global cache hits and misses;
- per-run checkpoint reuses;
- total and completed speech segments;
- the currently failed segment ID.

This makes recovery visible instead of hiding it in logs.

## Stronger Audio Quality Gate

Phase 2 already checked format, duration, contiguous joins, exact digital
silence, audible speech markers, and full-scale clipping. Phase 3 adds:

- exact requested-silence frame calculations;
- SHA-256 for each segment's PCM;
- SHA-256 for the assembled PCM stream;
- boundary-discontinuity measurement;
- a `-1.0 dBFS` peak-headroom limit.

The headroom check catches dangerously hot samples before they reach the exact
integer clipping value. A regression test writes a `32,000` sample—below the
full-scale `32,767` clipping value—and proves that the headroom gate still
rejects it.

Another regression test changes the requested pause duration without changing
its frames and proves that the timing gate rejects the mismatch.

The manifest is now schema version 2. It records cache keys and PCM digests for
speech spans so quality results can be traced to exact segment bytes.

## What Repeated Runs Do

Suppose the fixture timeline contains:

```text
speech-0001: the user's prompt
silence-0001: 1,500 ms
speech-0002: fixed completion sentence
```

The first render has two cache misses and synthesizes both speech segments.

Running the identical prompt again has two cache hits and performs no speech
synthesis.

Changing only the prompt causes:

- one miss for `speech-0001`;
- one hit for the unchanged fixed `speech-0002`.

Silence is cheap and deterministic, so it is generated directly rather than
stored in the synthesis cache.

## Testing

Run the same complete gate used by CI:

```bash
uv run --extra dev python scripts/check.py
```

Phase 3 tests cover:

- identical-run cache hits;
- changed-input cache misses;
- corrupt cache regeneration;
- transient retry with recorded history;
- fatal failure without retry;
- failed-segment resume;
- recovery from an interrupted `running` process;
- timing regression detection;
- pre-clipping peak regression detection;
- Phase 1 and Phase 2 run-record compatibility.

Tests always use temporary cache roots. They never depend on a contributor's
real cache.

## Offline Work

Phase 3 adds no dependencies and no model weights. Once this succeeds:

```bash
uv sync --extra dev --locked
uv run --offline --extra dev python scripts/check.py
```

the existing Python environment and `uv` cache are enough to develop and test
this phase without internet access.

Do not download a speculative model for Phase 3. Exact LLM and TTS artifacts
will be pinned only after adapter contracts, license checks, hardware profiles,
and quality bakeoffs exist. Large weights are replaceable runtime data, not
source files.

## Code Map

- `audio/synthesis.py` — replaceable speech protocol, cache-key inputs, and
  transient/fatal error taxonomy;
- `pipeline/cache.py` — shared content-addressed cache and integrity metadata;
- `pipeline/checkpoints.py` — per-run segment state and verified PCM;
- `pipeline/worker.py` — retry, resume, checkpoint reuse, and cache orchestration;
- `audio/quality.py` — per-segment and whole-WAV integrity checks;
- `audio/renderer.py` — assembly of prepared speech and exact silence;
- `pipeline/runs.py` — schema-v3 recovery counters;
- `cli.py` — `run resume` and `cache stats`.

## Deliberate Limits

Phase 3 still has:

- one foreground worker, with no concurrent leases or background queue;
- fixture tones instead of human speech;
- in-memory final WAV assembly;
- no manual cache-prune command;
- no user command for regenerating an arbitrary healthy segment;
- no database transaction spanning every artifact;
- no LLM, TTS model, FFmpeg, API, or permanent frontend.

Those limits keep this PR focused on proving cache and recovery correctness.
