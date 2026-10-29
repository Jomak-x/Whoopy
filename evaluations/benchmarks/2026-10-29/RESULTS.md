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
