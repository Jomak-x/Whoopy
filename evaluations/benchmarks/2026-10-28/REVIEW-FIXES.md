# Review fixes after 5abe0d8

This report covers the assigned review phase only. Final repository test/lint,
strict typing, and GitHub CI gates remain pending with the outer executor. No
post-fix model inference or ASR was run, and these source changes do not certify
the historical benchmark recordings.

| Finding | Root cause and fix | Behavioral regression |
| --- | --- | --- |
| REVIEW-01 | Alternative finalization followed a run ID across task replacement. It now owns the original task, abandons cancellation/supersession, and serializes restoration with later selection. | `tests/test_takes.py`: abandoned operations, ownership recheck before commit, successful restoration, and selection/finalization ordering. |
| REVIEW-02 | Completion persistence assumed the last result was updated and retained resolved resume entries. Completion now carries the exact result identity and clears its resume entry when resolved. | `tests/test_evaluation.py`: two pauses/resumes with reordered persisted results; completed evidence remains successful and the resume map empties. |
| REVIEW-03 | Saved trial results bypassed the failed-baseline gate. Saved and newly executed results now share the same controlled-comparison stop. | `tests/test_evaluation.py`: saved failed baselines never launch challengers on voice, delivery, or pacing resumes. |
| REVIEW-04 | Quiet-limit matching used raw text and only the final speech chunk. It now compiles authored paragraphs and matches their complete canonical chunk sequences. | `tests/test_recipe_whole_session.py`: plain, emphasized, and split paragraphs preserve 7,999/16,000/22,000 ms bounds through pacing and real-speech fitting. |
| REVIEW-05 | Failed active-pointer publication left an allocated but unpublished revision. Cleanup now rolls back that revision and newly created empty directories while preserving an already published pointer. | `tests/test_voice_references.py`: initial/replacement publication failures before and after commit, followed by usable retries. |
| REVIEW-06 | Qwen smoke omitted the existing hardware requirement. Its adapter now receives the pack requirement and applies the immediate-before-load guard. | `tests/test_model_pack_smoke.py`: memory refusal before runtime construction for CustomVoice and VoiceDesign. |
| OWNER_STRICT_AUDITION_FINAL | The earlier aggregate-based manifest hid Vox's full-file disagreement behind Pass. The builder now defaults to the strict sidecar, accepts explicit manifest/output paths, and labels the retained disagreement without asserting missing speech. | `tests/test_listening_review.py`: status/links, Vox explanation, explicit manifest selection, and read-only evidence preservation; actual-recording page validation described below. |

## Current audition evidence

Only two new prepared JSON sidecars were copied into this publication:
`verification/policy-v3/voxcpm2-designed-male-strict-review.json` and
`verification/listening/manifest-policy-v3-strict.json`. Earlier v3 receipts and
manifests remain unchanged. The current 12 samples are **7 Pass / 5 Review**:
seven voice Pass results, two voice Reviews, and three unchanged writing Reviews.

Vox's full-file recognizer omitted 28 expected tail words. This is an unresolved
recognition disagreement, **not proof that speech is missing**. Exact independent
paragraph recognition and PCM concatenation remain positive evidence, linked
alongside the original full-file failure and the new Review receipt. No new
recognition, acoustic inspection, or audio transformation was performed here.

The generated `review.html` is intended to sit beside the complete local benchmark
tree. This publication contains HTML/JSON only; its relative media links require
the retained local recordings. Original recordings and receipts are read-only
inputs to page validation, never output destinations.

## Validation

The focused regression batch passed **211 tests** across `test_takes.py`,
`test_evaluation.py`, `test_lab_execution_boundaries.py`,
`test_recipe_whole_session.py`, `test_voice_references.py`,
`test_model_pack_smoke.py`, `test_listening_review.py`,
`test_legacy_api_contract.py`, and `test_webui_review_boundaries.py`. The sole
warning was the existing Starlette/httpx deprecation. Changed Python files were
formatted; the complete test/lint/type and CI gates were not run in this phase.

The local builder generated `review.html` using the original benchmark directory
as read-only input and this worktree as its output destination. HTML parsing
confirmed nine voice and three writing cards, **7 Pass / 5 Review**, Vox's Review
label and recognition-disagreement explanation, and all **64 media/evidence
links** resolving against the original local tree. All 18 voice raw/listening
audio hashes and three writing audio hashes matched their manifests. The two
copied JSON files were byte-identical to their originals, and the other eight
voice records matched the earlier manifest. Hash, size, and modification-time
snapshots of all **63 referenced original files** were identical before and after
page generation. No media was copied into the publication.

`git diff --check` passed, and the frontend trees had no differences from the
original base commit. Final CI remains pending with the outer executor.

R51 speed behavior and REVIEW-07 prebuilt runtime behavior remain unchanged while
awaiting owner decisions. Frontend trees remain at the original base. Whoopy's
source license remains **UNDECIDED**, and the full runtime-bundle GPL
redistribution review remains incomplete.

## Review fixes after b67a27d

This subsequent review phase addresses only the selected findings below. The
earlier validation results above belong to the previous phase. No historical
audio, inference report, or ASR receipt is regenerated or replaced.

| Finding | Root cause and fix | Focused verification |
| --- | --- | --- |
| REVIEW-08 | The acceptance timeout owned only the wrapper process. The command boundary now terminates and waits for descendants, including a detached child, before reaping the wrapper and continuing. | Disposable-process regression in `tests/test_release_acceptance.py`, including a child that ignores termination. |
| REVIEW-09 | Lab restart discarded the run association before trial completion was durable. Trial/run ownership is now persisted before launch and retained through result persistence; resume reconciles the existing run instead of starting a new trial. | Restart and completion regressions in the Lab/evaluation tests. |
| REVIEW-11 | A durable purge transaction incorrectly required the original digest after destructive progress. Retry now uses its persisted authorization; initial integrity and receipt/path safety checks remain. | `tests/test_legacy_cleanup.py`: partial directory deletion and redirected-payload refusal, using disposable fixtures only. |
| REVIEW-12 | The baseline guide presented earlier tolerant screening as current qualification. It now links strict voice/writing sidecars and reports 7 Pass / 5 Review across 12 auditions, retaining earlier results as historical. | Compare parsed strict-sidecar classifications and guide links. |
| AUDIT_MATRIX_SCOPE | The Kokoro matrix described primary code/model terms without its known runtime dependencies. It now records eSpeak/phonemizer evidence, incomplete desktop redistribution/source closure, and separate source/model/runtime rights. | Parse matrix semantics; preserve declared primary-code/model license IDs and unrelated candidate rows. |

The matrix uses the existing local audit's owner-supplied dependency observations.
The pinned [eSpeak 1.52.0 README](https://raw.githubusercontent.com/espeak-ng/espeak-ng/1.52.0/README.md)
and [GPL text](https://raw.githubusercontent.com/espeak-ng/espeak-ng/1.52.0/COPYING)
were rechecked for license identification and the distinction between running,
network interaction, and conveying copies. This is not new installed-wheel
source-closure verification or legal clearance. Whoopy source remains UNDECIDED.

Focused verification passed **179 tests** across `test_release_acceptance.py`,
`test_legacy_cleanup.py`, `test_evaluation.py`, `test_lab_execution_boundaries.py`,
`test_lab_recipe_attestation.py`, `test_legacy_api_contract.py`, and
`test_webui_review_boundaries.py`. This includes both inherited-session and
detached-child timeout cases, six live/completed/interrupted restart scenarios,
two prelaunch ownership cases, and disposable partial-purge/path-safety fixtures.
The only warning was the existing Starlette/httpx deprecation.

Parsed strict sidecars confirmed seven voice Pass results, two voice Reviews,
three writing Reviews, the writing edit counts, and Vox's 28-word disagreement.
The guide's classification total and local links matched those retained records.
Parsed matrix checks confirmed incomplete desktop redistribution/source closure,
UNDECIDED Whoopy licensing, unchanged declared model/primary-code license fields,
and unchanged unrelated candidate rows. Changed Python files were formatted;
`git diff --check` passed. Frontend trees match the original base, and no raw
audio, model payloads, or verification receipts changed in this phase.

Final repository test/lint/type and GitHub CI gates remain pending with the outer
executor. No new model or ASR inference was performed.
R51 speed, REVIEW-10 explicit pause priority, and REVIEW-07 prebuilt-mode behavior
remain unchanged while awaiting owner decisions.

## Review fixes after 4af8bfb

The owner has now approved explicit speed/pause precedence and removal of the
unused prebuilt runtime format. The earlier pending-decision statements above
describe their respective historical review phases.

| Finding | Root cause and fix | Focused verification |
| --- | --- | --- |
| REVIEW-13 | Admission relied on volatile web tasks and missed surviving workers after restart. Shared generation/model-operation admission now consults durable run ownership and reconciles stale owners. | Durable ownership, stale-run recovery, and cancellation regressions. |
| REVIEW-14 | Recovery started a new clock and reported it as the original trial duration. Recovered trials now omit unavailable total elapsed timing while retaining a separate polling timeout. | Completed/live/interrupted recovery and comparison exclusion regressions. |
| REVIEW-15 | Checkout line endings depended on user Git settings. Restored the global LF rule while retaining byte-preserved license snapshots. | Actual Git checkout with `core.autocrlf=true` verifies Python/YAML bytes and untouched CRLF license snapshots. |
| OWNER_APPROVED_RUNTIME_AND_PACING | Removed the unused prebuilt runtime bypass, retaining locked-wheelhouse and Kokoro's separate native archive path. Explicit speed applies without an explicit pacing profile; later explicit pause edits preserve their accepted timing and report rendered duration. Fresh generated runs retain fitting. | Runtime schema/materialization, speed precedence, edited-pause rendering, and fresh-generation regressions. |

The focused batch covered **216 cases** in `test_durable_admission.py`,
`test_lab_recipe_attestation.py`, `test_runtime_packs.py`,
`test_runtime_release_validation.py`, `test_ci_portability.py`, `test_cli.py`,
`test_webui.py`, `test_webui_review_boundaries.py`, `test_revisions.py`,
`test_worker.py`, `test_lab_pacing_provenance.py`, and `test_legacy_api_contract.py`.
Initially 215 passed; the remaining test correctly observed duplicate-resume
rejection but expected an older error message. Its assertion was updated and its
blocked fixture now releases in `finally`; that case passed on focused rerun.
The first batch also emitted the existing Starlette/httpx deprecation and a
fixture-thread timeout caused by that assertion failure; the rerun was clean.

The regressions exercise live durable ownership, stale recovery, recovered
comparison metrics, Git's actual Windows-style checkout conversion, wheelhouse
installation, API-to-CLI speed precedence, planning resume, pause render/restore,
selective speech regeneration, WAV/manifest agreement, and fresh duration fitting.
Changed Python files were formatted, and `git diff --check` passed. No historical
benchmark receipt or media changed, and no model inference or real-data purge
was performed. Frontend trees match the original base and source licensing
remains UNDECIDED. Final repository test/lint/type and GitHub CI gates belong to
the outer executor.

## Review fixes after 1d000c8

| Finding | Root cause and fix | Behavioral regression |
| --- | --- | --- |
| REVIEW-16 | Script compilation reset the persisted explicit-timing policy. Wording revisions now inherit that policy from the previous timeline. | `tests/test_revisions.py`: accepted 1,000 ms pause, wording edit, rerender, WAV/manifest duration agreement, and prior revision restoration. |
| REVIEW-17 | Mutable Lab control storage duplicated directory-fsync logic without Windows support. It now uses `RunStore._fsync_directory`, retaining file flush and atomic replacement. | `tests/test_lab_execution_boundaries.py`: simulated Windows directory-open refusal still permits durable state updates and worker dispatch. |
| REVIEW-18 | Soak verification bound exports to current narration but not to the qualified recording. It now also requires the production receipt's audio SHA-256 before counting a case. | `tests/test_soak_exports.py`: intact 20-case synthetic corpus, replaced audio with matching exports, missing/null hashes, and byte-identical retained receipts. |

The focused batch passed **115 tests** in `test_revisions.py`,
`test_lab_execution_boundaries.py`, `test_lab_evidence.py`, `test_evaluation.py`,
and `test_soak_exports.py`. It emitted one existing Starlette/httpx deprecation
warning. Windows directory-open behavior was simulated; this was not a native
Windows run. Tests used synthetic audio or disposable fixtures; no real
inference, purge, or historical evidence replacement was performed.
`git diff --check` passed, and frontend trees still match the original base.
Source licensing remains unchanged. Final repository test/lint/type and GitHub
CI gates remain with the outer executor.

## Review fixes after 70e9ad3

| Finding | Root cause and fix | Behavioral regression |
| --- | --- | --- |
| REVIEW-19 | Timeline pause edits left stale controls in the stored editable script. Revision publication now synchronizes edited pauses through the compiler's marker mapping and honors subsequent explicit script pause changes. | Read the stored script after a 1,000 ms edit, change only prose, render and restore; verify accepted pauses and WAV/manifest duration. |
| REVIEW-20 | Edit, restore, and take selection mutated persisted state before checking generation admission. These operations now hold shared admission from before mutation through worker registration. | Busy model/generation ownership leaves run files byte-identical; retry succeeds after release. Concurrent admission stays blocked until the revised run registers its worker. |
| REVIEW-21 | Soak verification omitted the export loader's exception from per-case failures. Missing or damaged manifests now yield failed cases while the remaining corpus is checked. | CLI produces a fresh failure report for two damaged cases, counts the other 18, and preserves every receipt byte. |
| QUICKSTART_QUALIFICATION_SCOPE | The first generation example used the failed five-minute sleep request. It now uses the measured three-minute daytime grounding request, with the retained sleep failure linked and explicitly described as experimental/unqualified. | Source documentation was checked against the retained writing benchmark results; no inference was run. |

The focused batch covered **141 tests** in `test_revisions.py`,
`test_revision_admission.py`, `test_timeline.py`, `test_takes.py`,
`test_webui_review_boundaries.py`, `test_durable_admission.py`, `test_webui.py`,
and `test_soak_exports.py`. Initially 140 passed; one existing take-finalizer
fixture mocked the public resume entry point rather than the new internal
admitted entry point. Updating that fixture's mock made the failed case pass on
its targeted rerun. Both runs emitted the existing Starlette/httpx deprecation.
`git diff --check` passed and frontend trees match the original base.

Tests use disposable fixtures and synthetic speech only. Historical evidence and
source licensing remain unchanged; no real model inference or real-data purge
was requested or performed. Final repository test/lint/type and GitHub CI gates
belong to the outer executor.


## Focused test-phase fixes after b0f4130

This phase fixes the six defects explicitly reintroduced by the owner's later
`REINTRODUCED_REVIEW_22_27` decision. It does not undo earlier pipeline fixes or
reinterpret the earlier declined entries as the final decision. Only targeted
behavioral tests ran; `scripts/check.py`, full-suite testing, lint, formatting,
static analysis, publishing, and CI remain with the outer executor.

| Finding | Reproduction and root-cause correction | Focused result |
| --- | --- | --- |
| REVIEW-22 | An actual wheel built with disposable synthetic `config/local.yaml` included that private override. Replaced the broad config glob with the explicit shipped config allowlist. Replaced the declaration-string test with a real wheel build, archive inspection, and loading of the emitted settings. No real local settings were copied. | Failed before the fix; passed after. The wheel excludes `local.yaml`, retains product resources, and loads `af_heart` instead of the synthetic private voice. Build used existing cached tooling with `UV_OFFLINE=1`; no package or model download. |
| REVIEW-23 | Recompiling the editable script reset accepted canonical controls and fitted pauses. For unchanged segment ID/type topology, a three-way merge now retains unchanged controls, applicable pronunciation overrides, and fitted pauses. Compiler marker origins distinguish newly explicit default resets from unchanged implicit defaults. | Three new cases passed after reproducing the control/pronunciation failures and the explicit `natural` reset failure. Actual fixture-backed worker rendering, wording edit, and restoration preserve controls, pause durations, WAV/manifest agreement, and restored PCM. Nine existing pause/control/compiler cases also passed. Structural insertion/removal does not infer control associations across changed topology. |
| REVIEW-24 | Alternative generation stopped counting as an owner when its subprocess ended, before the polling finalizer restored the selected take. A task reservation now lasts through finalization and admitted assembly-worker registration, and is released on cancellation/exit. | New tests reject generation/model/deletion contenders in the completed-worker gap, retain original selected PCM, register the assembly worker, and release a cancelled reservation. Existing successful, abandoned, and ordered-finalization cases passed. |
| REVIEW-25 | Deletion checked persisted status without serializing against worker registration. It now holds application admission, checks active/finalizing local tasks, and probes durable worker ownership before moving a stopped run to trash. | New cases cover registered/interrupted and completed-but-active tasks, an existing durable worker lock, deletion waiting for registration, and successful retry after ownership ends. The durable lock is released before rename for Windows compatibility; application admission remains held. This serializes local API work and rejects an already-active external owner, without claiming atomic exclusion of a newly launched external CLI worker after the probe. |
| REVIEW-26 | Writing cards trusted historical screening status without checking the referenced recording. The builder now requires the writing audio to exist and match its declared `audio_sha256` before publishing output. | Both changed-audio and missing-audio cases failed before the fix and passed after; prior page and receipt bytes remain unchanged on rejection. |
| REVIEW-27 | Positive sleep/box-breathing matching consumed explicitly negated clauses. Negated clauses are removed before those positive checks, with punctuation and contrast-word boundaries preventing one exclusion from suppressing the next request. | Awake and natural-breathing requests now route correctly. Six mixed-clause regressions reproduced failures before tightening clause boundaries; nine focused cases passed, including the existing positive box-breathing rejection. |

### Targeted validation

Final focused verification passed **39 cases across separate targeted commands**:

- `UV_OFFLINE=1 .venv/bin/python -m pytest -q tests/test_locations.py::test_built_wheel_contains_product_data_without_local_settings` — 1 passed.
- `tests/test_revisions.py::test_wording_revision_preserves_rendered_controls_and_fitted_pauses` and `::test_new_script_controls_override_accepted_controls` — 3 passed.
- Existing revision cases `test_generated_pause_edit_renders_and_restores_exact_timing`, `test_wording_edit_preserves_accepted_generated_pause_timing`, `test_explicit_script_pause_change_overrides_generated_timing`, `test_pause_edit_updates_compiled_markers_without_changing_ignored_text`, and `test_pace_edit_resynthesizes_only_changed_speech`, plus timeline cases `test_markdown_script_compiles_to_bounded_speech_and_exact_pauses`, `test_delivery_controls_are_typed_and_never_enter_spoken_text`, and `test_unknown_or_unsupported_delivery_control_is_rejected` — 9 passed.
- `.venv/bin/python -m pytest -q tests/test_revision_admission.py -k 'alternative_take or delete_'` — 8 passed.
- `.venv/bin/python -m pytest -q tests/test_takes.py -k 'alternative_finalizer or alternative_generation'` — 7 passed.
- `tests/test_listening_review.py::test_page_refuses_damaged_writing_audio_and_preserves_existing_page` — 2 passed.
- Eight new routing cases in `tests/test_recipe_routing.py` and `tests/test_recipe_whole_session.py::test_explicit_box_request_reports_unsupported_reviewed_protocol` — 9 passed.

One intermediate ownership test incorrectly marked an invalidated fixture
completed without the required artifacts. It was corrected to use the pristine
completed fixture; the final focused cases passed. One existing finalizer test
mock was updated to the internal admitted resume entry point. These fixture
corrections are separate from the reproduced product failures. Rendering checks
use deterministic synthetic speech, not a real voice model.

### Live qualification remains incomplete

`TEST-LIVE-QUALIFICATION` and the live portions of `test-2` remain **inconclusive**.
Real inference is now authorized, superseding the earlier no-inference reason.
However, this assigned phase explicitly confines project reads/writes to this
worktree and forbids accessing another checkout. The requested pinned model and
ASR environment paths are in the original checkout, and the requested benchmark
output is also outside the permitted worktree. Those paths were not accessed.
No model assets or working ASR environment are available within this worktree.

A fresh actual CLI attempt requested HIGH, `recipe_whole_session`, Kokoro,
three-minute grounding, and seed 42 using this worktree's model path and an
allowed test-evidence run directory. It exited **2** before inference with:
`Artifact kokoro_multilang_v1_0 is not safe to load: The verified download is not installed.`
The command, complete stdout/stderr, producer hashes, elapsed time, and before/
after memory and swap samples are retained at
`/Users/jakob/.no-mistakes/evidence/01M62HVTDGAVV26ASJGAYEXNQ8/postfix-test-phase/generation-preflight.json`,
with the failed run state beside it. This is a preflight refusal, not an audio
quality result or a completed benchmark. No expected speech or ASR result was
invented; human listening remains pending. No original data was archived or
purged, and historical receipts were not relabeled as current certification.

The outer executor still needs a boundary-compatible live generation/ASR phase
with the pinned assets, actual attempt/resource evidence, and subsequent human
listening. Deterministic regression passes do not resolve that qualification.
Frontend trees remain byte-identical to the original base. No source license,
model payload, audio payload, or recovery archive was added. Existing prepared
dependencies were preserved; transient test bytecode/cache files were removed.
