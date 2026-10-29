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
