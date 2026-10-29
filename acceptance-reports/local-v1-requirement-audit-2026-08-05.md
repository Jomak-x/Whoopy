# Whoopy Local V1 requirement-by-requirement audit

Generated 2026-08-05 against authoritative plan SHA-256
`84bc74e856d96b2769cfc29b9587912aa5e2930939dff5eb5b2a4b66665046d3`.

This is an implementation audit, not a release waiver. `Proven` means current source plus the named
test or durable receipt proves the requirement on the development Mac. `Implemented` means the
product path exists but its full platform, hardware, visual, or human scope is not independently
proven. `External gate` means product code cannot manufacture the required observation. `Model
gate` means model/runtime acquisition or compatible hardware is still required. The fail-closed
release authority remains `acceptance-reports/local-v1-status-current.json`.

## Product architecture

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| ARCH-01 | Permanent Studio and Lab in one local browser product | Proven | `web/src/routes/+page.svelte`, `tests/test_local_api.py::test_fastapi_is_versioned_and_serves_the_bundled_product` |
| ARCH-02 | Bundled SvelteKit static frontend served by versioned FastAPI | Proven | `src/whoopy/webui/static/`, `/api/v1`, `tests/test_frontend_bundle.py`, `tests/test_local_api.py` |
| ARCH-03 | Runs/checkpoints/manifests authoritative; SQLite rebuildable | Proven | `src/whoopy/pipeline/`, `src/whoopy/webui/catalog.py`, and catalog/API tests; durable `studio`/`lab`/`system` ownership survives creation, promotion, resume, trash, and restore, while version-keyed experiment/rating/decision rows rebuild from authoritative Lab JSON and schema migration preserves settings |
| ARCH-04 | Reuse worker, heartbeat, locks, resume, cancel, cache; no second queue stack | Proven | `src/whoopy/pipeline/worker.py`, `locks.py`, `cache.py`; dependency audit shows no Redis/Huey/Postgres/Docker workflow engine |
| ARCH-05 | `whoopy app` starts API, worker supervision, reconciliation, UI, browser | Proven | `src/whoopy/cli.py`, app/recovery tests, and the lifecycle receipt; startup now reconciles abandoned queued placeholders, and new browser requests persist a checksum-bound launch envelope before worker creation so true pre-launch interruptions can resume with the original validated settings |
| ARCH-06 | Default `127.0.0.1`; origin/body/path protection; no cloud endpoints | Proven | `src/whoopy/webui/api.py`, `tests/test_local_api.py::test_fastapi_rejects_cross_origin_mutation`, `test_local_product_exposes_no_cloud_or_ambience_workflow`, web body/path tests |
| ARCH-07 | Offline inference after acquisition | Implemented | Workers set offline library policy; the installed-bundle harness can now enforce and probe external-network denial with loopback preserved on macOS, while independent target observation is still required on all five release targets |
| ARCH-08 | Friendly profiles instead of backend jargon | Proven | Studio exposes Basic/Lite/Standard and friendly names; hardware backend selection stays diagnostic (`web/src/routes/+page.svelte`, `tests/test_local_api.py`) |
| ARCH-09 | Basic authored-script path without LLM | Proven | `tests/test_local_api.py`, `tests/test_worker.py`, lifecycle receipt |
| ARCH-10 | Lite low-resource experiment | Implemented | Qwen3-1.7B profile/adapter exists and Lab comparison evidence exists; promotion is not yet supported by human decision evidence |
| ARCH-11 | Standard prompt generation plus Kokoro | Proven locally | `evaluations/local/standard-telemetry-current.json` and Studio lifecycle; independent platforms remain external gates |
| ARCH-12 | Optional High/Studio LLMs and expressive voices | Implemented | Optional candidates are isolated in Lab; Fish/MOSS/Qwen release runtimes and compatible hardware remain model gates |
| ARCH-13 | Every desktop has prompt-to-audio plus Basic fallback | External gate | Declared CI targets are not independent inference evidence; Windows, macOS x64, Linux x64/arm64 reports are absent |

## Canonical meditation plan and pacing

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| PLAN-01 | Versioned plan with kind, outcome, blueprint ID/version | Proven | `src/whoopy/meditation/models.py`, `blueprints.py`, `tests/test_meditation.py::test_v2_plan_derives_kind_ending_delivery_and_deterministic_silence` |
| PLAN-02 | Arrival, development, ending beats and purpose per block | Proven | Blueprint schema/config and meditation tests |
| PLAN-03 | Explicit silence intent and duration budget | Proven | `src/whoopy/meditation/pacing.py`, timeline tests |
| PLAN-04 | Natural/reviewed paced breath mode | Proven | `src/whoopy/meditation/breathing.py`, reviewed protocol tests |
| PLAN-05 | Delivery per spoken block and daytime/sleep ending contract | Proven | typed plan schema and daytime/sleep tests |
| PLAN-06 | Instructional, embodied, reflective, repeated, progressive, extended quiet allocation | Proven | `pacing.py`, blueprint config, 1/20-minute and progressive pacing tests |
| PLAN-07 | Rendered duration within 5% | Proven | QC and Lab provenance duration tests; 20-run export soak receipt |
| PLAN-08 | LLM cannot invent pause durations, breath ratios/holds, or exercises | Proven | strict schemas/compiler, `test_prescribed_breath_control_is_rejected`, breath syntax tests |

## Typed timeline and expressive delivery

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| TIME-01 | Clean speech text with typed delivery values and intensity | Proven | `src/whoopy/timeline/models.py`, `test_delivery_controls_are_typed_and_never_enter_spoken_text` |
| TIME-02 | Typed language, preset/reference/designed mode, pronunciation, neutral pace | Proven | timeline/ports models and adapter contract tests |
| TIME-03 | Author cues removed; unsupported tags never spoken | Proven | timeline compiler rejection/removal tests |
| TIME-04 | Kokoro capability translation | Proven | Kokoro adapter contracts and production smoke evidence |
| TIME-05 | Fish reference conditioning and visible fallback | Implemented | adapter contract tests pass; isolated runtime is missing, so real current-revision synthesis is a model gate |
| TIME-06 | MOSS 5B/8B reference/direct, language, instruction, duration, pronunciation | Implemented | typed adapter/API/contracts exist; real compatible-hardware smoke remains a model gate |
| TIME-07 | Qwen CustomVoice, VoiceDesign, and Base typed translation | Implemented | adapters and contract/integration tests; approved runtime revision/wheelhouses remain a model gate |
| TIME-08 | Deterministic `SILENCE`, not model-native pause markup | Proven | compiler/renderer/timeline tests and soak artifacts |
| TIME-09 | Every output-affecting control in cache identity and manifest | Proven | cache tests, revision invalidation tests, Lab attestation/provenance tests |

## PR 15 — real selectable model output

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| P15-01 | Reproduce MOSS failure with logs, memory, output, stage | Implemented | structured worker/smoke evidence paths exist; current Mac is resource-blocked, so compatible-hardware reproduction remains a model gate |
| P15-02 | Separate installed files, runtime probe, current-revision playable synthesis | Proven | model-pack state/schema and readiness tests; consolidated status correctly fails Fish/MOSS |
| P15-03 | Test voice performs production synthesis and saves playable sample | Proven | model-pack smoke endpoint/API test; Kokoro current sample evidence |
| P15-04 | Disable missing/resource/incompatible/failed models with remedy | Proven | capability/readiness endpoint and Studio selector tests/source |
| P15-05 | Fix MOSS load/request/decode/empty/timeout/cleanup paths | Implemented | isolated process adapter and contract tests; 5B/8B real compatible-hardware results remain missing |
| P15-06 | Validate PCM, signal, sample rate, duration, clipping, unload | Proven in framework | smoke/QC/unload tests; each optional runtime still needs real evidence |
| P15-07 | Structured long-load progress | Proven | background model task API and pollable live metrics test |
| P15-08 | Recoverable checksum-verified legacy cleanup | Proven | Checksum-bound plan `4db08d31f7e77b541abd181e` moved 7,138,896,585 obsolete bytes and 72,592 files to recoverable quarantine; receipt `5b20f229-75b6-4dae-bf0b-8c0d8ceece62`. Sole full MOSS checkpoints were preserved and nothing was purged |
| P15-09 | Kokoro/Fish/MOSS 5B/8B output or proven incompatibility | Model gate | Kokoro passes; Fish lacks runtime; MOSS is resource-blocked on this Mac. Release status remains failed as required |

## PR 16 — production model system

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| P16-01 | Clean pinned resumable isolated Fish/MOSS/Qwen installers | Model gate | deterministic builder/installer plus Studio runtime list/install/resume/verify APIs and UI exist; reviewed per-target locks/wheelhouses and approved Qwen revision remain absent |
| P16-02 | Qwen 0.6B/1.7B CustomVoice | Implemented | registry, adapter and integration contracts; release runtime evidence missing |
| P16-03 | Qwen 1.7B VoiceDesign | Implemented | reviewed preset derivation and tests; release runtime evidence missing |
| P16-04 | Qwen 0.6B/1.7B Base cloning with consent | Implemented | typed consented reference contract/tests; release runtime evidence missing |
| P16-05 | Typed delivery capability contract per adapter | Proven | ports models, adapters, API schemas, cross-adapter contract tests |
| P16-06 | Voice reference record/import/transcript/consent/trim/validate/preview/replace/delete | Proven | `voice_references.py`, full API tests, Svelte MediaRecorder/import UI and checker |
| P16-07 | Low-resource 1.7B, Standard 4B, measured larger candidates only via llama.cpp | Proven locally | configs, llama.cpp adapter, LLM Lab evidence and telemetry receipt |
| P16-08 | Heavy models sequential; live RAM recheck before load | Proven in framework | one-heavy-model enforcement and model smoke/runtime tests; generation and direct/background model/runtime operations now share atomic admission in both directions, closing the former race where generation could start during a smoke test |
| P16-09 | Run telemetry: load, memory, speed, seed, controls, runtime, license, hardware | Proven | `pipeline/telemetry.py`, `test_llm_telemetry_evidence.py`, real Standard telemetry receipt |
| P16-10 | Experimental/incompatible hidden from Studio, evidenced in Lab | Proven | Studio eligibility/API tests and Svelte filtering |
| P16-11 | Every enabled model/control has real contract and production smoke | Model gate | contract coverage exists; optional model production smokes are incomplete |

## PR 17 — Meditation Engine V2

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| P17-01 | Eleven original reviewed blueprints | Proven | `config/meditation_blueprints.yaml`, bundled loader, exact-set test |
| P17-02 | Arrival → instruction → quiet → reminder → deeper quiet → ending | Proven | blueprint validation and corpus structural receipts |
| P17-03 | Beat-level sparse narration around deterministic skeleton | Proven | three writing methods and meditation tests |
| P17-04 | Whole-script coherence/fidelity/repetition/reassurance/transition/talk/safety/ending checks | Proven | `editorial.py`, safety module and focused tests |
| P17-05 | Regenerate only failed sections; preserve accepted/raw attempts | Proven | generator/workspace implementation and repair/raw-attempt tests |
| P17-06 | Natural breath awareness | Proven | reviewed blueprint/breath tests |
| P17-07 | Gentle inhale/exhale, no holds, opt-out/dizziness guidance | Proven | protocol schema and explicit safety tests |
| P17-08 | Breath cycles and quiet compile to exact events | Proven | breath timeline tests and exact renderer/QC tests |
| P17-09 | Daytime reorientation vs sleep taper | Proven | separate editorial and fallback tests |
| P17-10 | Ten-prompt structural/safety pass and human improvement | Enforced external gate | 10/10 automatic coverage and zero safety failures pass. Acceptance now additionally requires blind content preference against the declared baseline for all ten cases and more challenger than baseline preferences; ratings/comparisons remain absent |

## PR 18 — permanent Studio

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| P18-01 | First-run hardware/model setup | Proven | setup card, hardware/readiness endpoints, UI tests/checker |
| P18-02 | Prompt and authored-script creation | Proven | generation API and lifecycle receipt |
| P18-03 | Duration/type/technique/voice/delivery/pacing controls | Proven | typed request schema and Studio UI; delivery/intensity derive from adapter capabilities, while reviewed pacing profiles now carry speech speed plus deterministic silence policy through the API, CLI, durable run config, resume command, and canonical timeline instead of reducing a pacing choice to an untracked speed value |
| P18-04 | Install/verify/remove/disk-use model views | Proven | managed model API/UI and tests |
| P18-05 | Real stage/segment/time/memory progress | Proven | live task projection/tests and Studio UI |
| P18-06 | Cancel/resume/retry/actionable logs | Proven | API/worker tests and UI controls plus the current lifecycle receipt, which actively cancels a real immutable authored run, waits through worker finalization, resumes from durable work, verifies playable audio and `resume_count`, and recoverably deletes both test runs |
| P18-07 | Search/filter library by status/model/duration/date | Proven | catalog/API/UI and range/filter tests; Studio defaults to durable `studio` ownership while explicit Lab/system/all queries and direct run IDs remain available |
| P18-08 | Run details: plan/script/timeline/models/QC/audio | Proven | versioned artifact/audio endpoints and editor tabs |
| P18-09 | Responsive, keyboard, labels, reduced motion | Proven locally | Svelte checker 0/0 and source accessibility tests pass. The rebuilt final bundle has renewed schema-v2 digest-bound desktop/narrow Studio and Lab QA, including sealed blind review, exact focus return, typed model-role enforcement, editor waveform/boundaries, and responsive 390x844 rendering |
| P18-10 | Settings export/import and local data explanation | Proven | catalog/API round-trip test and Studio UI |
| P18-11 | Safe Studio defaults; advanced/experimental only in Lab | Proven | generation schema rejects Lab-only controls from Studio; UI exposes raw MOSS instruction only in Lab |

## PR 19 — editor and finished workflow

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| P19-01 | Separate Plan/Script/Timeline/Delivery/Listen views | Proven | Svelte run-detail tabs and artifact endpoints |
| P19-02 | Immutable revisions, compare/restore | Proven | `revisions.py`, revision API/UI/tests |
| P19-03 | Safe narration/delivery/silence/technique/breath edits | Proven | strict revision models and semantic edit tests; run-scoped editor state is cleared before asynchronous artifact loading so a planless authored run cannot inherit or save a previously opened meditation's plan |
| P19-04 | Revalidate before active replacement | Proven | atomic revision store and rejection tests |
| P19-05 | Silence assembly-only; delivery/speech affected-TTS-only | Proven | selective invalidation tests |
| P19-06 | Waveform with speech/silence/breath/sections/regeneration | Proven | `audio_inspection.py` binds PCM spans to the canonical timeline, contiguous reviewed sections, breath cycle/protocol evidence, and saved-take selection status; exact-boundary and frontend visualization tests pass |
| P19-07 | Segment and section audition | Proven | byte crop API and tests |
| P19-08 | Regenerate/alternative/choose/undo/restore takes | Proven | `takes.py`, API/UI and take tests |
| P19-09 | Normalized master plus WAV/FLAC/AAC/Opus metadata exports | Proven | export implementation/tests and 20-run export receipt |
| P19-10 | Silence/duration/clipping/empty/corrupt/join/missing QC | Proven | `audio/quality.py`, export verification and audio tests |
| P19-11 | No ambience controls | Proven | API negative-surface test and UI source |
| P19-12 | Recoverable meditation deletion is actually restorable | Proven | versioned receipts bind run ID and audience; `/trash/runs` restore API/UI preserves Lab/system ownership and refuses tampered/symlinked entries |

## PR 20 — Evaluation Lab

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| P20-01 | `/lab` uses production APIs/adapters/artifacts | Proven | shared FastAPI control plane and `LocalProductionExperimentExecutor` tests; Lab production runs retain durable `lab` ownership through queued/active promotion and resume, stay out of Studio, and remain directly inspectable/recoverable |
| P20-02 | Controlled LLM comparison | Proven | production report plus frozen-control/provenance tests |
| P20-03 | Controlled three-way writing-method comparison | Proven | writing report and generation method tests |
| P20-04 | Controlled voice comparison | Proven | voice reports and identical text/silence/control assertions |
| P20-05 | Controlled pacing comparison | Proven | pacing report/profile/provenance tests |
| P20-06 | Versioned reusable evaluation sets | Proven | config, repository, exact ten-case tests, and an explicit versioned-set selector in Lab rather than an implicit first-set assumption |
| P20-07 | Single case before batch; queued sequential remaining/all | Proven | UI and queue/API tests |
| P20-08 | Randomized blind A/B | Proven | blind token assignment/storage tests and UI; candidate/artifact reveal is gated in the product until each available blind pair has a saved judgment, with visible per-pair rating progress |
| P20-09 | Waveform and artifact inspection | Proven | Lab evidence panel uses production waveform/artifact endpoints; Svelte checker passes |
| P20-10 | Metrics without hidden combined score | Proven | model validator and explicit test |
| P20-11 | 1–5 ratings and notes | Proven as capability | durable rating API/UI/tests; Lab shows saved dimensions/scores and prevents accidental duplicate dimension submissions in one review session; actual human ratings are external gate |
| P20-12 | Reports include hardware/model/runtime/seed/timing/controls | Proven | ExperimentReport provenance and attestation tests |
| P20-13 | Promotion via versioned decision record | Proven as capability | decision API/storage/default update tests; an accepted pacing profile now changes Studio's production silence policy and persists its version/digest rather than only relabeling TTS speed. LLM promotions require an explicit portable-default, low-resource-fallback, or optional higher-quality role, and role-specific settings prevent a fallback from overwriting the portable default; actual accepted decisions are external gate |
| P20-14 | Regression against accepted baseline | Proven as capability | baseline comparison and mismatch rejection tests; needs first human-backed baseline |
| P20-15 | No Cartesian-product experiments | Proven | experiment schema enforces one-variable axes and sequential workflow |
| P20-16 | Delivery axis extension | Proven | dedicated typed delivery experiment and one-variable tests; this strengthens but does not replace the four required axes |

## PR 21 — cross-platform release and acceptance

| ID | Requirement | State | Authoritative evidence or exact gap |
|---|---|---|---|
| P21-01 | One documented install/launch path on five desktop targets | Implemented | setup/native-portability docs, portable `{python}` acceptance commands, and five-target CI workflow; the final darwin-arm64 bundle installs and launches outside the checkout, while other targets remain independently unobserved |
| P21-02 | Bundled frontend; no end-user Node process | Proven | wheel/package freshness gate, frontend bundle tests, and the fresh-installed lifecycle receipt |
| P21-03 | Default inference offline on Windows/macOS/Linux | Partial | `platform-darwin-arm64-current.json` passes the canonical Standard/Kokoro production case under a macOS process sandbox with pre-reachable external endpoints denied by `EPERM` and localhost preserved; four other release targets remain independently unobserved |
| P21-04 | Kokoro + Basic on every platform | Implemented and proven on darwin-arm64 | the fresh install rejects foreign smoke evidence, runs production Test voice in its installed runtime, verifies playable PCM, and completes Basic generation/edit/export/resume/delete; independent remaining targets are missing |
| P21-05 | Automatic CPU/Metal/CUDA/Vulkan selection | Implemented | hardware selection tests pass; representative target hardware observations missing |
| P21-06 | Migration tests for timeline/run/cache/settings/model records | Proven | `release/migrations.py`, `tests/test_release_acceptance.py` |
| P21-07 | Forced interruption at six stages | Proven | `forced-interruption-local-2026-08-05.json` |
| P21-08 | Twenty Mac runs at 1/3/5/10/20 plus Windows/Linux smoke | Partial | Mac 20-run, four-format soak passes; Windows/Linux independent smokes absent |
| P21-09 | Recovery/cache/one-heavy-model/cleanup/offline/corrupt recovery | Partial | all implementation tests and interruption evidence pass; the complete fresh-installed Mac lifecycle now passes under probed OS isolation, while four release targets remain external |
| P21-10 | README/setup/troubleshooting/capability/privacy/user guides | Proven | shipped documentation set and package inclusion |

## Acceptance gates

| ID | Gate | Current result | Exact remaining action |
|---|---|---|---|
| GATE-01 | Every Studio-selectable model produces real output where declared | Failed | Complete Fish's reviewed isolated runtime and current production smoke. Kokoro passes; MOSS is disabled with a proven, actionable resource incompatibility on this Mac |
| GATE-02 | MOSS cannot be ready from files/history alone | Passed in implementation | Three-part current-machine/current-revision readiness is enforced and tested |
| GATE-03 | Fresh no-CLI install/launch/generate/edit/listen/export/resume/delete | Passed on darwin-arm64 | the final hash-bound offline bundle installs into an empty environment outside the checkout and passes the full lifecycle under verified external-network denial; four independent target receipts remain missing |
| GATE-04 | Prompt-to-audio on Windows/macOS/Linux | Partial | darwin-arm64 passes the exact canonical suite under verified isolation; run full independent acceptance on the four remaining declared targets |
| GATE-05 | Basic without LLM | Passed locally | Lifecycle/unit evidence |
| GATE-06 | Typed model controls excluded from speech | Passed | Compiler, API and adapter contracts |
| GATE-07 | Exact silence/breath timing | Passed | Timeline/renderer/QC tests and soak artifacts |
| GATE-08 | Ten corpus: safety, median ≥4, minimum ≥3 | Failed | One or more humans must complete blind content and voice ratings for all ten cases; automatic safety already passes |
| GATE-09 | Portable/default/fallback/optional selections saved from Lab | Missing | Save blind ratings, distinct portable-default and low-resource-fallback LLM decisions, then the required writing, voice, and pacing promotions. A higher-quality LLM decision is optional |
| GATE-10 | Crash recovery preserves healthy work | Passed locally | Six-stage interruption evidence |
| GATE-11 | Twenty valid primary-machine exports | Passed | `soak-exports-2026-08-05.json` |
| GATE-12 | Install/infer/edit/evaluate/play offline | Partial | Mac installed and platform workflows pass under probed OS isolation; observe equivalent complete workflows on the four remaining independent targets |
| GATE-13 | No deferred cloud/public/mobile/ambience dependency | Passed | API negative-surface test and architecture/source audit |

## Exact remaining external and model gates

1. Collect blind content and voice ratings for every ten-corpus case; thresholds are median 4/5 and
   no required dimension below 3/5.
2. Save at least one blind rating, distinct accepted LLM decisions for the portable-default and
   low-resource-fallback roles, and accepted promotions for writing method, voice, and pacing.
   Higher-quality LLM and delivery decisions are optional additional evidence.
3. Produce complete independent acceptance reports on Windows x64, macOS ARM64, macOS x64, Linux
   x64, and Linux ARM64, with OS networking disabled after acquisition. CI portability receipts do
   not satisfy this gate.
4. Publish/approve the pinned Fish runtime inputs and target wheelhouse locks and run Fish
   production synthesis. MOSS 5B/8B and Qwen remain optional/model-release evidence requiring
   reviewed runtimes and compatible hardware; they cannot enter Studio without current synthesis
   or a declared, actionable incompatibility.
5. Repeat rendered Studio/Lab QA on the independently tested Windows, macOS x64, and Linux release
   targets. The development macOS ARM64 final bundle already has renewed digest-bound desktop and
   390x844 QA after the model-role UI change.

Until all five groups are evidenced, `local_v1_release_ready` must remain false.
