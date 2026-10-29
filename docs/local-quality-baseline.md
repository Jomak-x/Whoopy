# Local quality baseline and comparisons

The current work targets original, reliable single meditations on an M4 Pro with
48 GiB unified memory. Public publishing, paid/API generation, courses and ambience
are outside this implementation slice.

## Writing

The new `recipe_whole_session` method uses structural recipes, not canned prose.
One call receives the original request and all beat purposes, posture, anchor,
ending and word/time contracts. One possible revision receives the full previous
draft and all feedback. Accepted sentences remain intact. Invalid drafts are
preserved as failures, not edited into apparent success.

Natural breathing is optional, and no-breath requests use an external anchor.
Unsupported box breathing is refused with an actionable alternative. Routing and
semantic quality still need real-model review; deterministic gates cannot fully
understand all possible user needs. Exact quiet is bounded in a typed timeline.
Actual spoken duration depends on the voice: a text estimate is not final-audio
measurement. The CLI reserves 32K writer context for full-session revision.

For prompt generation, an explicit speech speed remains effective when no pacing
profile is explicitly selected. Selecting a pacing profile uses its speed. Fresh generated
runs still fit their bounded silence to the requested duration using actual
speech. Later explicit pause edits preserve the accepted pause schedule instead
of fitting it back to the original duration; the rendered audio determines the
reported actual duration.

```bash
uv run --offline python scripts/benchmark_writing_methods.py \
  --profile high --output evaluations/benchmarks/writing-v1
```

The comparison supports Standard Qwen3 4B and High Ministral 3 14B, using
identical seed/context/sampling for both
methods, three needs and saved raw attempts. Read the content, then synthesize
selected old/new scripts using the same voice and pipeline. Judge progression,
relevance, transitions, agency, time to follow, silence and desire to replay.

Headspace's public teaching material supports a coherent explanation, practice
and integration, multiple anchors and progressively less guidance. This is
structural research, not source prose to copy or evidence of quality equivalence:

- [Teaching structure and techniques](https://www.headspace.com/meditation/techniques)
- [Progression after the Basics](https://www.headspace.com/articles/after-headspace-basics)

## Audio

Use the same fixed script, native raw audio, exact revisions/settings/seeds and
speech-only RTF for every voice. Acquire `/tmp/whoopy-benchmark-gpu.lock` for one
heavy model at a time. Never disable memory safeguards, count inserted silence as
fast speech generation, or change expected words to conceal dropped speech.

```bash
models/experimental/verification-2026-10-28/.venv/bin/python \
  scripts/verify_benchmark_audio.py candidate.wav \
  --expected-file spoken.txt --report candidate-verification.json
```

Independent offline ASR receives no expected-text prompt. The report combines
ordered coverage, word edits and PCM checks, with exact provenance and thresholds.
Exit 0 means pass, 2 review, 1 fail. Queue timeout/absent recognition is never a
content pass. ASR is imperfect; ambiguous cases need listening. Passing speech
checks does not mean a voice is warm, accurate in every word, or listener-preferred.

The retained constant-gain audition copies were made after historical screening
at common loudness, constrained by true peak. Copies reclassified as Review stay
visible with their current labels and original evidence. Preserve gain and
raw/copy hashes. Do not denoise, stretch or hide failures with ambience. Compare
first/middle/last voice identity in longer sessions before promoting a default.

## Kokoro recovery

The official Sherpa archive changed October 26, 2026. Its old lock correctly
refused the changed bytes. The refreshed lock binds official size 349,886,476,
SHA-256 `60703707bc05ccd48d60e1eb723f38b07d6e474795a0b6a588d7220400e0f7a4`,
three model/voice/token hashes and the full extracted-tree digest. The archive's
README states existing voice IDs 0–52 remain unchanged; a Spanish voice is ID53.

A valid cached archive is now retained when only extraction is missing/stale;
offline installation repairs the tree. Bad download bytes still go to quarantine.
Both paths have executable tests. Acquisition uses `models install --profile basic`.

## Licenses and boundaries

The source-code license remains undecided by explicit owner choice. Prefer
permissive components for optional future commercial use, but distinguish runtime,
model/codec weights, voice-reference rights and finished-output terms. Save exact
licenses and revisions. Fish Drama 3 has revenue/hosted-service conditions and
specific exceptions; its classification depends on the actual product/input flow.
Higgs TTS 3 has separate product and evaluation restrictions. These can justify
exclusion. A memory preflight refusal is also a result, not permission to raise
allocator limits. No commercial/API feature is implemented here.

## Dated local evidence: 28 October 2026

Read [the benchmark report](../evaluations/benchmarks/2026-10-28/RESULTS.md) before
interpreting the current defaults. The final High writer uses pinned Ministral 3
14B Q4_K_M. In the v5 daytime comparison, both methods had the same model, seed 42,
32K context and sampling; legacy controls were explicitly reused with provenance.
The smaller Standard Qwen3 4B pilot failed its holistic revision on word/time
budgets and contract details; the new method is not yet shown reliable on that
writer. Both profiles remain explicit CLI choices. Concurrent UI changes are
excluded from this backend publication.
The new High grounding and no-breath scripts each completed in one saved attempt and
show clearer practice/return/ending structure on text review. Their repetitive
wording and an unsupported eyes-opening instruction remain editorial limitations.
The initial five-minute sleep comparison was refused by memory preflight. Its
separate retry completed the legacy control but the new method failed after one
holistic revision: 213.7 s required quiet exceeded the 10–134 s bounded capacity,
and a reflection sentence was incomplete. That failure and the initial refusal
remain recorded. No accepted new sleep script/audio or human score is implied.

The grounding scripts were also rendered through the same production Kokoro
`am_michael` voice at native speed 0.8: legacy 180.204 s and new 183.511 s. Hashes
and exact pause sequences passed the recorded checks. Independent narration ASR
passed the historical tolerance-based screening: legacy WER 1.869%, ordered
coverage 99.065%; new WER 0.637%, coverage 99.363%. Under the current exact-word
policy, both are **Review**, with four and one normalized word edits respectively.
The [writing sidecar](../evaluations/benchmarks/2026-10-28/writing-listening-policy-v3.json)
links the unchanged original receipts and current classifications. Human listening
remains pending; these are reassessments of retained recognition, not new ASR runs.

A separate real production High prompt-to-audio run at default Kokoro speed 0.6
and planning rate 95 WPM completed after one holistic revision. It produced
180.000125 s (102.500125 s speech, 77.5 s exact silence); all pauses were
6.567–15.826 s, within the 16 s cap. Audio QC passed with no resource-guard stop.
Independent narration ASR passed historical tolerance-based screening with WER
2.5% and ordered coverage 99.375%; its four normalized word edits now require
**Review**.
The dated `production-new-writer/integration-summary.json` and verification report
retain the evidence and recognition/tokenization differences. All three writing
auditions are Review under the current policy, without word-perfect or
human-quality claims. The initial sandbox monitor refusal is preserved separately.

The fixed-script voice inventory has nine complete auditions and one Dots
resource failure. Eight complete auditions passed historical tolerance-based
screening, including the final MLX Ryan follow-up. Native MPS Ryan failed localized
content screening and remains excluded. Vox is now **Review** for the retained
full-file ASR disagreement involving 28 tail deletions. This is not proof that
speech is missing: complete independent paragraph recognition and exact PCM
assembly remain positive evidence. Runtime/precision/policy changes prevent
attributing the Ryan difference to one cause. The separate production Sherpa
Kokoro recording also passed historical screening; its one extra recognized
initial word now requires **Review**.

The current [strict voice sidecar](../evaluations/benchmarks/2026-10-28/verification/listening/manifest-policy-v3-strict.json)
selects seven voice Pass results and two voice Reviews (production Kokoro and Vox).
Together with the three writing Reviews, the current
[audition page](../evaluations/benchmarks/2026-10-28/review.html) contains
**7 Pass / 5 Review across 12 auditions**. Earlier tolerant receipts and the
[earlier v3 sidecar](../evaluations/benchmarks/2026-10-28/verification/listening/manifest-policy-v3.json)
remain historical evidence. Pass means no normalized word mismatches in the
selected retained ASR evidence, not a guarantee of perfect speech. No new
recognition or audio changes accompanied this classification update.
The report links raw reports, gain-only matched copies and the exact license audit.
Human listening and sustained voice quality remain pending; experimental results
do not promote a production pack. Earlier generations are recoverably archived,
and their old receipts are historical evidence.

The controlled writer benchmark accepts `--models-dir` when verified model assets
are stored in another checkout. Fresh outputs retain each actual llama.cpp
invocation under `raw-invocations/`, including available command inputs, raw
output, streams and failure metadata. Its local `.gitignore` keeps those records
out of accidental publication; select a small factual summary for GitHub.
