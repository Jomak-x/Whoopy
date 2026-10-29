# Local V1 evaluation evidence

The checked-in Lab definitions are controlled, versioned single-case comparisons for
the reviewed `grounding-3m` case in `config/evaluation/local-v1.yaml`. Register a JSON
definition through `POST /api/v1/lab/experiments`, then explicitly execute only
`grounding-3m` through its versioned execute endpoint. Saving a definition never starts
inference.

Run and review the comparisons in this order:

1. `lab-writing-method-definition.json` selects the production writing approach.
2. `lab-llm-grounding-3m-v1.json` compares Standard Qwen3 4B with explicit Lite
   Qwen3 1.7B while writing, voice, pacing, seed, and case stay fixed.
3. `lab-voice-kokoro-grounding-3m-v1.json` compares Kokoro `af_heart` with
   `af_nicole` while the accepted content inputs and pacing stay fixed.
4. `lab-pacing-kokoro-grounding-3m-v1.json` compares `meditative-v2` with
   `progressive-quiet-v2` while content and the complete voice configuration stay fixed.

Finish the randomized blind A/B review and save 1–5 ratings and notes after each
comparison. Do not promote a candidate into Studio defaults from automatic metrics or
from successful generation alone. Promotion requires completed human ratings and a
written, versioned decision record. After an accepted single-case result, run the
relevant regression comparison before expanding to the full ten-case corpus.

The two LLM candidates require their pinned local packs. A missing or resource-blocked
pack is evidence that the comparison cannot run on the current machine; it is not a
reason to substitute an untracked model or change another control.

`standard-telemetry-current.json` is a single-machine production receipt for the pinned
Standard Qwen3 4B → Kokoro path. It records the exact command, controls, runtime/license,
hardware, measured load/inference/token-speed/process-memory fields, output hashes, and
explicitly unavailable fields. It proves that path on the named machine only; it does not
replace cross-platform reports or human meditation ratings.
