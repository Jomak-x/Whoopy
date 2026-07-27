# Phase 3.5: First Real Local Meditation

Phase 3.5 is the bridge between the reliable fixture pipeline and the Phase 4
user interface. It proves that Whoopy can turn an ordinary prompt into a real,
spoken meditation entirely on one laptop.

The target flow is:

```text
prompt
  -> local script model
  -> validated meditation plan and section scripts
  -> canonical SPEECH/SILENCE timeline
  -> local Kokoro speech
  -> Phase 3 cache, checkpoints, retry, assembly, and quality checks
  -> playable narration.wav
```

This is deliberately a vertical slice. Ambience, advanced mastering, background
workers, and the permanent UI remain later work.

## What “Working Locally” Means

After a one-time model installation, the first milestone must:

- run without Docker;
- run without an internet connection;
- keep the prompt, script, timeline, and audio on the laptop;
- choose a safe runtime profile before downloading or loading a model;
- support a Basic path that accepts a pasted script without loading an LLM;
- save every intermediate artifact so the result can be understood and resumed;
- produce human speech rather than the current deterministic fixture tones; and
- fail with a useful explanation when the laptop does not have enough resources.

The first complete command should look like:

```bash
whoopy generate \
  "A gentle three-minute grounding meditation after a stressful day." \
  --minutes 3 \
  --profile auto \
  --voice af_heart
```

The Basic path should also work:

```bash
whoopy generate \
  --script-file my-meditation.txt \
  --profile basic \
  --voice af_heart
```

The exact CLI can evolve during implementation, but those two user journeys are
the acceptance contract.

## Researched Starting Stack

These are pinned integration candidates, not permanent product dependencies.
They sit behind Whoopy ports so later comparisons or replacements do not rewrite
the pipeline.

| Capability | First candidate | Why it is the first candidate |
|---|---|---|
| Script generation | Qwen3-4B Q4_K_M GGUF through llama.cpp | Official quantized artifact, Apache-2.0 license, small enough for the Standard profile, and successfully smoke-tested on the development Mac |
| Speech | Kokoro v1.0 through sherpa-onnx | Small 82M model, Apache-2.0 license, native wheels across the target operating systems, and 24 kHz mono output that matches Whoopy's current renderer |
| Deterministic assembly | Whoopy's existing renderer | Already owns exact silence, joins, manifests, digests, and integrity checks |

Official references:

- [llama.cpp](https://github.com/ggml-org/llama.cpp) documents native GGUF
  inference and CPU, Metal, CUDA, HIP, Vulkan, and SYCL backends.
- [Qwen3](https://qwenlm.github.io/blog/qwen3/) documents the dense model sizes,
  Apache-2.0 release, and local llama.cpp support.
- [Qwen3-4B-GGUF](https://huggingface.co/Qwen/Qwen3-4B-GGUF) is Qwen's official
  quantized repository.
- [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) documents the model's
  size and Apache-2.0 license.
- [sherpa-onnx Kokoro](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html)
  documents the v1.0 bundle, speaker IDs, and 24 kHz output.
- [sherpa-onnx installation](https://k2-fsa.github.io/sherpa/onnx/tts/faq.html)
  documents prebuilt Python wheels for Windows, macOS, and Linux.

### Initial Reproducible Artifact Set

The model installer PR must move these values into a machine-readable lock
manifest. They are recorded here first so implementation does not depend on a
mutable “latest” download.

| Artifact | Pin | Download size | SHA-256 |
|---|---|---:|---|
| Qwen3-4B Q4_K_M | Hugging Face revision `bc640142c66e1fdd12af0bd68f40445458f3869b`, file `Qwen3-4B-Q4_K_M.gguf` | 2,497,280,256 bytes | `7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5` |
| llama.cpp macOS arm64 | release `b10142`, archive `llama-b10142-bin-macos-arm64.tar.gz` | 10,877,713 bytes | `496696a75da480b80c4cc26c112e00f55e99567c386bd6cf51a8d914ae68373f` |
| Kokoro multilingual v1.0 | archive `kokoro-multi-lang-v1_0.tar.bz2` | 349,418,188 bytes | `c133d26353d776da730870dac7da07dbfc9a5e3bc80cc5e8e83ab6e823be7046` |
| sherpa-onnx | Python package and native core version `1.13.4` | platform-dependent | wheel hashes must be locked separately for every supported platform |

The first voice is `af_heart`, speaker ID `3` in the Kokoro v1.0 bundle.
Voice choice, speaking speed, model revision, runtime version, and normalization
settings must all become part of the Phase 3 speech-cache identity.

### Why These Choices Are Provisional

“Replaceable” does not mean “chosen casually.” It means Whoopy keeps model
loading, prompting, sampling quirks, voice controls, and runtime errors inside
adapters that implement stable typed ports.

Qwen3-4B is the initial Standard candidate because an official GGUF exists and
the complete native path has already been demonstrated. A newer model should
replace it only after it:

1. has a trustworthy, reproducibly pinned artifact;
2. has compatible redistribution and output-use terms;
3. fits the same hardware profile safely;
4. passes the same structured-output contract; and
5. wins a documented meditation-quality and performance comparison.

Qwen3.5 is worth evaluating, but its official repository currently publishes
the original model weights rather than an official Qwen GGUF. Depending on an
unreviewed third-party conversion would add avoidable provenance risk to the
first integration. Phase 3.5 therefore starts with Qwen3-4B and keeps a bake-off
gate before declaring any default permanent.

Qwen3-1.7B remains the Lite-profile candidate. Basic mode intentionally uses no
LLM at all. Neither profile should silently switch to a remote service.

## Step-By-Step PR Plan

Each numbered step below is one pull request. Merge them in order. Every PR
keeps fixture tests fast and keeps all large artifacts outside Git.

### Phase 3.5 PR 1: Add The Offline Artifact Manager

Status: implemented on the `phase-3-5-offline-artifact-manager` branch.

Goal: make native runtimes and models reproducible, verifiable, and easy to
prepare before travel.

Changes:

- define a typed artifact-lock manifest with immutable versions, URLs, sizes,
  SHA-256 digests, licenses, platforms, and architectures;
- add `whoopy models list`, `whoopy models doctor`, and
  `whoopy models install`;
- run the existing live-resource and disk checks before a download;
- use resumable downloads and atomic final placement;
- verify size and digest before extraction or loading;
- report exactly what is already installed and what still needs the internet;
- support an offline wheel directory for native Python dependencies; and
- keep weights, archives, extracted runtimes, and wheels under ignored
  machine-local storage.

Acceptance criteria:

- an interrupted download can resume;
- a changed or corrupt artifact is rejected;
- `models doctor` never loads a model;
- `models install --profile standard` prepares the complete first stack;
- a second install is an idempotent no-op; and
- CI uses tiny fake artifacts and never downloads real weights.

Out of scope: generating text or speech.

### Phase 3.5 PR 2: Add Stable Runtime Ports And Native Adapters

Status: implemented on the `phase-3-5-runtime-adapters` stacked branch. See
[`phase-3-5-runtime-adapters.md`](./phase-3-5-runtime-adapters.md).

Goal: prevent concrete model code from leaking into the worker.

Changes:

- add typed `ScriptGenerator` and production `SpeechSynthesizer` protocols;
- add shared adapter metadata containing model revision, runtime version,
  license, device, and generation settings;
- add `TransientError`, `FatalError`, and invalid-output errors;
- move the fixture synthesizer behind the same speech contract;
- add a llama.cpp process adapter with bounded timeouts and captured diagnostics;
- add a sherpa-onnx Kokoro adapter with explicit voice and speed controls; and
- add contract tests that every adapter must pass.

Acceptance criteria:

- pipeline code depends only on ports;
- fixture behavior remains unchanged;
- no model is loaded by importing a module or listing adapters;
- changing a configured adapter does not change domain or renderer code; and
- runtime crashes become readable, classified Whoopy errors.

Out of scope: meditation prompting and the final one-command workflow.

### Phase 3.5 PR 3: Produce Real Speech From A Pasted Script

Goal: make the first useful meditation possible without waiting for LLM
integration.

Changes:

- accept a text or Markdown script file;
- compile paragraphs and explicit pause markers into the canonical timeline;
- synthesize every `SPEECH` segment with Kokoro;
- trim only unintended TTS edge silence while preserving a documented residual;
- convert adapter output to Whoopy's mono 24 kHz 16-bit PCM contract;
- include the real model, voice, speed, and processing settings in cache keys;
- reuse Phase 3 checkpoints, retries, assembly, and quality checks; and
- save the source script, timeline, narration, manifest, and quality report.

Acceptance criteria:

- a user can listen to a real locally spoken meditation;
- explicit pauses remain sample-accurate;
- a repeated run reuses verified speech cache entries;
- one failed segment resumes without resynthesizing healthy segments; and
- Basic mode works without downloading or loading an LLM.

Out of scope: generating the script from a prompt.

### Phase 3.5 PR 4: Generate A Validated Meditation Locally

Goal: turn a short user prompt into a safe, inspectable canonical timeline.

Changes:

- define versioned prompts and typed schemas for the meditation plan and
  section drafts;
- have the LLM create a small plan before drafting prose;
- draft bounded sections, using limited parallelism only after the shared plan
  is valid;
- validate structured output before it enters the domain;
- retry malformed output a bounded number of times;
- enforce requested duration with pacing budgets and timeline calculations;
- apply content rules for non-medical framing and unsafe breath instructions;
- save raw model output separately for debugging, while treating only validated
  artifacts as pipeline inputs; and
- record the model revision, prompt version, seed, context, sampling settings,
  device, and measured throughput.

Acceptance criteria:

- a prompt produces a valid plan, script, and canonical timeline offline;
- arbitrary model text cannot bypass schema validation;
- a failed section can be retried without rewriting completed valid sections;
- output remains within the documented duration tolerance; and
- fixture generators cover all logic in normal CI.

Out of scope: a permanent model winner or an advanced editor.

### Phase 3.5 PR 5: Join The Real End-To-End Flow

Goal: provide one understandable command for the first generated meditation.

Changes:

- implement the target `whoopy generate` prompt and `--script-file` flows;
- select Basic, Lite, or Standard through `auto` and display that choice;
- show stage, segment, cache, retry, and estimated-progress information;
- preserve every inspectable run artifact;
- add cancellation that leaves the run resumable;
- add a local-only end-to-end test marker for installed real models; and
- document the exact offline demo and troubleshooting sequence.

Acceptance criteria:

- one command creates a real spoken meditation from a prompt;
- the same installed stack completes with networking disabled;
- cancelling and resuming does not restart completed speech segments;
- a weak machine selects Basic or refuses before an unsafe load;
- the final output passes all existing audio-quality gates; and
- no large runtime artifact appears in Git.

Out of scope: the Phase 4 web UI, ambience, public sharing, and background queue.

### Phase 3.5 PR 6: Run The Default-Model Bake-Off

Goal: distinguish a working first model from the best supported default.

Changes:

- create a small, versioned evaluation set covering sleep, grounding, anxiety,
  body scan, breath awareness, and different durations;
- score structure validity, instruction safety, repetition, tone, timing fit,
  generation speed, peak memory, and artifact size;
- compare at least the Lite and Standard candidates;
- add a blind listening rubric for voice naturalness and pacing;
- publish measured results without committing generated weights or audio; and
- update profile defaults only when the evidence supports the change.

Acceptance criteria:

- the default is justified by recorded measurements and human review;
- a lower-resource fallback is documented;
- model replacement requires adapter-contract tests rather than pipeline edits;
- licenses and artifact provenance are reviewed; and
- failures and tradeoffs are recorded, not hidden behind one total score.

Out of scope: making every experimental model an officially supported adapter.

## Required Run Artifacts

The first real run should remain understandable without reading logs:

```text
runs/<uuid>/
  run.json
  input.json
  resolved-config.json
  model-metadata.json
  plan.json
  script.md
  timeline.json
  raw-model-output/
  segments/
  narration.wav
  audio-manifest.json
  quality.json
```

Large model files are shared machine assets, never copied into each run.

## Phase Completion Gate

Phase 3.5 is complete only when:

- a clean supported laptop can install a verified stack through Whoopy;
- one prompt creates a three- to five-minute spoken meditation locally;
- the same flow works offline after installation;
- the Basic pasted-script path works without an LLM;
- all generated decisions are inspectable in run artifacts;
- real speech uses Phase 3 caching, retry, resume, and integrity checks;
- model and voice replacements stay behind typed adapters;
- resource checks choose a safe profile or give a clear refusal; and
- the user can listen to the result before any Phase 4 UI work begins.
