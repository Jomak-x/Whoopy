# Phase 3.5 Local Script-Model Bake-Off

Date: 2026-07-26

Decision: keep Qwen3-4B Q4_K_M as the Standard prompt-mode candidate. Do not
promote Qwen3-1.7B Q8_0 as a dependable Lite prompt-mode fallback. Keep Basic
authored-script mode as the reliable lower-resource path. Do not call any model
or voice a permanent winner until the blind listening review is complete.

## Reproduction

The versioned input is
[`config/evaluation/phase-3-5.yaml`](../../config/evaluation/phase-3-5.yaml).
Run the comparison after both profiles are installed:

```bash
uv run --offline whoopy evaluate \
  --profiles lite standard \
  --models-dir models/managed \
  --output-dir evaluations/local/phase-3-5-v1
```

The command saves `report.json` and each case's raw/validated artifacts. Local
evaluation output remains uncommitted because prompts can produce different
text when a model, runtime, seed, or prompt version changes. This document is
the reviewed result record.

Environment:

- MacBook Pro with Apple M4 Pro, 14 CPU cores, 48 GB RAM;
- macOS 26.5.1 arm64;
- llama.cpp b10142 using Metal;
- seed 42 and reasoning disabled;
- prompt bundle v1;
- Qwen3-1.7B Q8_0, immutable revision
  `90862c4b9d2787eaed51d12237eafdfe7c5f6077`;
- Qwen3-4B Q4_K_M, immutable revision
  `bc640142c66e1fdd12af0bd68f40445458f3869b`; and
- both downloaded files fully reverified against the artifact-lock digest.

The results are one measured run, not a universal performance claim. Peak
memory is sampled RSS for Whoopy plus its child process tree.

## Automatic Results

| Candidate | Strict success | Model bytes | Mean elapsed/case | Peak process tree | Mean timing error on successes |
|---|---:|---:|---:|---:|---:|
| Lite: Qwen3-1.7B Q8_0 | 0 / 6 | 1.83 GB | 14.53 s | 2,808 MB | n/a |
| Standard: Qwen3-4B Q4_K_M | 5 / 6 | 2.50 GB | 17.61 s | 3,697 MB | 7.57% |

The evaluation does not create a combined score. A smaller, faster candidate
that cannot reliably satisfy the structured contract is not interchangeable
with a slower successful one.

### Standard Per-Case Evidence

| Case | Result | Elapsed | Validation retries | Timing error | Repeated-trigram ratio |
|---|---:|---:|---:|---:|---:|
| grounding, 1 min | pass | 15.23 s | 2 | 9.52% | 0.0618 |
| breath awareness, 2 min | pass | 19.31 s | 1 | 4.05% | 0.1605 |
| body scan, 3 min | pass | 16.45 s | 0 | 18.81% | 0.1581 |
| sleep, 4 min | pass | 20.94 s | 0 | 4.40% | 0.1141 |
| anxious moment, 2 min | pass | 14.30 s | 0 | 1.07% | 0.0757 |
| daytime focus, 1 min | fail | 19.43 s | 5 | n/a | n/a |

The Standard failure corrected two earlier over-budget sections, then returned
59, 45, and 51 words for a final section budgeted at 23–37 words. This is a
visible bounded failure, not a silently truncated script. It shows that PR 4's
recovery and future prompt tuning remain important.

### Lite Failure Evidence

Lite failed every strict case. The dominant failures were:

- pause values of zero despite the minimum of one second;
- section IDs containing underscores despite the documented hyphen-only form;
- weights greater than the maximum;
- sections remaining substantially over their allocated word budgets after
  three repair attempts; and
- one section remaining under budget.

Two Lite plans eventually validated, but their section drafting still failed.
The model used about 889 MB less peak memory and its artifact was about 670 MB
smaller than Standard, but those savings do not compensate for 0/6 completion.

## Safety, Tone, And Timing Interpretation

Every reported success passed the implemented schema, ID, word-budget, markup,
medical-claim, guaranteed-outcome, breath-holding, and prescriptive-emotion
checks before becoming a timeline. “Passed” does not mean that code has proven
the content universally safe.

The repeated-trigram ratio is a diagnostic, not a quality verdict. The longer
body-scan and breath-awareness results repeated more phrases and deserve human
review. Invitational-phrase counts likewise cannot decide warmth or comfort.

The final real end-to-end Standard completion test requested 180 seconds,
estimated 180.57 seconds, rendered 180.37 seconds, and passed all audio
integrity checks. Its planning budget uses the measured Kokoro rate rather than
assuming a generic narration speed.

## License And Provenance Review

- Qwen3-1.7B and Qwen3-4B artifacts come from Qwen's official GGUF repositories
  and declare Apache-2.0.
- llama.cpp b10142 comes from the official ggml-org release and declares MIT.
- Kokoro-82M and the pinned sherpa-onnx model bundle declare Apache-2.0.
- sherpa-onnx 1.13.4 wheels are platform-specific official release artifacts.
- Exact URLs, immutable revisions, byte sizes, and SHA-256 digests live in
  [`config/artifacts.yaml`](../../config/artifacts.yaml).

This review supports local use. Public publishing still follows Whoopy's
separate asset and output-policy gate.

## Decision And Follow-Up

1. Standard remains the recommended prompt-mode profile on hardware that passes
   its live safety check.
2. Basic remains the dependable low-resource fallback: the user supplies a
   script and only Kokoro is loaded.
3. Lite stays installed and replaceable for experimentation, but `auto` must not
   describe it as reliable prompt generation based on this result.
4. Prompt/repair improvements should be evaluated on a new versioned fixture;
   this v1 result must not be overwritten.
5. The voice default remains provisional until a person completes the blind
   rubric in
   [`voice-listening-rubric.md`](./voice-listening-rubric.md).
