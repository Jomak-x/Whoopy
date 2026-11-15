# Rejected v7 Standard grounding: draft annotation

Reviewed 5 November 2026. This is an annotation of actual retained output, not a
quality score, listening review, or evidence that v8 improves generation.

## Exact evidence

Campaign: `candidate-v7-standard-grounding-119430f5d6`.
Run: `331b8ba5-b8c1-451b-8e9a-821fa890fbbc`.
Evidence root in the primary checkout:
`evaluations/benchmarks/2026-11-05/candidate-v7-standard-grounding-119430f5d6/runs/.generation-workspaces/331b8ba5-b8c1-451b-8e9a-821fa890fbbc/`.

| Receipt under evidence root | SHA-256 |
| --- | --- |
| `raw-model-output/whole-session-01.json` | `f82ddbf7a07e9c2023cc9d80fecf8f276c436c0a7465cf5be944eac4ff2fc52d` |
| `raw-model-output/whole-session-revision-02.json` | `1aaa7b754c85a85ee83736afffc8152cdf5b5752c6c25a7ee6f8ffede36000ff` |

The first invocation is `33de0c55957f450f9deaf6363f536654/001`; the
revision is `0fbcd959441b4947b1d7439b34b76a73/001`, both beneath
`raw-invocations/`. Their command receipts use Qwen3-4B-Q4_K_M, llama.cpp b10142,
32,768 context, 2,000 output tokens, temperature 0.7, top-p 0.9, reasoning off,
and seeds 42/43. Both processes completed and produced JSON; the session failed
semantic/budget validation after the one allowed holistic revision.

Both command receipts include `--system-prompt-file`. Both stdout files report
"using custom system prompt". The system files are identical, 1,031 whitespace
words / 7,140 characters, SHA-256
`df5b37d47ff595ec8be5641c4e2a45340a63005a7becec34df227b780cdc1403`.
The initial contract adds 572 words and revision input adds 1,215. There is no
evidence of missing system delivery. Model receipt times are approximately
29.08 and 30.41 seconds; those are not audio-generation measurements.

## Observations

The total target was 216 words across nine paragraphs, each targeting 24 words
with a hard maximum of 39. Actual counts use Whoopy's speakable-word definition.

| Paragraph | Initial | Revision |
| --- | ---: | ---: |
| comfort | 48 | 37 |
| orientation | 47 | 36 |
| anchor | 43 | 41 |
| practice 1 | 47 | 39 |
| practice 2 | 39 | 36 |
| practice 3 | 30 | 30 |
| integration 1 | 37 | 29 |
| integration 2 | 37 | 34 |
| return | 52 | 48 |
| Total | 380 | 330 |

- **P1, actual safety:** the initial opening says "You are in a safe space".
  The then-current general safety validator and safety-assurance regex both
  missed it. The revision removed it, but the feedback had not identified it.
- **P1, budgets:** five initial paragraphs and two revised paragraphs exceed
  their cap. The revised anchor and return remain 41 and 48 words. Rejection is
  correct; a complete JSON object is not an accepted meditation.
- **P2, invented environment:** both drafts supply hums, conversation, clocks,
  breeze, or leaves despite the instruction not to name expected sound sources.
- **P2, unsupported anchor property:** both say "It is a steady presence in the
  moment." Actual sound may change or stop. This was not detected by the gates.
- **P2, action clarity:** "move gently to resume your day" does not tell the
  listener which small movement to try. The ending gate correctly rejects it.
- **P2, progression:** the practice repeats returning to sound and uses the
  slogan "a practice of presence, not of perfection" instead of developing
  observation or reducing instruction meaningfully.
- **P2, outcome language:** "This will help you stay grounded and present"
  supplies an expected benefit. It is not a treatment claim, but weakens the
  requested openness to the listener's actual experience.
- **P2, ending:** "a little space between you and the rest of the day" is
  abstract. The ending lacks a concrete next-activity cue and useful check-in.

The revision trimmed 50 words but remained far above the target. It retained the
third practice paragraph unchanged; that paragraph was not named as invalid in
feedback. Repeated ideas are not necessarily exact repeated sentences.

## Bounded response and pending comparison

V8 shortens the shared prompt, foregrounds paragraph budgets, specifies a named
small movement, removes duplicate return feedback, and shows one effective
global maximum. V7 feedback had mixed a 361-word timing capacity with a
351-word sum-of-paragraph ceiling and the 216-word target. All underlying timing
and paragraph gates remain enforced; this is a clearer explanation of the same
constraints. Targeted actual-safety coverage and corpus routing fixes accompany
the prompt change.

No decoder word-count constraint, prose truncation, filler, extra retry, or output
repair is used. Grammar-forced length could mask a weak writer by forcing a
syntactically closed but incomplete thought and would not fix the other defects.

The historical 14B grounding example is shorter and gives a concrete hand/finger
movement, but also contains invented sounds and unsupported continuity. It used
an older contract and eight paragraphs, so it is not a matched control. Fresh
matched 4B/14B comparisons and editorial review remain necessary before any
claim of improved prose quality.
