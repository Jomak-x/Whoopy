# Phase 3.5 PR 4: Validated Local Meditation Generation

This PR lets Whoopy draft a meditation with a local Qwen model. It deliberately
stops before speech: its responsibility is to turn unpredictable model text
into a trusted plan, script, and canonical timeline. PR 5 connects these
validated artifacts to the real Kokoro path.

## Try It Offline

Install the Standard stack once, then disconnect if desired:

```bash
uv run whoopy models install --profile standard
uv run --offline whoopy draft \
  "A gentle three-minute grounding meditation after a stressful day." \
  --minutes 3 \
  --profile standard
```

Whoopy prints a draft UUID and directory. The default files are:

```text
drafts/<UUID>/
├── request.json
├── plan.json
├── raw-model-output/
├── sections/
├── script.md
└── timeline.json
```

Nothing in this command leaves the laptop. `llama-cli` is launched as a
bounded child process using the exact verified runtime and GGUF model selected
by the artifact lock.

## Why Model Output Is Untrusted

A language model predicts likely text. Even when asked for JSON, it can add
Markdown, miss a field, repeat an ID, exceed a word budget, or write an unsafe
instruction. A prompt is helpful guidance; it is not a security or correctness
boundary.

Whoopy therefore uses this flow:

```text
user request
  -> untrusted plan JSON
  -> strict ProposedPlan validation
  -> deterministic time/word allocation
  -> trusted plan.json
  -> independently generated untrusted section JSON
  -> schema + identity + word-budget + safety validation
  -> trusted section checkpoints
  -> script.md
  -> canonical timeline.json
```

Only the objects on the trusted side can become a timeline. Raw strings are
saved for diagnosis, but pipeline code never treats them as valid just because
the model returned them.

## Plan First, Then Sections

The first model call proposes a title, intention, and three to six structural
sections. Each proposed section has a relative `weight`, not authority over
exact timing. Deterministic Python code divides the requested duration into:

- a speaking budget based on section weights;
- exact pause milliseconds; and
- a minimum and maximum word count for each section.

This separation is important. The model chooses meaning and flow; ordinary code
owns arithmetic and limits.

After the plan passes, each section is drafted against the same shared plan.
The default is one section at a time because two simultaneous llama.cpp
processes load two model copies. `--parallel-sections 2` is available for a
laptop with enough memory. Parallelism starts only after the shared plan is
valid, so sections cannot silently invent different structures.

## Bounded Repair

Invalid JSON or prose gets at most three attempts. The next attempt receives a
short validation error, a different deterministic seed, and the original
contract. There is no unbounded loop.

The real Qwen3-4B smoke test demonstrated why this matters: its plan passed on
the first call, while several sections initially exceeded their exact word
budgets and were corrected on the second or third bounded attempt. All raw
attempts were retained.

Whoopy currently rejects:

- missing, extra, or wrongly typed JSON fields;
- duplicate or malformed section IDs;
- a section returned under the wrong ID;
- prose outside its allocated word range;
- headings, pause markers, code fences, or SSML inside speech;
- treatment or guaranteed-outcome claims;
- instructions requiring breath holding; and
- prescriptive claims about what the listener must feel.

These checks are intentionally narrow and reviewable. They are not a claim that
software can determine whether all meditation content is safe. Human review
remains appropriate, especially before publication.

## Checkpoints And Resume

`request.json` binds a workspace to the exact prompt, duration, seed, prompt
versions, model revision, runtime, and settings. `plan.json` and every file in
`sections/` contain already validated objects.

Resume the same workspace with:

```bash
uv run --offline whoopy draft \
  "A gentle three-minute grounding meditation after a stressful day." \
  --minutes 3 \
  --profile standard \
  --draft-id <UUID>
```

The request must match exactly. Whoopy revalidates saved checkpoints and asks
the model only for missing sections. It refuses a corrupt checkpoint or an
attempt to reuse the directory with different inputs.

Writes use a temporary sibling followed by an atomic rename, so another process
does not see a half-written JSON checkpoint.

## Duration Is An Estimate At This Stage

The plan uses a documented 210-words-per-minute estimate plus exact pause time.
That value is calibrated from Kokoro v1.0 at Whoopy's default 0.9 speed and is
kept slightly below the measured roughly 214 words per minute.
The completed script must be within 25 percent or 20 seconds of the requested
duration, whichever tolerance is larger. This catches structurally short or
long results before TTS.

Real speech duration depends on the chosen voice, speed, punctuation, and text.
PR 5 measures the rendered WAV and joins this preflight estimate to the real
audio-quality report. Exact deliberate pauses are already canonical
milliseconds and do not depend on this estimate.

## Replaceability

The generator depends on `ScriptGenerator`, not Qwen or llama.cpp. The adapter
returns text plus versioned metadata; schemas, safety, allocation, checkpoint
storage, and timeline compilation remain model-independent. A replacement
model must pass the same contract and bake-off rather than changing this
pipeline.
