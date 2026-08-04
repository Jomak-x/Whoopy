# Roadmap

This roadmap turns the spec into implementation phases. Each phase has a reason for existing, not just a deliverable list.

> [!IMPORTANT]
> The authoritative near-term sequence is now
> [`local-first-master-plan.md`](./local-first-master-plan.md). It reconstructs
> the real merged/local/model state and defines PRs 12–21 required before the
> permanent Phase 4 UI begins. The broader phase descriptions below remain the
> long-range product map.

Implementation status:

- **Phase 0 — complete:** the portable foundation is merged.
- **Phase 1 — complete:** the local-core run and worker slice is merged.
- **Phase 2 — complete:** deterministic fixture audio is merged.
- **Phase 3 — complete:** segment caching, retry, recovery, and stronger integrity checks are merged.
- **Phase 3.5 — merged; human acceptance pending:** install, replaceable adapters, authored
  speech, validated generation, one-command audio, and automatic model
  evaluation are merged. Anonymous voice samples await listener ratings.
- **Phase 3.6 — merged; human acceptance pending:** adaptive pacing and
  additional voice adapters are merged, but voice and meditation quality are
  not accepted until listening review.
- **Local stabilization — PR 14 active:** durable recovery is merged and cross-platform CI is green;
  managed voice packs are being made installable, verifiable, removable, and
  testable before wider voice comparison. Blind voice
  selection, meditation evaluation, technique blueprints, pacing v2, reviewed
  breathing exercises, the polished local UI, and an offline soak gate remain
  PRs 14–22.
- **Phase 4 — laboratory slice only:** a small private local tester exercises
  the real flow; the permanent editor and product-polish work must wait for the
  local V1 exit gate.
- **Phases 5–6 — planned:** extended local capabilities and public-platform behavior are not implemented yet.

## Phase 0: Documentation And Repo Foundation

Goal:

- turn the design spec into readable project docs
- create the repo layout and configuration skeleton

Why this phase exists:

- the repository began as only a spec
- the code should start from a stable map
- new contributors need an entry point before they need features

Done when:

- the docs explain the architecture clearly
- the directory structure exists
- the baseline config files exist
- the package installs and its foundation quality gate passes
- native hardware inspection chooses a safe profile or refuses before model loading
- the same locked quality gate passes on Windows, macOS, and Linux

Evidence:

- `whoopy --help` and `whoopy config show` run successfully
- `whoopy doctor` reports structured capabilities without loading a model
- `uv run python scripts/check.py` runs linting, formatting, strict typing, and tests
- configuration precedence is covered by automated tests

## Phase 1: Local Core Skeleton

Goal:

- build the smallest possible local generation flow
- connect prompt input to a saved run record

Why this phase exists:

- it proves the control plane and worker boundaries
- it makes the architecture tangible quickly
- it surfaces missing assumptions before the system grows

Done when:

- a prompt creates a run
- a worker processes that run
- the system writes a timeline artifact

Evidence:

- `whoopy run create PROMPT` writes a validated queued `run.json`
- `whoopy worker process RUN_ID` owns the running/completed transitions
- a successful worker writes validated `timeline.json` before completion
- a failed worker saves a readable failed state
- run IDs are UUID-validated before becoming filesystem paths
- important JSON artifacts are atomically replaced
- tests cover the control plane, storage, worker, failure, and full CLI flow

Deliberate boundary:

- the timeline contains one prompt-passthrough `SPEECH` segment
- there is one foreground worker, not a polling or concurrent queue
- there is no model, TTS, audio, API, database, or UI yet

## Phase 2: Deterministic Audio Assembly

Goal:

- make the canonical timeline produce a real audio file
- ensure pauses and joins are deterministic

Why this phase exists:

- the main technical risk is audio timing, not UI polish
- explicit silence and segment joins are the heart of the product

Done when:

- a short meditation renders end-to-end
- pause timing is stable
- basic audio quality checks pass

Evidence:

- schema-v2 timelines contain validated `SPEECH` and exact `SILENCE` segments
- a dependency-free fixture produces audible deterministic speech markers
- the renderer writes a playable mono 24 kHz 16-bit PCM WAV
- an audio manifest records exact frame ranges for every segment
- the quality gate verifies format, frame count, duration, joins, silence, audibility, and clipping
- a corruption test proves nonzero data inside silence is rejected
- Phase 1 schema-v1 records and timelines remain readable

## Phase 3: Quality, Caching, And Recovery

Goal:

- add cached per-segment synthesis
- add retry and resume behavior
- add quality checks for output integrity

Why this phase exists:

- long jobs need partial recovery
- cached segments make iterative improvement practical
- quality gates prevent regressions that are hard to hear by inspection

Done when:

- a failed segment can be regenerated without starting over
- repeated renders reuse cached work
- tests catch timing or clipping regressions

Evidence:

- canonical SHA-256 keys include every current synthesis-affecting input
- cache reads revalidate metadata, PCM length, digest, format, audibility, and headroom
- corrupt cache entries become misses and are regenerated
- every run stores verified per-speech-segment checkpoints
- transient errors use bounded exponential backoff while fatal errors stop immediately
- `whoopy run resume RUN_ID` reuses completed checkpoints after failure or interruption
- schema-v3 run records expose attempts, resumes, cache hits/misses, checkpoint reuse, progress, and failed segment ID
- the final manifest contains per-segment and whole-stream PCM digests
- tests deliberately introduce timing and peak-headroom regressions and reject both
- Phase 1 and Phase 2 run records remain readable

## Phase 3.5: First Real Local Meditation

Goal:

- replace fixture tones with real local speech
- turn a prompt into a validated meditation plan, script, and timeline
- prove the complete flow offline before building the permanent UI

Why this phase exists:

- Phase 3 proves reliability with deterministic fixtures, not model quality
- a UI should be built around the real workflow and artifacts
- model downloads and native runtime compatibility must be solved before an
  internet-constrained development period

Implementation order:

1. add a verified, resumable offline artifact manager — implemented
2. add typed llama.cpp and sherpa-onnx adapters — implemented
3. render a pasted script with real Kokoro speech — implemented
4. generate and validate a meditation locally with Qwen3-4B — implemented
5. join both paths behind one `whoopy generate` command — implemented
6. run a documented model and voice bake-off before freezing defaults —
   automatic model comparison implemented and merged; human voice review pending

Each numbered item is a separate PR. The detailed changes, acceptance criteria,
initial artifact pins, and research sources are in
[`phase-3-5-first-local-meditation.md`](./phase-3-5-first-local-meditation.md).

Done when:

- one prompt creates a three- to five-minute spoken meditation locally
- the installed stack runs without networking
- Basic mode creates real speech from a pasted script without an LLM
- generated plans, scripts, timelines, model metadata, and audio remain inspectable
- real speech reuses Phase 3 caching, retry, resume, and quality checks
- hardware checks choose a safe profile or refuse before an unsafe load
- model and voice implementations remain replaceable behind typed ports

## Phase 4: Local Product Polish

Entry condition:

- every item in the
  [Local V1 exit gate](./local-first-master-plan.md#local-v1-exit-gate) passes.

Current first slice:

- `whoopy web --open` starts a dependency-free tester on `127.0.0.1`
- the page uses the real CLI pipeline for prompt and authored-script modes
- readiness, task state, recent durable runs, audio, and important artifacts
  are visible in one place
- the server does not expose itself to the local network or a cloud service

This tester is intentionally not the full Phase 4 editor.

Goal:

- make the local UI pleasant and reliable
- improve run visibility and editing workflows

Why this phase exists:

- a good engine is not enough if the user cannot understand it
- the local experience should feel simple enough to trust

Done when:

- the UI can launch and monitor runs
- logs and outputs are easy to inspect
- the local workflow feels coherent

## Phase 5: Public Platform

Goal:

- add the optional sharing layer
- publish rendered meditations with proper metadata and license checks

Why this phase exists:

- sharing is valuable only after the local core is stable
- public publishing introduces moderation and storage concerns

Done when:

- published items can be browsed
- audio assets are stored and delivered correctly
- license checks are enforced before publication

## Phase 6: Ecosystem Hardening

Goal:

- stabilize the system for other contributors
- make the model and backend choices easy to swap
- document operational limits and troubleshooting

Why this phase exists:

- systems become easier to extend when the boundaries are explicit
- future model upgrades should not require a rewrite

Done when:

- adapter swaps are mostly configuration changes
- documentation matches the real build
- the setup process is predictable from a clean machine

## Practical Milestone Ordering

If you want to learn while building, do not jump ahead.

Use this order instead:

1. understand the architecture
2. create the repo skeleton
3. define the timeline schema
4. build the local pipeline
5. verify audio determinism
6. add recovery and tests
7. prove real local script and speech generation
8. build the permanent local UI around the proven workflow
9. only then think about the public platform

That sequence keeps the hardest unknowns visible early.
