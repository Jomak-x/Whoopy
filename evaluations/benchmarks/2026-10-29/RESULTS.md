# October 29 local quality evidence

This update restores the local backend and adds original whole-session writing,
strict speech screening, and experimental MLX voices. Human voice preference,
comfort and teaching quality remain unreviewed. No Headspace equivalence is claimed.
The concurrent UI experiment is excluded from this branch.

## Retained listening sets

All large evidence stays local under `evaluations/benchmarks/`. The localhost
server on port 8772 serves that parent; the original port 8771 page remains available.
The morning hub will link explicit datasets rather than silently select the newest
folder or pool different prompt versions.

| Dataset | Measured result | Scope |
| --- | --- | --- |
| October 28 fixed-text auditions | 9 listed auditions: 7 Pass, 2 Review under strict word policy | Existing and new voice families; excluded failures remain visible separately |
| October 29 pilot, prompt v5 | 11 complete narrations: 2 Pass, 9 Review; 1 additional failed sleep generation | Two generated scripts, portable Michael and MLX Ryan/Aiden/Serena, plus deep male VoiceDesign |
| Final prompt v6, timing compiler v3 sleep | 299.999792 seconds; full-file Review, all 16 selected native segments Pass | Actual prompt-to-audio High Ministral + MLX Ryan |
| Final prompt v6 no-breath focus | 180.000083 seconds; full-file Review, all 9 selected native segments Pass | Actual prompt-to-audio High Ministral + MLX Ryan |

Pass means automated speech screening, not listener approval. Every normalized
word mismatch remains Review; major omissions, truncation, no speech, invalid
native completion and other hard failures remain Fail. Whisper receives no
expected-text prompt. Final typed timeline text supplies the comparison transcript.

The v6 grounding campaign initially failed on two validator false positives:
“lift your hands / shift your weight” was missed as movement, and “body touches
the seat” was missed as contact. The actual failed attempts remain unchanged.
The recognizers now accept those physical instructions while still rejecting
missing sound/contact/movement and metaphorical shifts. A fresh run must establish
the corrected result; historical failures are not relabeled.

## What changed in writing

Recipes supply beat purposes, user needs, posture, one selected anchor, reflection
and ending contracts. They contain no finished meditation prose. The model sees
the entire sequence and makes one complete draft plus at most one holistic revision.
Accepted prose stays intact. Duration-derived word budgets and directional
revision feedback prevent short drafts being padded with arbitrary silence.
Prompt v6 asks for real sensory observations, a single continuing practice,
permissive integration and a concrete daytime carryforward; sleep ends at rest.
Timing compiler v3 adds guided cycles at the selected articulation rate while
preserving per-gap quiet limits, breath exclusions, posture and completion guards.

Text review still found real weaknesses in accepted samples: vague sleep language,
a secondary sound cue not clearly framed as a fallback, assumptions about
independence, and weak carryforward. These are listening/editorial findings,
not evidence that structural acceptance proves excellent teaching.

## New local models and admission

MLX Qwen CustomVoice 1.7B 8-bit (Ryan, Aiden, Serena) and VoiceDesign 1.7B 8-bit
(deep male grounded preset) run through the durable CLI pipeline. Their exact
runtime and checkpoint bytes are pinned. They remain `experimental_local`,
`managed_ready=false`, and absent from web/API selectors. Native FLOAT audio,
actual attempts, EOS evidence and deterministic native-to-processed PCM checks
are retained. Cached repeated speech is bound by synthesis identity and PCM,
with original native IDs recorded separately from delivered timeline IDs.
Listening cards fail closed when playback bytes are missing or changed.

The High writer remains Ministral 3 14B Q4_K_M. Standard Qwen3 4B has not earned
whole-session baseline acceptance. An additional Qwen3.5 9B Q6_K writer was
installed from a pinned Apache-2.0-declared conversion: revision
`182be2fd6c7bc44887d88a91cb03ff009cc9f549`, SHA-256
`073a9275e65d9c8cd2819cf5f77b99fbaa6e87ba591da6bbaa86ec073a64bfef`.
Production High selection is unchanged. Legacy writing loaded and generated;
a following method was refused by the unchanged 16 GiB memory admission floor.
A separate constrained trial exposed llama.cpp b10142 sampler initialization
failure despite exit status zero. Replaying the exact runtime-generated grammar
through `--grammar-file` produced JSON. That transport experiment is not a
qualified production writer or a schema bypass. All failures and raw commands
remain retained. Converter provenance does not identify an exact upstream source
checkpoint commit, which is recorded as a limitation.

Independent Neon Claude Opus 4.8 review recommended the one-variable grammar
replay and withholding promotion. Its causal hypotheses remain hypotheses.
Upstream reports describe related issues:
[Qwen3.5 template/prefill report](https://github.com/ggml-org/llama.cpp/issues/21600)
and [schema versus explicit grammar report](https://github.com/ggml-org/llama.cpp/issues/29006).
The local failure and replay receipts are the evidence for this machine.

## Verification and delivery

The latest complete local source gate passed **1,346 tests**, Ruff formatting/lint,
and strict mypy before this documentation update. The follow-up GitHub gate must
bind its final revision and actual Linux/macOS/Windows Actions results; earlier
three-platform CI success at `6477f10361bdd28a38f34237e3253ddc50f87377` is historical.
The branch is a reviewable PR, not an automatic merge or release certification.

Review caught and fixed native cache-ID false failures, playback hash drift,
ordinary contact/movement and negated waking false rejections, empty Assistant
transcript handling, zero throughput metrics, and exit-zero sampler failures.
The earlier baseline fixes are listed in
[the review-fix record](../2026-10-28/REVIEW-FIXES.md).

Weights, virtual environments, generated audio, complete ASR logs, source snapshots,
recovery archives and local configuration remain excluded from publication.
The project source license remains undecided. Model/runtime/voice terms remain
separate; local success does not clear redistribution or future hosted use.

[Local generation controls](../../../docs/experimental-mlx-qwen.md) and
[benchmark/review commands](../../../docs/overnight-e2e-review.md) explain the
explicit CLI workflow. [Ambience research](../../../docs/ambient-models-2026-10-29.md)
recommends reliable procedural/CC0 assets first, with official MLX Stable Audio 3
Small-SFX as an optional gated-model trial. No gated terms were accepted and no
ambience model was promoted or added to the product.


## Follow-up review corrections after c31d4e3

Review found eleven remaining misses in ASR completion/coverage, publication of
full traces, control preservation across chunk splits, export source/artifact
integrity, failed sampler evidence, physical movement matching, numeric speed,
alternative-take admission, package inventory, and ignore-test semantics. All are
addressed in this source round; the detailed per-finding account is in the
[review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-c31d4e3).

Full ASR receipts are retained byte-for-byte in the ignored worktree directory
`.whoopy-recovery/asr-publication/`; public JSON retains compact status, metrics
and provenance with original receipt hashes. No original external-checkout
evidence or prior Git history was changed. Human listening remains pending.
The owner reports a fresh corrected grounding Pass; final campaign paths and
receipts still require the outer Document/PR phase's evidence reconciliation.
This review phase did not rerun inference or independently certify that result.

Focused verification ran 430 tests across the changed areas and their immediate
callers: adapters, benchmark retention, CLI, exports, package assessment/build,
overnight orchestration, recipes, revisions/admission/takes, web/legacy dispatch,
MLX integration, and compact publication receipts. The initial batch passed 428;
two new admission cases exposed a fixture missing its original take selection.
After correcting that setup without weakening assertions, both targeted cases
passed. The sole warning was the existing Starlette/httpx deprecation. Changed
Python files were formatted. No full test, lint, strict-type or CI gate ran in
this phase; those remain pending with the outer executor.


## Follow-up review corrections after 6fa81ef

Five additional misses were corrected: control-marker origin shifts after chunk
expansion, typographic negation, movement noun bypasses, raw rejected-output text
in public validation errors, and discarded invalid MLX returns. The
[review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-6fa81ef)
records their causes, corrections, and regressions. Invalid numeric MLX samples
are retained in immutable non-playable NPY files with failure metadata; they
cannot qualify as speech or valid native WAV/PCM evidence and remain ignored.

Focused verification passed **333 tests** across `test_recipe_routing.py`,
`test_recipe_whole_session.py`, `test_revisions.py`, `test_mlx_qwen3_tts_worker.py`,
`test_mlx_qwen_integration.py`, `test_publication_receipts.py`, and
`test_validation_privacy.py` in 17.69 seconds. An initial command named a nonexistent
adapter test file and ran no tests; the corrected focused batch passed. Touched
Python files were formatted. Full repository test/lint/type and CI gates remain
with the outer executor. No fresh inference or external receipt reconciliation
was performed here. The owner's completed live
campaign results remain bound to `c31d4e3` / `2e13ae4`; the outer Document/PR
phase must consume the final evidence summary and replace earlier pending states
without relabeling these historical receipts as final-pipeline inference.


## Follow-up review corrections after fe1ca3f

Six further findings were corrected: marker provenance across chunk expansion,
ordinary movement phrasing, required ending sound cues, requested VoiceDesign
identity, bounded export titles for detailed prompts, and timeout stream draining.
The [review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-fe1ca3f)
retains each finding and its regression coverage. The single focused verification
batch passed **345 tests in 28.99 seconds** across `test_revisions.py`,
`test_timeline.py`, `test_recipe_whole_session.py`, `test_evaluation.py`,
`test_exports.py`, `test_llama_timeout_capture.py`, `test_adapters.py`, and
`test_benchmark_attempt_retention.py`. The long-prompt cases exercised actual
four-format FFmpeg exports. Touched Python files were formatted.
No new live inference or external evidence reconciliation was performed in this
review phase; full test/lint/type/CI gates and final Document/PR reconciliation
remain with the outer executor.


## Follow-up review corrections after 520b450

Four selected findings were corrected: marker correspondence across combined
paragraph splits and control changes, independent breath-focus exclusions in
compound requests, verified extraction integrity before cached-artifact reuse,
and stable cross-process ownership through run deletion/restoration.
The [review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-520b450)
retains the finding IDs, root causes and executable regressions.

Focused verification covered 444 distinct tests. The initial batch passed 441
and exposed one negation-span boundary plus two invalid test fixtures. After
correction, all 227 affected routing, whole-session and ownership tests passed;
the other 217 had passed in the initial batch. The ownership tests used spawned
processes and actual worker resume and directory moves on this host. Native
Linux/Windows execution and full repository test/lint/type/CI gates remain with
the outer executor. No new live inference or external evidence reconciliation
was performed; historical model results retain their actual producer revisions.


## Follow-up review corrections after f443aad

Four selected findings were corrected: repeated authored-marker correspondence,
compound breath exclusions in either item order, shared ownership throughout CLI
plan regeneration, and independent original/listening-copy audio hash checks.
The [review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-f443aad)
records the finding IDs and executable regressions.

The single focused batch passed **372 tests in 32.87 seconds** across revisions,
timeline compilation, regeneration/deletion ownership, admission, recipe routing,
whole-session generation and listening-page evidence. The marker oracle checked
1,600 short-stream pairs against all optimal ordered alignments. Ownership tests
used spawned processes and actual CLI/revision/worker boundaries with deterministic
generation fixtures. Touched Python files were formatted.

No live inference, raw-evidence publication, external receipt reconciliation or
full repository test/lint/type/CI gate was performed in this review phase. Native
Linux/Windows checks and final Document/PR reconciliation remain with the outer
executor; historical model evidence retains its actual producer revisions.

## Follow-up review corrections after 95531c4

Five selected findings were corrected: repeated-prose control preservation with
safe ambiguity rejection, independent sleep/breath clauses, current Fail audition
cards with immutable report history, optional ratings for rejected comparisons,
and process-tree cleanup on forced-interruption timeout or exception.
The [review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-95531c4)
records finding IDs, root causes and executable regressions.

No new live inference or external receipt reconciliation was performed. Historical
model evidence retains its actual producer revisions. Full repository gates,
native Linux/Windows checks and final Document/PR reconciliation remain with the
outer executor.

The single focused batch passed **442 tests in 51.25 seconds**, covering the
changed revision, routing, listening-report and release paths. Timeout/exception
regressions used synthetic ordinary and detached child processes and verified
cleanup before the next case. Touched Python files were formatted; no full
repository test/lint/type or CI gate ran in this phase.

## Follow-up review corrections after 60c9eed

Three selected findings were corrected: split-prose control preservation with
safe structural/timing rejection, independent subject clauses in sleep/breath
routing, and benchmark-local listening-report filenames with valid relative
links. The [review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-60c9eed)
records the finding IDs, root causes and executable regressions.

Historical model evidence retains its actual producer revisions. No live
inference, external receipt reconciliation or publication was performed in this
review phase. Full repository gates, native Linux/Windows checks and final
Document/PR reconciliation remain with the outer executor.

Focused verification covered **413 distinct tests**. The combined batch passed
412 tests in 36.79 seconds and exposed one older ownership fixture relying on
an implicit delivery reset. After making that reset explicit, all four cases of
the affected ownership test passed in 0.40 seconds, preserving its assertions.
Touched Python files were formatted. No full repository test/lint/type or CI
gate ran in this phase.

## Follow-up review corrections after bba3104

Six selected findings were corrected: noun-subject request clauses, coordinated
negated waking actions, paragraph/quiet-bound identity, plan-input admission
rollback, alternative-take completion visibility, and packaged-worker acceptance
inventory including MLX. The [review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-bba3104)
retains finding IDs, root causes and executable regression coverage.

Focused verification passed **462 tests in 13.14 seconds**. Coverage includes
clause routing, paragraph validation and quiet recovery, plan-input failures,
alternative-take finalization and restart failures, selected-audio/export access,
admission/ownership and wheel worker resolution. Touched Python files were
formatted; the only test warning was the existing Starlette/httpx deprecation.

No new live inference, raw-evidence publication or external receipt reconciliation
was performed. Historical model evidence retains its actual producer revisions.
Full repository gates, native Linux/Windows checks and final Document/PR
reconciliation remain with the outer executor.

## Follow-up review corrections after 92d3005

This review narrows sleep exclusions and edited-paragraph recovery, rejects
compiler-discarded generated prose, persists alternative-take finalization
across restart, removes stale task-error overrides, and binds acceptance
execution to its selected directories. Manual recipe script revisions preserve
canonical pauses and record actual rendered duration; original drafts remain
immutable. The [review-fix record](../2026-10-28/REVIEW-FIXES.md#review-fixes-after-92d3005)
retains all six finding IDs and behavioral coverage.

Focused verification covered **710 distinct tests**, all passing across the
initial batch and targeted reruns. Initial results were 701 passed and nine
fixture failures in 85.14 seconds; the affected rerun passed 53 with four remaining
fixture counter errors in 17.56 seconds, and the final four passed in 0.93 seconds.
The corrections preserved assertions and retained evidence; no source changes
followed the initial batch. Touched Python files were formatted. The only warning
was the existing Starlette/httpx deprecation.

No new live inference or external receipt reconciliation was performed here.
Historical model evidence retains its actual producer revisions. Full repository
gates, native platform validation and final Document/PR reconciliation remain
with the outer executor.
