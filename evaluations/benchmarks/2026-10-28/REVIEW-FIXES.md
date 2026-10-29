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


## Review fixes after c31d4e3

These eleven misses were confirmed against the current implementation before
editing. This round preserves the existing frontend exclusion, prior fixes,
source-license decision, and local model/evidence boundaries. No live inference,
paid API call, pipeline control, publication, or CI phase ran here.

| Finding | Root cause and narrow correction | Behavioral regression |
| --- | --- | --- |
| REVIEW-INCOMPLETE-ASR | Qualification defaulted to Pass for nonterminal results and omitted native coverage checks. Receipt loading now requires terminal process/report outcomes; reduction requires every selected segment exactly once. Incomplete reports remain intact. | `test_overnight_e2e_runner.py`: interrupted, incomplete, mismatched exit, missing and duplicate coverage. |
| REVIEW-ASR-PUBLICATION | Full local verification payloads crossed the publication boundary. Tracked receipts now contain compact results/provenance; exact originals are retained in the ignored worktree recovery directory, identified by SHA-256. | `test_publication_receipts.py`: semantic compact-artifact contract and preserved disagreement/proof references. |
| REVIEW-CONTROL-PRESERVATION | Control merging depended on identical complete topology. Ordered segment correspondence preserves accepted controls on unchanged segments across unrelated chunk splits, while explicit replacements win. | `test_revisions.py`: edit/render/restore with a split later paragraph, unchanged opening/ending controls and fitted pauses. |
| REVIEW-EXPORT-SOURCE-RACE | Each format read a mutable master independently. Exports now share one frozen source and refuse activation if the master changes. | `test_exports.py`: same-duration source replacement between encodes, identical frozen bytes, prior export preserved. |
| REVIEW-EXPORT-ARTIFACT-INTEGRITY | Export resolution trusted manifest metadata without checking served artifacts. Listing/download now require recorded sizes and hashes for exported files. | `test_exports.py`: same-size replacement, truncation and removal, preserving the receipt. |
| REVIEW-SAMPLER-EVIDENCE | Adapter failure preceded durable writing-attempt capture. Both writer methods now retain immutable command, prompt/schema, streams and partial output before temporary cleanup; public errors omit raw diagnostics. | `test_adapters.py`: composed sampler, timeout, nonzero-exit and empty-output failures, with repeated attempts. |
| REVIEW-PHYSICAL-MOVEMENT | An unrestricted movement verb accepted metaphorical endings. Movement/stretch matches now require physical body targets. | `test_recipe_whole_session.py`: reject moving into the day or stretching attention; accept body, hands, fingers, arms and feet. |
| REVIEW-UNSUPPORTED-SPEED | Unsupported backends discarded explicit speed after it changed pacing selection. Shared CLI validation rejects it before model setup; backend launchers emit numeric speed only for Kokoro. | `test_cli.py`, `test_webui.py`: every unsupported backend plus preserved Kokoro and legacy MOSS/Qwen launches. |
| REVIEW-TAKE-ADMISSION | Alternative capture/selection preceded admission. Admission now encloses those mutations through worker registration and existing finalizer ownership. | `test_revision_admission.py`: blocked model/run requests preserve selection and Undo; retry registers ownership. |
| REVIEW-PACKAGE-INVENTORY | A second package manifest drifted from the actual build declarations. Inventory and wheel destinations now derive from explicit setuptools data-file declarations. | `test_locations.py`: emitted wheel/sdist assessment, synthetic local settings excluded, changed MLX worker/guide detected. |
| REVIEW-IGNORE-TEST-SEMANTICS | A byte assertion did not establish exclusion behavior. The test now invokes Git's ignore consumer in a disposable repository. | `test_benchmark_attempt_retention.py`: generated reports/raw attempts ignored; publication control remains visible. |

Focused verification for this round is recorded in the October 29 results file.
Full test/lint/type gates and final-revision native CI remain the outer executor's
responsibility. Compact receipts are historical results, not fresh certification.


## Review fixes after 6fa81ef

The five follow-up findings were confirmed in the current code before editing.
The previous fix commits and all recorded evidence remain preserved.

| Finding | Root cause and correction | Behavioral regression |
| --- | --- | --- |
| REVIEW-CONTROL-ORIGIN-SHIFT | Control provenance compared absolute speech positions. Marker boundaries now use speech correspondence across chunk changes, retaining accepted controls only when the origin corresponds and honoring replacements. | `test_revisions.py`: marked ending after earlier paragraph expansion, rendered controls, restore and explicit marker changes. |
| REVIEW-UNICODE-NEGATION | Two exclusion patterns differed from existing apostrophe handling. Negative sleep and box-breathing requests now accept ASCII and typographic apostrophes consistently. | `test_recipe_routing.py`: exact awake/natural-breath counterexamples and separate positive clauses. |
| REVIEW-MOVEMENT-NOUN-BYPASS | A bare finger/toe alternative bypassed physical-action validation. That branch was removed; fingers/toes now require the same action-and-body-target matching as other body parts. | `test_recipe_whole_session.py`: resting finger/toe/body nouns rejected, actual move/stretch/wiggle/flex/curl invitations retained. |
| REVIEW-RAW-VALIDATION-ERROR | Public error text reused rich rejected-output exceptions. Public diagnostics now contain sanitized schema locations/types and safe guidance; full exceptions and rejected output remain in local attempt evidence. | `test_validation_privacy.py`: schema values, arbitrary extra/duplicate keys, CLI/run-summary errors, checkpoint rejection and local detailed evidence. |
| REVIEW-MLX-INVALID-OUTPUT-RETENTION | Validation rejected returned arrays before evidence ownership. Invalid numeric returns now persist as immutable non-playable NPY arrays with failure metadata and hashes, then fail closed without a valid WAV/PCM receipt. | `test_mlx_qwen3_tts_worker.py`: invalid values, shape/rate, earlier yields and repeated-attempt immutability; `test_publication_receipts.py`: Git excludes raw NPY evidence. |

This phase performs no live model/ASR generation, publication, pipeline control,
or full repository gate. Latest campaign receipts remain tied to their actual
producer revisions (`c31d4e3` / `2e13ae4`), not this follow-up source revision.
Document/PR reconciliation of the final external evidence summary and final CI
is owned by the outer executor. Human listening remains pending.


Focused verification: **333 passed in 17.69 seconds** across recipe routing,
whole-session generation, revisions, MLX worker/integration, publication contracts,
and validation privacy. The first invocation contained a nonexistent test target
and ran no tests; the corrected batch passed. Touched Python files were formatted;
full test/lint/type gates and CI were not run in this review phase.


## Review fixes after fe1ca3f

All six selected findings were checked against current source before editing.

| Finding | Root cause and correction | Behavioral regression |
| --- | --- | --- |
| REVIEW-CONTROL-ANCHOR-SPLIT | Marker provenance depended on the first compiled speech chunk in its scope. Correspondence now uses authored paragraph/control provenance independently of chunk boundaries, retaining explicit changes and conservative scope handling. | `test_revisions.py`: controls before/after expansion, multiple later paragraphs, independent control changes, duplicate content/markers and explicit resets. |
| REVIEW-MOVEMENT-ARTICLE-REGRESSION | The action/object grammar excluded ordinary articles and intransitive stretching. It now accepts physical invitations such as wiggle a finger, lift an arm, move both hands and stretch a little, while excluding bare nouns and metaphorical objects. | `test_recipe_whole_session.py`: accepted prose remains exact; invalid endings still exhaust only the permitted two attempts. |
| REVIEW-ENDING-SOUND-BYPASS | Room/surroundings nouns substituted for auditory practice. The ending now requires hearing/listening or an explicit attention-to-sound cue. | `test_recipe_whole_session.py`: room-only, visual surroundings and adjective-only sound examples fail. |
| REVIEW-LAB-VOICEDESIGN-IDENTITY | Launch and attestation independently derived a voice identity from delivery. The redundant mapping was removed; both consume the requested preset. | `test_evaluation.py`: warm and grounded presets retain distinct identities under identical calm delivery; wrong recorded preset/name fails attestation. |
| REVIEW-EXPORT-LONG-PROMPT | The unbounded prompt was passed into bounded display metadata. Export now derives a whitespace-normalized title capped at 300 characters, preserving the full run prompt and record. | `test_exports.py`: real four-format exports at 300, 301 and 20,000 prompt characters, verified embedded titles and unchanged records. |
| REVIEW-TIMEOUT-STREAM-RETENTION | Timeout evidence was frozen before reader draining. Timeout exceptions now capture streams after bounded joins and record per-stream incompleteness when draining fails. | `test_llama_timeout_capture.py`: deterministic trailing output, blocked readers and reader errors persisted through the real capture boundary. |

This assigned review phase does not run live inference, publish raw evidence,
change UI/source licensing, invoke pipeline controls or execute the outer gates.
The final external campaign summary remains an input for the later Document/PR
phase, bound to the actual producer revisions rather than this source update.


Focused verification: **345 passed in 28.99 seconds** across revisions, timeline
compilation, whole-session generation, Lab evaluation, real exports, timeout
capture, adapters and benchmark attempt retention. Touched Python files were
formatted. No full test/lint/type or CI gate ran in this review phase.

## Review fixes after 520b450

All four selected findings were confirmed against the current source.

| Finding | Root cause and correction | Behavioral regression |
| --- | --- | --- |
| REVIEW-CONTROL-PARAGRAPH-SPLIT | Partially changed marker streams still depended on paragraph correspondence. Unique unchanged values now establish per-control marker identity independently of paragraph splits, while explicit replacements and ambiguous duplicate resets remain authoritative. | `test_revisions.py`: combined paragraph split and unrelated marker change, multiple later paragraphs, explicit replacement, duplicate reset, rendering and restoration. |
| REVIEW-COMPOUND-BREATH-EXCLUSION | Removing a negated box-breathing span discarded an independent breath-focus exclusion inside it. Routing now evaluates that independent exclusion before removing the span. | `test_recipe_routing.py` and `test_recipe_whole_session.py`: compound exclusions retain an external anchor through generation, while declining box breathing alone permits natural breathing. |
| REVIEW-CACHED-ARCHIVE-REPAIR-ADMISSION | Metadata-only reuse skipped damaged previously verified extraction trees. Installation now verifies digests at the shared reuse decision and reaches existing offline archive repair. | `test_artifacts.py`: missing and replaced extracted model files are repaired without network use, archive replacement or quarantine; subsequent installation reuses the repaired tree. |
| REVIEW-DELETE-CROSS-PROCESS-OWNERSHIP | Deletion released worker ownership before moving the directory. Shared run locks now live outside movable run directories and remain held through deletion and restoration; missing runs cannot be claimed. Cleanup probes the same stable locks. | `test_run_delete_ownership.py`: spawned workers contend before/after the actual move, cannot resurrect deleted runs, can resume after explicit restoration, and exclude deletion while holding ownership. |

No live inference, external receipt reconciliation, UI or licensing changes,
publication, or outer pipeline gates are performed in this review phase.
Focused verification covered **444 distinct tests**. The combined batch initially
passed 441 tests and exposed one negation-span boundary plus two invalid new
interrupted-run fixtures. After correcting those, the affected routing,
whole-session and cross-process ownership batch passed **227 tests in 2.77
seconds**. The remaining 217 tests passed in the initial batch. Touched Python
files were formatted; full repository test/lint/type gates and native
Linux/Windows execution remain with the outer executor.

## Review fixes after f443aad

All four selected findings were confirmed against current source before editing.

| Finding | Root cause and correction | Behavioral regression |
| --- | --- | --- |
| REVIEW-REPEATED-MARKER-CONTROL-LOSS | Marker matching depended on paragraph topology or unique values. Ordered authored-marker alignment now preserves only unambiguous correspondence, including repeated equal values when another marker changes. | `test_revisions.py`: repeated markers with combined prose/control edits, explicit resets and ambiguity, plus exhaustive short-stream comparison with all optimal alignments. |
| REVIEW-COMPOUND-BREATH-EXCLUSION-ORDER | Stripping the first negative item discarded negation for later coordinated items. Routing now evaluates independent breath exclusions within original clauses while separating contrastive positive requests. | `test_recipe_routing.py` and `test_recipe_whole_session.py`: both item orders, coordinated punctuation, typographic apostrophes, positive natural-breath clauses and unsupported positive box-breathing requests. |
| REVIEW-PLAN-REGENERATION-OWNERSHIP | CLI regeneration owned only the final commit, leaving reads and draft persistence exposed to deletion. The shared stable lock now covers reads, drafting and revision activation; the commit implementation accepts existing ownership without nested acquisition. | `test_plan_regeneration_ownership.py`: real CLI with a deterministic generator, spawned deletion contender at read/persistence/activation boundaries, existing-owner refusal and deletion-before-regeneration without path recreation. |
| REVIEW-ORIGINAL-AUDIO-BINDING | Voice cards verified only the listening copy, despite linking original audio. Both independently hash-bound files must now exist and match before page generation. | `test_listening_review.py`: original/copy replacement, removal and missing hashes reject rebuilding while retaining receipts, remaining audio and the prior page bytes. |

This review phase does not perform live inference, external evidence
reconciliation, UI/source-license changes, publication or outer pipeline gates.
Focused verification: **372 passed in 32.87 seconds** across revisions, timeline
compilation, plan-regeneration ownership, deletion/restoration, admission, recipe
routing, whole-session generation and listening-page evidence. The ordered-marker
oracle additionally checked 1,600 pairs of short streams against independently
enumerated optimal matchings. Touched Python files were formatted. No full
repository test/lint/type gate, native Linux/Windows run or CI phase ran here.

## Review fixes after 95531c4

All five selected findings were confirmed against source before editing.

| Finding | Root cause and correction | Behavioral regression |
| --- | --- | --- |
| REVIEW-REPEATED-PROSE-CONTROL-LOSS | Global text uniqueness overruled stable ordered identity. Revisions now use unambiguous ordered segment correspondence; ambiguous structural edits that would lose accepted controls reject before activation unless explicit replacements resolve them. | `test_revisions.py`: A/B/A/C edit/render/restore, explicit marker and field resets, duplicate insertion/deletion/reordering, and unchanged persisted artifacts after rejection. |
| REVIEW-POSITIVE-SLEEP-CLAUSE-NEGATED | Sleep negation consumed unrelated positive clauses. Sleep and breath routing now share independent request-clause boundaries. | `test_recipe_routing.py` and `test_recipe_whole_session.py`: positive sleep with breath exclusions, punctuation and typographic apostrophes, and actual negated sleep. |
| REVIEW-AUDITION-DOWNGRADE | Rebuilding aborted on invalid audio and left stale qualified cards. Current voice and writing cards now downgrade to Fail and hide unverified playback/evidence links; previous HTML bytes are retained in content-addressed history. | `test_listening_review.py`: changed/missing/unbound original and listening audio, independent unaffected cards, immutable receipts and report history. |
| REVIEW-REJECTION-RATING-GATE | The release gate applied promotion rating requirements to rejections. Only promotions require nonempty ratings; all supplied rejection ratings still require valid identity and content bindings. | `test_lab_decision_ratings.py`: complete synthetic gate with unrated/rated rejection, missing/stale supplied ratings and unrated promotion refusal. |
| REVIEW-INTERRUPTION-TIMEOUT-CHILD | Timeout and exceptional interruption exits bypassed shared process-tree cleanup. Every exit now invokes cleanup before draining streams and proceeding. | `test_release_acceptance.py`: timeout and marker exceptions with ordinary and detached child processes, retained partial streams/artifacts and next-case process exclusivity. |

No live inference, external evidence reconciliation, UI/source-license changes,
publication or outer pipeline gates were performed in this review phase.

Focused verification: **442 passed in 51.25 seconds** in one batch across
revisions, timeline compilation, recipe routing, whole-session generation,
listening-page evidence, Lab decision ratings, release status and acceptance
cleanup. Synthetic child processes exercised timeout and exceptional exits,
including detached POSIX sessions on this host. Touched Python files were
formatted. No full repository test/lint/type or CI gate ran here.

## Review fixes after 60c9eed

All three selected findings were confirmed against source before editing.

| Finding | Root cause and correction | Behavioral regression |
| --- | --- | --- |
| REVIEW-SPLIT-PROSE-CONTROL-LOSS | Unequal replacement gaps with no exact segment matches bypassed control preservation and ambiguity checks. Exact speech splits can now retain safely corresponding controls; unsupported structural control or timing mappings reject before activation. | `test_revisions.py`: exact sentence/blank-line split, explicit replacements and unsupported structural changes with unchanged persisted state. |
| REVIEW-POSITIVE-SLEEP-SUBJECT-CLAUSE | Shared negation boundaries recognized imperative clauses but missed independent subject clauses. Coordinating conjunctions before subject pronouns now separate clauses for both sleep and breath routing. | `test_recipe_routing.py` and `test_recipe_whole_session.py`: positive subject-clause sleep requests, actual negative sleep, coordinated exclusions and positive natural/box-breathing clauses. |
| REVIEW-LISTENING-EXTERNAL-OUTPUT | Unrestricted report destinations broke benchmark-relative playback and evidence links. Output is now a report filename alongside the benchmark root; unsupported destinations reject before writes. | `test_listening_review.py`: resolved playback/evidence links, unsupported destinations, retained history and current Fail behavior. |

This phase does not perform live inference, external receipt reconciliation,
UI/source-license changes, publication or outer pipeline gates.

Focused verification covered **413 distinct tests**. The combined batch passed
412 tests and exposed one existing ownership fixture that implicitly discarded
an accepted delivery control during whole-script replacement. Its replacement
now declares an explicit reset while preserving every stale-draft ownership
assertion; all four parameterized cases then passed in **0.40 seconds**. The
initial focused batch took **36.79 seconds**. Touched Python files were formatted.
No full repository test/lint/type or CI gate ran here.

## Review fixes after bba3104

All six selected findings were confirmed against source before editing.

| Finding | Root cause and correction | Behavioral regression |
| --- | --- | --- |
| REVIEW-NOUN-SUBJECT-SLEEP-CLAUSE | Clause splitting recognized only pronouns and selected imperatives. A shared subject/finite-verb pattern now separates noun-subject requests while coordinated negative object lists retain their scope. | `test_recipe_routing.py`: goal/aim/session subjects, negative noun clauses, coordinated sleep/breath/box objects, and separate positive breathing clauses. |
| REVIEW-COORDINATED-NEGATED-WAKING | Negative movement spans ended before their body objects, exposing coordinated waking actions as positive. Movement objects and coordinated body/action phrases now remain within the negative span. | `test_recipe_whole_session.py`: exact hands/eyes example, contractions, body objects, negative coordination and independently positive waking clauses. |
| REVIEW-PARAGRAPH-SEPARATOR-QUIET-IDENTITY | Paragraph strings could contain compiler-recognized separators, breaking identity during quiet-bound recovery. Draft validation rejects blank separator lines without rewriting prose; unmatched recovered speech fails explicitly. | `test_recipe_whole_session.py`: LF/CRLF/whitespace and Unicode separator variants, unchanged single-line-break prose, matching quiet limits and invalid recovery identity. |
| REVIEW-PLAN-LAUNCH-ADMISSION-LEAK | Task registration preceded fallible plan-input preparation. Preparation now occurs under admission before task registration. | Task/admission regressions inject directory and write failures and verify no stuck task, activated revision or blocked subsequent operation. |
| REVIEW-ALTERNATIVE-TAKE-PREMATURE-COMPLETION | Public task status followed child completion before alternative finalization finished. Finalization inspects durable child state separately while public status and selected-audio access remain unavailable through restoration and reassembly. Finalization failures persist a safe failure state requiring retained-take selection before web or CLI resume. | `test_task_finalization.py` exercises child exit, save/restore and reassembly boundaries through task/run and audio/export interfaces, plus failure handling across restart and worker resume. |
| REVIEW-MLX-WHEEL-ACCEPTANCE-INVENTORY | A duplicated worker allowlist omitted packaged MLX. The runtime probe now resolves the complete worker inventory declared by the supplied wheel. | `test_review_evidence_regressions.py`: actual subprocess/package consumer accepts matching MLX and rejects changed or missing runtime bytes. |

No live inference, external receipt reconciliation, UI/source-license changes,
publication or outer pipeline gates were performed in this review phase.

Focused verification passed **462 tests in 13.14 seconds**, covering routing,
whole-session validation, acceptance evidence, task finalization, takes,
revision/durable admission, web interfaces, plan-regeneration ownership and
exports. The only warning was the existing Starlette/httpx deprecation.
Touched Python files were formatted. No full repository test/lint/type or CI
gate ran here.
