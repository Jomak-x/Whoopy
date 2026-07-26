# Whoopy Step-by-Step PR Plan

This document turns the Whoopy specification into a sequence of small pull requests. Each numbered step is one PR. Complete and merge them in order unless a PR explicitly says it can run in parallel.

The initial repository is the one documented exception: Milestone 0's PR 1–3 review slices are delivered together in a single Phase 0 foundation PR because there was no committed base to branch them from. The PR keeps those slices visible in its description and verification checklist. Beginning with PR 4, every numbered step is a separate pull request.

The first major objective is not the web application. It is a dependable local CLI that converts a script into a canonical timeline and then into correctly timed audio. Real models, background ambience, the local web UI, and the public Commons platform are layered on only after that foundation works.

## Rules For Every PR

Every PR should:

- solve one bounded problem;
- keep the application runnable;
- include tests for new behavior;
- update relevant documentation;
- avoid unrelated refactors;
- state assumptions and tradeoffs in its description;
- include exact manual verification steps;
- leave generated audio, model weights, caches, secrets, and local configuration out of Git.

Use this PR description template:

```text
## Goal

## Why

## Changes

## Acceptance criteria

## Verification

## Assumptions and tradeoffs

## Out of scope
```

## Milestone 0: Repository Foundation

### PR 1: Add the Python project skeleton

Goal: Create the smallest installable Python project without implementing product logic.

Changes:

- Add `pyproject.toml`, `.python-version`, and a cross-platform `uv.lock` with Python 3.11 support.
- Add the `whoopy` package and an empty `tests/` package.
- Add `.gitignore` entries for virtual environments, Python caches, generated runs, model weights, local databases, and local configuration.
- Add a platform-neutral Python quality script plus optional Unix Make wrappers.
- Add a placeholder CLI that supports `whoopy --help`.

Acceptance criteria:

- `uv` installs the project reproducibly on Windows, macOS, and Linux.
- `whoopy --help` exits successfully.
- the platform-neutral quality script succeeds.
- A new contributor can find the supported Python version in the README.

Verification:

```bash
uv sync --extra dev --locked
uv run whoopy --help
uv run --extra dev python scripts/check.py
```

Out of scope: timeline models, audio, ML dependencies, API, and frontend.

### PR 2: Add configuration loading

Goal: Establish one typed configuration path before product code begins depending on settings.

Changes:

- Add `config/default.yaml`.
- Add `config/models.yaml`.
- Add `config/pacing_profiles.yaml`.
- Add `config/runtime_profiles.yaml`.
- Add `.env.example`.
- Implement layered configuration loading with this precedence:
  `default.yaml` < `local.yaml` < `WHOOPY_*` environment variables < CLI arguments.
- Add tests for defaults, overrides, missing files, and invalid values.
- Add `whoopy doctor` with cross-platform RAM, disk, CPU, and accelerator inspection.
- Select the highest safe Basic, Lite, Standard, High, or Studio profile without loading a model.

Acceptance criteria:

- Default configuration loads without local files.
- Environment variables override YAML values.
- Invalid settings produce a readable error.
- `config/local.yaml` is ignored by Git.
- Weak laptops fall back to Basic without requiring a local LLM.
- Machines below Basic return an actionable refusal before any model load.

Verification:

```bash
uv run whoopy config show
uv run whoopy doctor
uv run --extra dev pytest
```

Out of scope: constructing model adapters from configuration.

### PR 3: Add continuous integration and quality checks

Goal: Make every later PR automatically prove that the basic project remains healthy.

Changes:

- Add linting and formatting configuration.
- Add static type checking.
- Add a CI matrix for Python 3.11 on Windows, macOS, and Linux.
- Run unit tests, linting, formatting checks, and type checks in CI.

Acceptance criteria:

- The workflow passes on a clean checkout.
- A deliberately malformed file would fail the appropriate check.
- Local `scripts/check.py` runs the same essential checks as CI; Make is an optional Unix wrapper.

Verification:

```bash
uv run --extra dev python scripts/check.py
```

Out of scope: real ML runtime tests and large model downloads in CI.

## Milestone 1: Canonical Timeline

### PR 4: Define the timeline segment models

Goal: Introduce the canonical data contract that every later stage will consume.

Changes:

- Add typed models for timeline metadata and timeline segments.
- Initially support `SPEECH` and `SILENCE` segments.
- Give speech segments fields for text, voice, speed, delivery mode, model version, and cache key.
- Give silence segments an exact `duration_ms`.
- Use a discriminated union for segment types.

Acceptance criteria:

- Valid timelines can be constructed and validated.
- Empty speech, invalid speeds, non-positive silence, duplicate IDs, and unknown segment types are rejected.
- Validation errors identify the problematic field.

Verification:

```bash
make test
```

Out of scope: cue parsing and audio rendering.

### PR 5: Add stable timeline serialization and versioning

Goal: Make timelines durable artifacts rather than temporary in-memory objects.

Changes:

- Add JSON serialization and deserialization.
- Add a timeline schema version.
- Export a generated JSON Schema.
- Define deterministic JSON formatting.
- Add fixture timelines under `tests/fixtures/`.

Acceptance criteria:

- A timeline survives a save/load round trip without semantic changes.
- Serialized output is stable for identical input.
- Unsupported future schema versions fail with a clear message.
- The checked-in JSON Schema matches the Python model.

Verification:

```bash
make test
whoopy timeline validate tests/fixtures/minimal_timeline.json
```

Out of scope: migrations between schema versions.

### PR 6: Define the script cue grammar

Goal: Specify the small human-readable language compiled into a timeline.

Changes:

- Document ordinary prose and the initial `[pause Ns]` cue.
- Define accepted integer and decimal duration formats.
- Define whitespace and paragraph behavior.
- Define errors for malformed and unknown cues.
- Add parsing fixtures covering valid and invalid inputs.

Acceptance criteria:

- The grammar is documented with examples.
- Every accepted syntax has a fixture.
- Ambiguous or unsupported syntax has a documented error.

Verification:

```bash
make test
```

Out of scope: breath events, music cues, and flexible pauses.

### PR 7: Implement the deterministic timeline compiler

Goal: Convert prose and explicit pause cues into the canonical timeline without using a model.

Changes:

- Parse the script cue grammar.
- Emit alternating speech and silence segments.
- Assign stable segment IDs.
- Populate timeline metadata.
- Expose `whoopy timeline compile INPUT --output OUTPUT`.

Acceptance criteria:

- Identical input and configuration produce identical output.
- `[pause 3s]` becomes exactly `3000` milliseconds.
- Malformed cues include line and column information in errors.
- Compiler output passes timeline schema validation.

Verification:

```bash
whoopy timeline compile tests/fixtures/sample_script.txt --output /tmp/timeline.json
whoopy timeline validate /tmp/timeline.json
make test
```

Out of scope: converting punctuation into exact silence. Micro-pauses remain inside speech text.

### PR 8: Add advanced timeline primitives

Goal: Complete the initial canonical model with breathing and music control events.

Changes:

- Add `BREATH` segments.
- Add `MUSIC_CUE` events for fade-in, fade-out, and ducking.
- Extend validation and JSON Schema.
- Extend the cue grammar only where an explicit source representation is required.

Acceptance criteria:

- New types serialize and validate through the same timeline interface.
- Existing timeline fixtures remain valid.
- Invalid breath phases and music cue values are rejected.

Verification:

```bash
make test
```

Out of scope: rendering breath visuals or applying music automation.

## Milestone 2: Deterministic Placeholder Audio

### PR 9: Add run directories and artifact manifests

Goal: Give every generation a predictable, inspectable workspace.

Changes:

- Create `runs/<run-id>/` safely.
- Store copied input, resolved configuration, timeline, segment files, logs, and an artifact manifest.
- Use atomic writes for important JSON artifacts.
- Add a run ID abstraction that can be replaced later by database IDs.

Acceptance criteria:

- Two runs never overwrite one another.
- An interrupted write does not leave valid-looking partial JSON.
- Generated run directories remain ignored by Git.
- Tests use temporary directories instead of the real `runs/` directory.

Verification:

```bash
make test
whoopy run create
```

Out of scope: database persistence and job queues.

### PR 10: Add safe FFmpeg and FFprobe execution

Goal: Centralize external audio-process execution and error handling.

Changes:

- Detect `ffmpeg` and `ffprobe` availability.
- Add typed wrappers using argument arrays rather than shell command strings.
- Capture exit status and bounded diagnostic output.
- Add probe helpers for duration, sample rate, channels, codec, and peak information.

Acceptance criteria:

- Missing binaries produce installation guidance.
- Failed processes include the command purpose and useful stderr.
- Paths containing spaces work correctly.
- No user input is interpolated into a shell command.

Verification:

```bash
make test
whoopy doctor
```

Out of scope: generating or combining audio.

### PR 11: Render exact silence segments

Goal: Render timeline silence as deterministic PCM audio.

Changes:

- Add the first renderer implementation for `SILENCE`.
- Choose and document the internal narration sample format.
- Probe every generated silence file.
- Add duration-tolerance tests.

Acceptance criteria:

- A 3,000 ms silence probes to the expected duration within one audio-frame tolerance.
- Output sample rate, channel layout, and PCM format are consistent.
- Invalid durations fail before FFmpeg is launched.

Verification:

```bash
make test
whoopy audio silence --milliseconds 3000 --output /tmp/silence.wav
ffprobe /tmp/silence.wav
```

Out of scope: speech and concatenation.

### PR 12: Add the fixture speech synthesizer

Goal: Make the full pipeline testable without models, downloads, or network access.

Changes:

- Define a fixture synthesizer that renders an identifiable tone or bundled speech fixture.
- Make duration deterministic from fixture configuration.
- Record adapter name, version, and license metadata.
- Add contract tests for output existence and audio format.

Acceptance criteria:

- Every speech segment produces valid audio.
- Automated tests do not require a real TTS model.
- Repeated calls with the same inputs are stable.

Verification:

```bash
make test
```

Out of scope: the final public `SpeechSynthesizer` port and Kokoro.

### PR 13: Assemble timeline segments into narration

Goal: Concatenate speech and silence in exact timeline order.

Changes:

- Normalize segment formats before concatenation.
- Generate a safe FFmpeg concat manifest.
- Produce `narration.wav` or a lossless equivalent.
- Record measured segment and final durations.

Acceptance criteria:

- Segment ordering matches the timeline.
- Expected and measured durations agree within documented tolerance.
- Empty timelines and missing segment files fail clearly.
- Integration tests cover speech-silence-speech assembly.

Verification:

```bash
make test
```

Out of scope: ambience, mastering, and delivery codecs.

### PR 14: Add the first end-to-end CLI generation path

Goal: Produce a correctly timed lossless meditation from a text script using only fixture speech.

Changes:

- Add `whoopy generate SCRIPT`.
- Connect run creation, compilation, segment rendering, assembly, and artifact recording.
- Print concise stage progress and the final output path.
- Add an example sleep script.

Acceptance criteria:

- One command creates input, resolved config, timeline, segments, narration, and manifest artifacts.
- The command returns nonzero on failure.
- An end-to-end test runs offline with the fixture synthesizer.
- Re-running the same source creates a separate run without overwrites.

Verification:

```bash
whoopy generate examples/sleep.txt --tts fixture
make test
```

Out of scope: real TTS, ambience, and generated scripts.

## Milestone 3: Real Speech And Production Audio

### PR 15: Introduce typed backend ports and errors

Goal: Make model and renderer implementations swappable behind stable contracts.

Changes:

- Add base adapter metadata.
- Add `SpeechSynthesizer`, `ScriptGenerator`, `AmbienceGenerator`, and `Renderer` protocols.
- Add `TransientError` and `FatalError`.
- Move fixture components behind these ports.
- Add contract-test helpers reusable by every adapter.

Acceptance criteria:

- The existing fixture pipeline behaves unchanged.
- Pipeline code does not import concrete adapter classes directly.
- Adapter metadata includes `versioned_model_id` and `license_id`.
- Type checking proves required port methods are implemented.

Verification:

```bash
make check
whoopy generate examples/sleep.txt --tts fixture
```

Out of scope: automatic retries and real model adapters.

### PR 16: Add the model adapter registry

Goal: Select adapters by configuration instead of hard-coded imports.

Changes:

- Load adapter declarations from `config/models.yaml`.
- Validate adapter class, runtime, version, and license metadata.
- Construct only the selected adapter.
- Add `whoopy models list` and `whoopy models inspect NAME`.

Acceptance criteria:

- The fixture adapter is selected through configuration.
- Unknown, malformed, or incompatible adapters fail before a run starts.
- Listing models does not load model weights.

Verification:

```bash
whoopy models list
whoopy models inspect fixture
make test
```

Out of scope: plugin discovery and third-party package loading.

### PR 17: Add the universal Kokoro speech adapter

Goal: Replace fixture tones with local generated narration through one Windows/macOS/Linux path.

Changes:

- Add Kokoro through sherpa-onnx as the universal native adapter.
- Package or acquire verified sherpa-onnx binaries for each supported platform.
- Pin and record the model and voice versions.
- Support text, voice, native speed, seed where supported, and output path.
- Add an optional model download/setup command.
- Keep real-model tests separate from fast CI tests.

Acceptance criteria:

- A short sentence produces audible speech.
- Output metadata records the selected model and license.
- Missing weights or an unsupported runtime produce clear recovery instructions.
- The worker runs natively without Docker on Windows, macOS, and Linux.
- The same adapter contract selects CPU or an available ONNX execution provider.

Verification:

```bash
whoopy models doctor sherpa_onnx_kokoro
whoopy generate examples/sleep.txt --tts sherpa_onnx_kokoro
make test
```

Out of scope: StyleTTS 2, OpenAudio S1, Piper, and voice cloning.

### PR 18: Trim and normalize speech segments

Goal: Ensure explicit timeline silence is the only source of deliberate pause duration and speech loudness is consistent.

Changes:

- Detect and trim leading and trailing baked-in TTS silence.
- Retain a documented 20–30 ms edge residual.
- Normalize individual speech segments to a documented target.
- Preserve raw adapter output for debugging when configured.

Acceptance criteria:

- Tests prove that added TTS edge silence is removed within tolerance.
- Explicit silence segments remain unchanged.
- Speech segment loudness stays within the documented range.
- Processing never overwrites the raw source artifact in place.

Verification:

```bash
make test
whoopy generate examples/sleep.txt --tts sherpa_onnx_kokoro
```

Out of scope: final program loudness mastering.

### PR 19: Add content-addressed segment caching

Goal: Avoid regenerating identical speech and enable cheap partial reruns.

Changes:

- Hash normalized text, voice, speed, delivery mode, model version, and relevant synthesis settings.
- Store cached audio with metadata and integrity checks.
- Link or copy cache hits into run directories.
- Add cache inspection and pruning commands; pruning must be explicit and safely scoped.

Acceptance criteria:

- An identical request reuses the cached segment.
- Changing any synthesis-affecting input misses the cache.
- Corrupt entries are detected and regenerated.
- Tests never share a global user cache.

Verification:

```bash
whoopy generate examples/sleep.txt --tts fixture
whoopy generate examples/sleep.txt --tts fixture
whoopy cache stats
make test
```

Out of scope: distributed caches.

### PR 20: Add a license-tracked ambience library

Goal: Introduce background ambience without losing provenance or redistribution safety.

Changes:

- Add a small CC0 ambience fixture or documented download path.
- Add a manifest containing source, creator, license, checksum, duration, and tags.
- Validate every configured asset against the manifest.
- Implement loop selection by tag.

Acceptance criteria:

- No ambience asset can be used without license metadata.
- Checksums detect modified assets.
- Fixture tests remain small and repository-friendly.
- Missing full-size assets produce setup guidance.

Verification:

```bash
whoopy ambience list
whoopy ambience verify
make test
```

Out of scope: generative music.

### PR 21: Mix narration with ambience

Goal: Create a coherent pre-master mix from narration and an ambient bed.

Changes:

- Loop and trim ambience to narration length.
- Add configurable bed gain and fades.
- Upsample narration using the documented resampler when necessary.
- Apply simple ducking beneath speech.
- Save the lossless pre-master mix.

Acceptance criteria:

- The bed spans the full target duration without an audible hard boundary in the test fixture.
- Narration remains intelligible.
- Output uses the documented 48 kHz master format.
- Runs without ambience continue to work.

Verification:

```bash
whoopy generate examples/sleep.txt --tts fixture --bed test_rain
make test
```

Out of scope: advanced music cue automation and final mastering.

### PR 22: Add final mastering and delivery formats

Goal: Produce consistent listening-ready artifacts.

Changes:

- Implement two-pass loudness normalization.
- Apply the configured integrated loudness, true-peak, and loudness-range targets.
- Export a lossless FLAC master.
- Export AAC and Opus delivery files.
- Record measured mastering statistics in the artifact manifest.

Acceptance criteria:

- Integrated loudness and true peak fall within documented tolerances.
- FLAC, AAC, and Opus outputs probe successfully.
- Delivery encoding starts from the lossless master.
- The master is never produced by transcoding a lossy artifact.

Verification:

```bash
whoopy generate examples/sleep.txt --tts fixture --bed test_rain
whoopy audio inspect runs/<run-id>/master.flac
make test
```

Out of scope: streaming manifests.

### PR 23: Add the audio quality gate

Goal: Fail bad artifacts before they are presented as complete.

Changes:

- Check duration, empty files, clipping, loudness, sample format, missing segments, and suspiciously long silence.
- Produce a machine-readable QC report.
- Distinguish warnings from failures.
- Add intentionally broken audio fixtures.

Acceptance criteria:

- Known-good fixtures pass.
- Clipped, truncated, missing, or silent fixtures fail for the expected reason.
- A failed QC gate marks the run unsuccessful.
- QC thresholds are configuration-driven and documented.

Verification:

```bash
make test
whoopy qc runs/<run-id>/master.flac
```

Out of scope: round-trip ASR text comparison.

## Milestone 4: Script Generation And Recovery

### PR 24: Add prompt templates and the fixture script generator

Goal: Establish the planning, writing, and editorial contracts without loading a real LLM.

Changes:

- Add versioned plan, script, and editorial system prompts.
- Define structured plan and section models.
- Implement a deterministic fixture script generator.
- Store all intermediate text artifacts in the run directory.

Acceptance criteria:

- A theme and duration produce a plan, sections, edited script, and valid timeline.
- Prompt versions appear in run metadata.
- Tests cover each stage independently.

Verification:

```bash
whoopy generate --theme sleep --minutes 5 --llm fixture --tts fixture
make test
```

Out of scope: a real local LLM.

### PR 25: Add the first universal llama.cpp script generator

Goal: Generate real meditation scripts locally through the `ScriptGenerator` port.

Changes:

- Implement the llama.cpp/GGUF adapter as the universal CPU/Metal/CUDA/Vulkan-capable path.
- Resolve a precise small GGUF artifact from the safe runtime profile.
- Implement plan, section-by-section writing, and editorial stages.
- Validate structured outputs and retry only malformed generations.
- Record model, quantization, seed, prompt versions, and generation settings.

Acceptance criteria:

- A prompt produces a coherent script and valid timeline without cloud services.
- Long generation is divided into bounded sections.
- Invalid structured output gets a limited retry and then a clear failure.
- Model setup, checksums, expected memory, and measured throughput are documented.
- Profile selection refuses unsafe downloads or loads.

Verification:

```bash
whoopy models doctor <gguf-model>
whoopy generate --theme sleep --minutes 5 --llm auto --tts fixture
make test
```

Out of scope: comparing several LLMs or making Qwen3-32B mandatory before the integration is proven.

### PR 26: Add pipeline checkpoints and run state

Goal: Persist enough state to inspect and resume long-running generations.

Changes:

- Define explicit pipeline states.
- Write a checkpoint after each major stage and each synthesized segment.
- Record start time, completion time, attempt count, and failure details.
- Make state transitions validated and idempotent.

Acceptance criteria:

- Interrupting after compilation preserves usable artifacts.
- State cannot skip required stages accidentally.
- Repeating a completed idempotent stage does not corrupt the run.
- Checkpoint tests simulate interruption without real models.

Verification:

```bash
make test
whoopy run inspect <run-id>
```

Out of scope: automatic background execution.

### PR 27: Add resume, retry, and partial regeneration

Goal: Recover from one failed segment without restarting the entire meditation.

Changes:

- Add `whoopy run resume RUN_ID`.
- Retry transient errors with bounded exponential backoff.
- Surface fatal errors immediately.
- Add targeted speech-segment regeneration.
- Invalidate only dependent downstream artifacts.

Acceptance criteria:

- A simulated failure in segment N resumes at segment N.
- Completed unaffected segments are reused.
- Regenerating one segment rebuilds narration, mix, master, and QC but not the script.
- Retry counts and reasons appear in run metadata.

Verification:

```bash
make test
whoopy run resume <run-id>
whoopy segment regenerate <run-id> <segment-id>
```

Out of scope: distributed workers.

### PR 28: Add round-trip ASR smoke testing

Goal: Detect gross TTS truncation, dropout, or garbling.

Changes:

- Add an optional local ASR adapter.
- Normalize expected and transcribed text.
- Calculate a lenient character or word error measure.
- Store transcripts and QC evidence.

Acceptance criteria:

- Clearly truncated or unrelated speech fails.
- Minor punctuation, homophone, and soft-speech differences do not cause brittle failures.
- ASR checks can be disabled for lightweight operation.
- CI uses fixtures or mocks instead of downloading an ASR model.

Verification:

```bash
make test
whoopy qc --with-asr <run-id>
```

Out of scope: transcript publication.

## Milestone 5: Local Self-Hosted Application

### PR 29: Add SQLite persistence

Goal: Store run records independently from filesystem checkpoints.

Changes:

- Add SQLAlchemy models for runs, segments, artifacts, adapters, and QC results.
- Add Alembic migrations.
- Use SQLite by default with Postgres-compatible modeling choices where practical.
- Add repository methods without coupling domain logic directly to SQLAlchemy sessions.

Acceptance criteria:

- A migration creates a fresh local database.
- Run and artifact records can be created, updated, queried, and related.
- Filesystem artifact paths are stored relative to the configured data root.
- Database tests use isolated temporary databases.

Verification:

```bash
whoopy db upgrade
make test
```

Out of scope: embeddings and Postgres deployment.

### PR 30: Add the FastAPI control plane

Goal: Expose stable local HTTP contracts for creating and inspecting runs.

Changes:

- Add health and version endpoints.
- Add create, list, and detail endpoints for runs.
- Add artifact metadata endpoints.
- Generate and check the OpenAPI schema.
- Keep long-running work out of HTTP request handlers.

Acceptance criteria:

- API tests cover success, validation, missing resources, and internal failures.
- Creating a run returns promptly with an ID.
- Response models do not expose arbitrary filesystem paths or secrets.
- OpenAPI generation succeeds.

Verification:

```bash
make api
make test
```

Out of scope: authentication and public internet exposure.

### PR 31: Add Huey background jobs

Goal: Process generation asynchronously while retaining native CPU and accelerator access on every supported operating system.

Changes:

- Configure Huey for local operation.
- Enqueue generation by run ID.
- Add the native worker command.
- Connect checkpoints, retries, and database status.
- Handle clean worker shutdown.

Acceptance criteria:

- The API creates a queued run without performing ML work.
- `make worker` processes the run natively.
- Restarting a worker does not duplicate completed work.
- Job failures are visible through the API.

Verification:

```bash
make worker
make api
make test
```

Out of scope: running the ML worker in Docker on macOS.

### PR 32: Scaffold the SvelteKit local PWA

Goal: Create the smallest frontend connected to the local API.

Changes:

- Add the SvelteKit project under `web/`.
- Add formatting, linting, type checks, and frontend tests.
- Add API client configuration.
- Add application shell, navigation, and error boundaries.
- Add a basic PWA manifest.

Acceptance criteria:

- The frontend starts locally and reaches the API health endpoint.
- Loading, disconnected, and API-error states are visible.
- Frontend checks run from the root Makefile.

Verification:

```bash
make web
make check
```

Out of scope: the generation form and audio player.

### PR 33: Add meditation submission and run progress UI

Goal: Let a local user start a meditation and understand its progress.

Changes:

- Add prompt, duration, voice, pacing, and ambience controls.
- Submit a run to the API.
- Add run list and detail pages.
- Poll or stream state changes with a documented fallback.
- Display stages, completed segments, warnings, and failures.

Acceptance criteria:

- A user can create a fixture-backed run from the browser.
- Refreshing the page preserves the visible run state.
- Validation errors appear next to the responsible fields.
- Progress does not claim completion before QC passes.

Verification:

```bash
make dev
make check
```

Out of scope: editing the generated timeline.

### PR 34: Add playback, artifacts, retry, and regeneration UI

Goal: Complete the essential local workflow after generation.

Changes:

- Add playback for delivery audio.
- Show timeline, script, QC report, and model metadata.
- Add retry/resume controls.
- Add targeted segment regeneration with confirmation.
- Add safe artifact download endpoints and UI actions.

Acceptance criteria:

- Completed audio plays in the browser.
- A failed run can be resumed from the UI.
- A selected segment can be regenerated without recreating the script.
- The UI clearly distinguishes raw, intermediate, master, and delivery artifacts.

Verification:

```bash
make dev
make check
```

Out of scope: public sharing.

### PR 35: Add one-command local setup and operational documentation

Goal: Make the polished local product reproducible on clean Windows, macOS, and Linux laptops.

Changes:

- Finalize cross-platform `uv` setup plus `whoopy worker`, `whoopy dev`, and `whoopy doctor` commands.
- Build native installer artifacts on each target operating system so end users do not install Python or build tools.
- Add an optional Docker Compose file only for appropriate non-ML services.
- Document native worker startup and platform service integration only where optional.
- Add troubleshooting for FFmpeg, model caches, memory pressure, ports, databases, and failed jobs.
- Add a clean-machine verification checklist.

Acceptance criteria:

- A new machine can follow one documented path from clone to fixture generation.
- Real-model setup is explicit and separable from the fast fixture setup.
- Hardware profiling selects safe capabilities without asking users to choose a backend.
- Clean Windows, macOS, and Linux machines pass installer smoke tests.
- All documented commands match real commands.

Verification:

```bash
uv sync --locked
uv run whoopy doctor
uv run whoopy worker
uv run whoopy dev
```

Out of scope: the public Commons platform.

## Milestone 6: Model And Product Hardening

These PRs can begin only after the local workflow is dependable.

### PR 36: Validate production model profiles and optional accelerators

Goal: Bind measured, quality-tested model artifacts to Lite, Standard, High, and Studio without changing the pipeline.

Changes:

- Pin one GGUF model and checksum per supported profile after the quality bakeoff.
- Measure memory use, generation time, and output quality across representative Windows, macOS, and Linux hardware.
- Add optional MLX acceleration on Apple Silicon only if it passes the same contracts and improves measured performance.
- Document quantization, model acquisition, and automatic fallback.
- Add representative prompt evaluations.

Acceptance criteria:

- Every profile completes within its documented live-memory margin.
- Model identity and quantization are recorded in every run.
- Quality is compared across profile candidates before any becomes a default.

Verification: Run the documented cross-platform evaluation suite and attach results to the PR.

### PR 37: Add the StyleTTS 2 adapter and blind comparison harness

Goal: Evaluate a more expressive public-safe narration option.

Changes:

- Implement the adapter behind `SpeechSynthesizer`.
- Verify and record code, weight, and reference-audio licenses.
- Add an A/B comparison command using identical timeline segments.
- Randomize labels for blind listening.

Acceptance criteria:

- Contract tests pass.
- License evidence is documented before public-safe status is enabled.
- Comparisons use level-matched outputs and identical source text.

### PR 38: Add the Piper fallback adapter

Goal: Provide a lightweight, portable narration fallback.

Changes:

- Implement Piper behind the same speech port.
- Add voice/model metadata and licenses.
- Add adapter contract tests and setup documentation.

Acceptance criteria:

- The end-to-end pipeline works without MLX.
- Output metadata identifies the fallback model and voice.
- Failure and cache behavior match the existing adapter contract.

### PR 39: Add timeline editing and selective rerendering

Goal: Let users refine generated work without restarting the pipeline.

Changes:

- Add safe edits for speech text and silence durations.
- Validate edited timelines.
- Identify affected cache keys and downstream artifacts.
- Rerender only changed speech plus dependent assembled outputs.

Acceptance criteria:

- Editing one silence does not rerun TTS.
- Editing one speech segment reruns only that segment's TTS.
- Invalid edits cannot replace the last valid timeline.

## Milestone 7: Public Commons Platform

Do not start this milestone until local artifacts, licenses, and manifests are stable. Each PR below requires a threat-model and privacy review appropriate to its scope.

### PR 40: Define and sign the publish manifest

Goal: Establish the exact contract between a local Whoopy instance and Commons.

Changes:

- Define the versioned publish manifest.
- Include hashes, durations, languages, model chain, license chain, QC results, and artifact metadata.
- Add Ed25519 signing and verification.
- Add compatibility and tampering tests.

Acceptance criteria:

- Modified manifests or artifacts fail verification.
- Unsupported versions fail safely.
- Non-redistributable adapter chains cannot be marked publishable.

### PR 41: Add the Commons service and Postgres schema

Goal: Create the deployable public service foundation without accepting uploads yet.

Changes:

- Add the Commons FastAPI service.
- Add Postgres models and migrations.
- Add health, readiness, and version endpoints.
- Add deployment configuration and isolated tests.

Acceptance criteria:

- A fresh database migrates successfully.
- Service startup and health checks work.
- Local core operation remains independent from Commons.

### PR 42: Add instance pairing and authenticated publishing

Goal: Allow an authorized local instance to submit a signed manifest.

Changes:

- Add instance registration and revocation.
- Add scoped API credentials.
- Verify signatures, timestamps, replay protection, and manifest versions.
- Add audit records without logging secrets.

Acceptance criteria:

- Unpaired, revoked, replayed, expired, or incorrectly signed requests fail.
- Credentials are stored and displayed safely.
- Pairing can be revoked without deleting published content automatically.

### PR 43: Add object storage and artifact ingestion

Goal: Store verified audio artifacts safely and reproducibly.

Changes:

- Add an object-storage abstraction.
- Use presigned, size-limited upload flows.
- Verify hashes, MIME types, codecs, and durations after upload.
- Quarantine incomplete or invalid uploads.

Acceptance criteria:

- Uploaded bytes must match the signed manifest.
- Unsupported or oversized uploads fail safely.
- Failed ingestion does not create a public meditation.

### PR 44: Add the hard license and automated moderation gate

Goal: Prevent unlicensed or clearly unsafe content from becoming public.

Changes:

- Enforce allowlisted redistribution licenses for the entire generation chain.
- Add script and audio checks.
- Add quarantine, rejection, and review states.
- Preserve moderation evidence and reason codes.

Acceptance criteria:

- Any local-only or unknown license blocks publication.
- Moderation failure never publishes partially.
- Decisions are auditable and can be appealed or re-reviewed.

### PR 45: Add browse, detail, search, and streaming APIs

Goal: Expose approved public meditations through a versioned read API.

Changes:

- Add pagination, filtering, sorting, detail, and stream URL endpoints.
- Add cache controls and CDN-compatible delivery.
- Prevent quarantined, rejected, or deleted content from appearing.

Acceptance criteria:

- Only approved content is returned.
- Pagination and filters are deterministic.
- Stream delivery supports expected browsers and range requests.

### PR 46: Add the public SvelteKit PWA

Goal: Let users discover, play, and download public meditations.

Changes:

- Add browse, search, detail, and playback experiences.
- Add accessible media controls and Media Session integration.
- Add appropriate offline caching for metadata and opted-in audio.
- Add shareable metadata for public pages.

Acceptance criteria:

- Browse-to-play works on target desktop and mobile browsers.
- Keyboard and screen-reader playback controls work.
- Offline behavior is explicit and storage-conscious.

### PR 47: Add reporting and moderator review

Goal: Complete the minimum responsible public-content lifecycle.

Changes:

- Add user reports with rate limits and reason categories.
- Add a moderator queue and decision history.
- Add unpublish, restore, and escalation actions.
- Notify relevant owners without leaking reporter identity.

Acceptance criteria:

- Reports cannot directly remove content without policy.
- Moderator actions are authorized and audited.
- Unpublished media stops being returned or streamed promptly.

## Release Gates

### v0.1 CLI release gate

PRs 1–28 are complete. The release is ready when:

- a single CLI command generates a meditation locally;
- the canonical timeline is stored and validated;
- deliberate pauses are deterministic;
- speech segments are cached and individually regenerable;
- FLAC, AAC, and Opus artifacts pass QC;
- interrupted work resumes without a full restart;
- fixture-backed end-to-end tests run offline.

### v1.0 local application release gate

PRs 29–39 are complete. The release is ready when:

- the API, worker, and UI provide the complete local workflow;
- a user can create, monitor, play, inspect, retry, and edit a meditation;
- native setup and hardware refusal succeed predictably on Windows, macOS, and Linux;
- Basic mode remains useful without a local LLM;
- adapter and asset licenses are recorded in artifacts;
- documentation matches the shipped commands and behavior.

### v2.0 Commons release gate

PRs 40–47 are complete. The release is ready when:

- publishing uses verified signed manifests;
- every public artifact passes license and moderation gates;
- uploads are hash-verified and safely stored;
- approved meditations can be browsed and streamed;
- reporting, removal, audit, and credential-revocation paths work;
- local Whoopy remains fully useful without Commons.

## The First PR To Open

The empty-repository exception combines Milestone 0's three foundation slices in PR #1 so every later branch has a portable, tested base. After that PR merges, start only with **PR 4: Define the timeline segment models** and return to one numbered step per pull request.
