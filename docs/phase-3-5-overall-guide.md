# Phase 3.5 From Zero: How Whoopy Creates A Local Meditation

This is the beginner-friendly explanation of everything Phase 3.5 added. You
should be able to read this without knowing machine learning, audio
programming, web development, or the command line.

Phase 3.5 answers one question:

> Can Whoopy turn an ordinary request into a real, spoken meditation entirely
> on one laptop, while keeping the result inspectable and recoverable?

The answer is now yes. It works from a prompt on a laptop that can safely run
the Standard model. It also works from a script on weaker hardware without
loading a language model.

## Try It First

From the Whoopy repository, run:

```bash
uv run --offline whoopy web --open
```

`uv run` starts the command inside Whoopy's reproducible Python environment.
`--offline` tells `uv` not to contact the internet. `whoopy web` starts a small
server available only on this laptop. `--open` asks the operating system to
open the page in your default browser.

If the browser does not open automatically, visit:

```text
http://127.0.0.1:8765
```

The page has two modes:

- **Describe it** takes a normal request such as “a gentle three-minute
  grounding meditation after a difficult day.” It uses the local Standard
  language model and the local speech model.
- **Use my script** takes your exact words. You can add `[pause: 3s]` on its
  own line for three seconds of exact silence. This uses the Basic profile and
  does not load a language model.

Leave the terminal window open while using the page. Press `Ctrl+C` in that
terminal to stop the local server.

If the readiness card says a download is needed, prepare the models once while
online:

```bash
uv run whoopy models install --profile standard
```

The Standard profile includes everything needed by Basic, so you do not need a
second download.

## The Whole System In One Picture

```text
you
 |
 | prompt: "a three-minute grounding meditation"
 v
local web page or CLI
 |
 v
hardware safety check
 |
 v
Qwen through llama.cpp
 |  1. plan JSON
 |  2. section JSON
 v
Whoopy schema + safety validation
 |
 v
script.md
 |
 v
canonical timeline
 |  SPEECH -> SILENCE -> SPEECH -> SILENCE ...
 v
Kokoro through sherpa-onnx
 |
 v
processed speech segments + exact digital silence
 |
 v
cache + run checkpoints
 |
 v
deterministic WAV renderer
 |
 v
audio integrity checks
 |
 v
runs/<UUID>/narration.wav and supporting evidence
```

Every arrow is a boundary. That matters because a model is never allowed to
control the entire system.

## What A Model Is

A model is a large file containing numbers learned during training. Software
loads those numbers and performs calculations called **inference**.

Whoopy uses two different kinds of models:

1. A **large language model**, or LLM, turns your request into a plan and
   written meditation. The current Standard candidate is Qwen3-4B.
2. A **text-to-speech model**, or TTS model, turns each saved sentence into
   waveform samples. The current candidate is Kokoro.

They do completely different jobs. Qwen does not create audio. Kokoro does not
write or plan the meditation.

The files are not committed to Git because they are large and
machine-specific. They live under the ignored `models/managed/` directory.
Whoopy downloads them once, verifies them, and can then use them offline.

## Are The Models Replaceable?

Yes, by design—but “replaceable” does not mean every replacement will work
well.

Whoopy defines small typed contracts called **ports**:

```text
ScriptGenerator:
  request in -> generated structured text + metadata out

SpeechSynthesizer:
  text + voice settings in -> standardized PCM audio + metadata out
```

An **adapter** connects one concrete runtime to a port. Qwen's adapter starts
`llama.cpp`. Kokoro's adapter uses `sherpa-onnx`.

The rest of Whoopy talks to the port, not directly to Qwen or Kokoro. Therefore
a future model change should mainly require:

1. a new or adjusted adapter;
2. a locked, verified artifact entry;
3. the same contract tests;
4. a hardware-profile review;
5. a model-quality comparison; and
6. configuration that selects the new adapter.

The timeline, run store, cache, renderer, quality checks, and web page should
not need a rewrite.

Replaceability protects the architecture. Evaluation protects the product. A
model should be replaced only when evidence says the new choice is safe,
licensed, reproducible, compatible, and better for Whoopy's actual prompts.

## Why Qwen3-4B Is Still The Current Choice

We did not keep Qwen3-4B only because it happened to run once. Phase 3.5 added a
versioned six-case evaluation covering:

- grounding;
- breath awareness;
- body scan;
- sleep;
- an anxious moment; and
- daytime focus.

The same seed, prompts, validation, and measurements were used for both
candidates.

| Candidate | Strict successes | Model size | Average time per case | Measured peak process memory |
|---|---:|---:|---:|---:|
| Lite: Qwen3-1.7B Q8_0 | 0 / 6 | 1.83 GB | 14.53 s | 2,808 MB |
| Standard: Qwen3-4B Q4_K_M | 5 / 6 | 2.50 GB | 17.61 s | 3,697 MB |

Lite is smaller and faster, but it repeatedly violated the structured contract:
invalid IDs, invalid pauses, invalid weights, and section lengths outside their
budgets. It remains available only for explicit experiments. Whoopy will not
pretend it is a dependable automatic fallback.

Standard also failed one strict case. That failure was visible and bounded
rather than silently accepted. So Standard is the best current supported
candidate, not a permanent winner.

Basic authored-script mode is the dependable low-resource fallback. It skips
the LLM entirely.

The default voice, `af_heart`, is still provisional until a person completes
the blind listening rubric. Automated checks can measure clipping and timing;
they cannot decide which voice feels most comforting.

## PR 1: Verified Offline Artifact Manager

GitHub PR: `#5`

Before Phase 3.5, a setup guide could tell someone to download a model, but
Whoopy could not prove which bytes they had. This PR created the model manager.

### Artifact

An **artifact** is an external file Whoopy needs: a model, a native runtime
archive, or a Python wheel.

### Artifact lock

`config/artifacts.yaml` is a versioned inventory. For every artifact it records:

- a stable Whoopy ID;
- the exact upstream revision and filename;
- the source URL;
- the expected byte size;
- a SHA-256 digest;
- the license;
- the supported operating systems and CPU architectures; and
- which hardware profiles need it.

A `.yaml` file is a human-readable data file. YAML uses indentation to express
structure. For example:

```yaml
profiles:
  standard:
    components:
      - llm_model_standard
      - tts_model
```

This means that `profiles` contains a `standard` entry, and `standard` contains
a list named `components`. Spaces are meaningful in YAML, so consistent
indentation matters.

### SHA-256

SHA-256 is a function that turns any file into a 64-character hexadecimal
fingerprint. The same bytes always create the same fingerprint. Changing even
one byte almost certainly changes it.

Whoopy checks both file size and SHA-256 before trusting a download. A partial,
corrupted, or unexpectedly replaced file is rejected.

### Resumable and atomic downloads

A resumable download can continue after an interruption rather than starting
from zero. Whoopy keeps incomplete data separate from the final artifact.

**Atomic placement** means the verified temporary file is renamed into its
final location in one filesystem operation. Other code sees either no final
file or a complete verified file, not a half-written model.

### Hardware preflight

Before download or model load, Whoopy checks:

- operating system and architecture;
- available and total RAM;
- free disk space;
- CPU count; and
- detected accelerators such as Apple Metal.

It chooses only a profile whose conservative requirements currently fit. If
the laptop is too constrained, Whoopy refuses with an explanation instead of
letting the operating system freeze under an unsafe model load.

### Commands

```bash
whoopy models list
whoopy models doctor
whoopy models install --profile standard
```

`list` shows locked artifacts. `doctor` plans without loading a model.
`install` performs the explicit download and verification.

## PR 2: Stable Ports And Native Adapters

GitHub PR: `#6`

This PR created the boundary between Whoopy's logic and model-specific code.

### Runtime

A **runtime** is the program that performs inference using a model file.
Whoopy's LLM runtime is `llama.cpp`; its speech runtime is `sherpa-onnx`.

`llama.cpp` can use native CPU execution and platform accelerators such as
Metal. Qwen weights use the GGUF file format understood by llama.cpp.

`sherpa-onnx` runs Kokoro using ONNX model files and native wheels across
Windows, macOS, and Linux.

### Native

“Native” means Whoopy runs directly through the laptop's operating system
instead of inside Docker. The locked runtime differs by operating system and
architecture, but the Whoopy command stays the same.

### Typed port

A type describes what kind of value is allowed. A typed port describes the
methods, inputs, outputs, and errors an adapter must provide. Static type
checking catches many boundary mistakes before the program runs.

### Classified errors

Adapters translate confusing process failures into Whoopy errors:

- **transient** means retrying might succeed;
- **fatal** means retrying the same request is not useful; and
- **invalid output** means the model ran, but its answer failed the contract.

This classification is how the worker knows whether to retry, stop, or ask for
recovery.

### Metadata

Every adapter returns metadata identifying the model revision, runtime version,
license, device, and relevant settings. That metadata is saved with each real
run and included in cache identities. Two voices or model revisions must not
accidentally share cached speech.

## PR 3: Real Speech From An Authored Script

GitHub PR: `#7`

This was the first moment Whoopy produced real spoken meditation audio.

### Script syntax

Ordinary paragraphs become speech. A marker such as:

```text
[pause: 3s]
```

becomes exactly three seconds of digital silence.

The script compiler rejects unknown or malformed cues before creating a durable
run. An invalid script does not leave a misleading queued job behind.

### Canonical timeline

“Canonical” means the authoritative version. Whoopy compiles the script into a
timeline such as:

```json
{
  "segments": [
    {"type": "SPEECH", "text": "Welcome to this moment."},
    {"type": "SILENCE", "duration_ms": 3000},
    {"type": "SPEECH", "text": "Let your shoulders soften."}
  ]
}
```

JSON is another structured text format. It is stricter and more repetitive
than YAML, which makes it useful for saved machine artifacts and APIs. Objects
use braces, lists use brackets, property names use quotes, and values have
explicit types.

The speech model does not decide the deliberate pauses. Whoopy renders
`SILENCE` as zero-valued samples, so the duration is deterministic.

### PCM and WAV

Sound inside a computer is a sequence of numeric samples.

Whoopy standardizes speech to:

- one channel, meaning mono;
- 24,000 samples each second;
- signed 16-bit integer samples; and
- little-endian PCM byte order.

**PCM** is the raw sample representation. **WAV** is a container that adds a
header explaining the sample format and then stores the PCM bytes.

Standardizing the format lets Whoopy concatenate speech and silence without
guessing or asking a media tool to reinterpret each segment.

### Speech processing

Generated speech is trimmed only at unintended outer silence. Small fades avoid
clicks at boundaries. Level normalization creates headroom without hiding
clipping. These processing settings become part of the cache key.

## PR 4: Validated Local Meditation Writing

GitHub PR: `#8`

This PR connected a prompt to a plan, bounded sections, a script, and a
timeline—but not yet to the final audio command.

### Why plan first

Asking an LLM for one long meditation gives the program little control over
structure, timing, or partial recovery. Whoopy first asks for a small JSON plan.
The plan states:

- title and intention;
- section IDs and purposes;
- relative section weights; and
- deliberate pauses.

Only after the plan validates does Whoopy calculate each section's word budget
and ask for that section.

### The model is untrusted

“Untrusted” does not mean malicious. It means the program never assumes a model
followed instructions just because it usually does.

Whoopy parses and validates every model answer. It checks:

- JSON structure and allowed fields;
- stable section IDs;
- number and weight boundaries;
- per-section word budgets;
- allowed markup;
- unsafe breath-holding instructions;
- prescriptive emotional claims;
- medical claims and guaranteed outcomes; and
- the preflight duration estimate.

Invalid output receives a bounded repair attempt. If it still fails, Whoopy
stops visibly. It does not silently truncate prose or render invalid text.

### Section checkpoints

Each valid section is saved separately. If section four fails, sections one
through three can be reused on the next attempt.

The CLI currently defaults to one section at a time. `--parallel-sections 2`
allows two, but only on a laptop with enough memory. Parallel LLM work can raise
peak memory sharply, so maximum parallelism is deliberately small rather than
unlimited.

## PR 5: One End-To-End Command

GitHub PR: `#9`

This PR joined prompt generation and real speech:

```bash
whoopy generate "A gentle three-minute grounding meditation." --minutes 3
```

The command performs three visible stages:

1. draft locally;
2. validate and save the plan and script; and
3. synthesize, cache, assemble, and check audio.

Authored script mode reaches the same timeline, speech, cache, renderer, and
quality path. There is no lower-quality “UI shortcut.”

### UUID

A UUID is a 128-bit identifier normally written like:

```text
a378fecd-e496-4fc2-a7b8-3bee77215ede
```

Whoopy creates a fresh UUID for each run. Random UUID version 4 has such a huge
space that accidental duplication is extraordinarily unlikely. More
importantly, Whoopy uses exclusive directory creation. If an ID already
exists, creation fails rather than replacing it.

The UUID lets every artifact, task, resume command, and browser URL refer to
the same run safely. Whoopy parses it as a real UUID before using it in a path,
which prevents path-traversal input such as `../../something`.

### Cancellation and resume

Pressing `Ctrl+C` stops generation without claiming success.

- If interruption occurs during drafting, reuse its ID with `--draft-id`.
- If a durable `run.json` exists, use `whoopy run resume UUID`.

Completed valid work remains on disk. Recovery verifies checkpoints rather
than trusting filenames alone.

## PR 6: Evidence-Driven Model And Voice Evaluation

GitHub PR: `#10`

This PR added:

- the versioned evaluation cases in `config/evaluation/`;
- a runner that applies the same cases to each candidate;
- structure, safety, repetition, timing, speed, memory, and size evidence;
- a permanent result document that records failures as well as successes;
- a four-voice sample preparation command; and
- a blind human listening rubric.

The final real three-minute calibration requested 180 seconds, estimated
180.57 seconds, rendered 180.37 seconds, and passed every implemented audio
integrity check.

The bake-off also exposed a duration allocator edge case for several short
sections. The allocator now assigns every section a hard minimum first, then
distributes the remaining words using the largest-remainder method. A
regression test keeps that bug fixed.

## The Local Web Tester

The first tester is included in the follow-up PR after the six Phase 3.5
changes. Its job is to let you use the real system before the complete Phase 4
product is designed.

### Why it is small

It uses only Python's standard-library HTTP server plus HTML, CSS, and
JavaScript. There is no Node download and no separate frontend build step.
That makes it easy to run on another laptop after `uv sync`.

The browser page is a control and inspection surface. It does not contain
generation logic.

### Local server

A **server** is a program that waits for requests and sends responses. This
server binds to `127.0.0.1`, the loopback address. Loopback means “this
computer.” It is not listening on the Wi-Fi or public internet interface.

The terminal process serves:

- the HTML page;
- its CSS visual styles;
- its JavaScript behavior;
- a small JSON API;
- saved run artifacts; and
- completed WAV files.

### HTML, CSS, and JavaScript

- **HTML** describes the page's meaning and structure: heading, textarea,
  button, audio player, and dialog.
- **CSS** describes presentation: spacing, color, layout, responsive behavior,
  focus states, and animation.
- **JavaScript** responds to clicks, calls the local API, polls task status,
  refreshes run history, and loads artifacts into the inspector.

The interface remains usable on narrow screens, includes keyboard focus
styles, respects reduced-motion settings, and uses semantic controls.

### API

An API is a documented way for programs to communicate. The tester exposes:

```text
GET  /api/health
GET  /api/status
GET  /api/runs
POST /api/generate
GET  /api/tasks/<UUID>
POST /api/tasks/<UUID>/cancel
GET  /api/runs/<UUID>/audio
GET  /api/runs/<UUID>/artifact/<allowed-name>
```

`GET` asks for data without changing it. `POST` submits an action.

The page sends JSON to `/api/generate`. The server validates the text, voice,
speed, duration, and mode. It then starts the existing `whoopy generate`
command as a child process using an argument list, not a shell string. This
avoids shell injection and ensures the web page uses the same model checks,
validation, caching, renderer, and quality gates as the CLI.

### Why task state and run state are separate

A browser **task** is temporary in-memory state used to show “queued,”
“running,” “completed,” “failed,” or “cancelled.” Restarting the server clears
that temporary task list.

A Whoopy **run** is durable state under `runs/<UUID>/`. Completed and failed
runs survive a browser refresh or server restart. The recent-meditations list
is rebuilt from those validated run records.

### Local security boundaries

The tester:

- binds only to loopback;
- accepts state-changing browser requests only from localhost origins;
- requires JSON for those requests;
- limits request size;
- parses UUIDs before constructing run paths;
- serves only a small allow-list of artifact names;
- launches subprocesses without a shell; and
- never accepts an arbitrary filesystem path from the browser.

This is a private development interface, not an internet-hardened public web
service. Do not expose its port with a tunnel or router rule.

## What Is Saved For Every Real Run

Prompt runs use schema version 5:

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

Script runs use schema version 4 and omit LLM-only plan and draft artifacts.

### `run.json`

The lifecycle record: UUID, status, prompt/source, timestamps, artifact
references, recovery counts, and any final error.

### `plan.json`

The validated structure produced before prose. Raw model output is not used as
a plan until it passes the schema.

### `raw-model-output/`

The original plan and repair attempts. These exist for debugging and evidence,
not as trusted inputs.

### `draft-sections/`

The individually validated sections. They are the drafting checkpoints.

### `script.md`

The complete human-readable meditation plus explicit pause markers.

### `resolved-config.json`

The exact profile, platform, voice, speed, processing settings, prompt versions,
seed, parallelism, requested duration, and duration estimate used for this run.

### `model-metadata.json`

The exact LLM and TTS adapters, model revisions, runtimes, licenses, and
devices.

### `timeline.json`

The authoritative ordered speech and silence segments.

### `segments/`

Verified per-run speech checkpoints. These make partial audio recovery
possible.

### `narration.wav`

The playable final mono PCM WAV.

### `audio-manifest.json`

Every segment's exact start frame, end frame, length, type, and digest, plus
the final stream digest.

### `quality.json`

The result of reading the finished WAV back and checking:

- mono channel count;
- sample width;
- sample rate;
- frame count and duration;
- contiguous segment joins;
- exact requested silence frames;
- zero-valued silence;
- audible speech;
- per-segment hashes;
- whole-stream hash;
- boundary continuity;
- peak headroom; and
- clipping.

Whoopy marks a run completed only after these checks pass.

## Cache, Checkpoints, Retry, And Resume

These Phase 3 mechanisms are now used by the real models.

### Cache

A cache stores reusable work. Whoopy hashes every input that can affect a
speech segment:

- text;
- model and runtime revision;
- voice and speaker ID;
- speed;
- language and provider; and
- speech-processing settings.

Identical inputs produce the same cache key. A repeated render can copy
verified PCM instead of asking Kokoro to speak again.

The cache is content-addressed, meaning the key describes the content rather
than a human filename.

### Checkpoint

A checkpoint is completed work saved inside one run. Even if the shared cache
changes later, a resumable run can revalidate its own completed segments.

### Retry

Transient failures use bounded exponential backoff: wait briefly, retry, and
increase the delay up to a fixed attempt limit. Fatal errors stop immediately.
Bounded means Whoopy never retries forever.

### Resume

Resume loads the durable record, validates existing checkpoints and cache
entries, completes missing speech, rebuilds the final WAV, and reruns every
quality check.

## What “Deterministic” Does And Does Not Mean

Whoopy's assembly is deterministic:

- a deliberate pause maps to an exact integer number of audio frames;
- segment order is fixed by the timeline;
- joins and file format are reproducible; and
- digests prove the rendered PCM matches the manifest.

Model generation is not guaranteed to be perfectly identical across every
runtime, device, or future version, even with the same seed. That is why Whoopy
saves model revision, runtime, prompt version, seed, and raw attempts.

## What Works Now

You can now:

- verify what this laptop can safely run;
- install pinned model/runtime artifacts once;
- work fully offline afterward;
- generate a meditation from a plain-language prompt;
- render your own exact script without an LLM;
- choose one of four reviewed Kokoro voices;
- choose a constrained speech pace;
- use exact pause markers;
- inspect every important intermediate artifact;
- listen from the browser;
- repeat work using the speech cache;
- recover interrupted or partially failed audio;
- run the six-case local model evaluation; and
- prepare anonymous samples for human voice comparison.

## What Is Deliberately Not Finished

The current tester is not the complete Phase 4 product. Still missing:

- a persistent background job queue;
- live segment-by-segment progress events;
- editing a generated plan or script before rendering;
- ambience and music;
- advanced loudness mastering and delivery formats;
- permanent voice-selection evidence;
- broad model bake-offs beyond the first two candidates;
- a packaged desktop installer;
- multi-user authentication;
- cloud fallback;
- public sharing or Whoopy Commons; and
- medical or clinical claims.

The web tester's cancellation stops its selected child process. Draft and run
checkpoints remain when the underlying pipeline has already saved them, but the
tester does not yet expose a one-click resume button.

## Common Problems

### The page does not open

Read the terminal for the printed address and open
`http://127.0.0.1:8765` manually.

### The port is already in use

Choose another local port:

```bash
uv run --offline whoopy web --port 8766 --open
```

### “Download needed”

While connected to the internet:

```bash
uv run whoopy models install --profile standard
```

Then start the tester offline again.

### The laptop cannot run Standard

Use **Use my script**. Basic mode loads Kokoro speech but no LLM. If Basic is
also unsafe, Whoopy refuses before model load.

### Generation stops

The browser displays the final CLI error. Inspect `runs/` and
`runs/.generation-workspaces/`. A durable failed run can be resumed from the
CLI:

```bash
uv run --offline whoopy run resume <UUID> --models-dir models/managed
```

### The first generation is slow

The first unique text requires actual model inference. Repeated identical
speech can be faster because valid segments are reused from the cache.

## Vocabulary

**Adapter**  
Model-specific code that implements a stable Whoopy port.

**API**  
A defined interface through which programs exchange requests and responses.

**Artifact**  
An external runtime/model file or a saved output produced by a run.

**Cache**  
Reusable computed work indexed here by a content-derived hash.

**CLI**  
Command-line interface; the `whoopy ...` commands typed in a terminal.

**Configuration**  
Values that select behavior without rewriting program logic.

**Digest or hash**  
A fixed-size fingerprint of bytes used to detect unexpected changes.

**GGUF**  
A model-file format designed for runtimes such as llama.cpp.

**HTML / CSS / JavaScript**  
The browser page's structure, visual presentation, and interactive behavior.

**Inference**  
Running a trained model to create an output.

**JSON**  
A strict structured-text format used for APIs and saved machine artifacts.

**LLM**  
Large language model; Whoopy uses one to plan and write meditation text.

**Localhost / loopback**  
The network address of the same computer, normally `127.0.0.1`.

**Model**  
A learned collection of numerical parameters used by a runtime.

**Native**  
Running directly on the operating system instead of in Docker.

**ONNX**  
A portable model representation used by many inference runtimes.

**PCM / WAV**  
Raw audio sample encoding and a container that describes and carries it.

**Port**  
A stable typed capability contract that adapters implement.

**Process**  
A running program. The local server and each generation command are separate
processes.

**Runtime**  
Software that loads a model and performs inference.

**Schema**  
Rules describing the allowed structure and values of data.

**Server**  
A process that listens for requests and returns responses.

**TTS**  
Text-to-speech; Whoopy uses Kokoro to create speech samples.

**UUID**  
A very-large-space identifier used to keep runs distinct and addressable.

**YAML**  
A human-friendly indentation-based format used for versioned configuration.

## The Most Important Ideas To Remember

1. The model proposes; Whoopy validates.
2. The canonical timeline—not prose or generated audio—is the source of truth.
3. Deliberate silence belongs to deterministic code, not the speech model.
4. Qwen and Kokoro are replaceable adapters, but replacements need evidence.
5. The current Standard model is the best tested candidate, not a permanent
   promise.
6. Basic mode keeps weaker laptops useful without an LLM.
7. Every run is durable, inspectable, and addressed by a validated UUID.
8. Cache and checkpoints avoid throwing away healthy completed work.
9. Completion means the final WAV passed integrity checks.
10. The browser is a local control surface over the real pipeline, not a second
    generation system.
