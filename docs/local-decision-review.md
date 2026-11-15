# Local writing and voice decisions

This is a small descriptive benchmark, not a clinical evaluation or a claim of Headspace-equivalent
quality. Human writing and voice preferences remain separate from generation success, PCM integrity,
and automatic word recognition. Model weights, generated audio and machine-local evaluation evidence
remain outside Git. Selecting a preferred option in the local review page does not change production
defaults, remove a model, or promote an experimental runtime.

## Writing methods

`config/evaluation/local_decision_review_2026_11.json` contains four original three-minute requests:
contact grounding, ordinary breath observation with an alternative, bedtime rest, and non-clinical
grounding for panic-like sensations. Both writing methods use the same case, pinned writer, voice,
requested speed and seed. The maintained `plan_first` route and `recipe_whole_session` are the actual
implementations, not two labels on the same prompt.

From a source checkout with an installed High writer and portable voice:

```sh
PYTHONPATH=src python scripts/generation_campaign.py \
  --source "$PWD" --python /absolute/path/to/python \
  --config config/evaluation/local_decision_review_2026_11.json \
  --models-dir /absolute/path/to/models/managed \
  --output /absolute/fresh/writing-comparison \
  --profile high --method plan_first --method recipe_whole_session \
  --voice am_michael --max-cases 8
```

Planning creates a frozen source inventory and exact commands without inference. Use a different
fresh output path and add `--execute` to run. See [campaign controls](generation-campaign.md).
These developer campaign tools use a POSIX GPU lock; the Mac measurements do not certify another
platform or turn the developer harness into a portable application installer.

Method-specific context sizes and planning budgets are part of the treatment. The current routes
use 8192 and 32768 context tokens, and different planning words-per-minute estimates. Duration fitting
changes adjustable quiet after synthesis; it does not rewrite prose or silently change explicit speed.
Report actual duration, speech and quiet time, word counts, rejected attempts and real elapsed time.
Incomplete pairs are failure evidence, not successful A/B pairs. One seed cannot establish variance.

Prompt version10 distinguishes an explicit primary anchor from background sensations. It also
distinguishes a discomfort-triggered alternative from a global exclusion, and forbidding prescribed
breath control from forbidding ordinary breath observation. Its progression guidance favors trying
the practice over repeating explanations. Contract tests establish routing behavior, not model quality.

## Voices

Choose one generated script and preserve its exact bytes for all voice candidates. Compile the same
speech blocks and authored pauses. Compare complete real configurations: speaker, instruction,
reference, precision, backend and supported pacing controls all matter. A runtime implementation
comparison is not isolated if those controls differ.

The normal CLI can render a retained script through portable or supported optional adapters:

```sh
whoopy generate --script-file /path/to/retained/script.md \
  --profile basic --tts-model kokoro --voice am_michael --speed 0.8 \
  --models-dir /path/to/models/managed --runs-dir /path/to/fresh/voice-runs --json
```

Use the backend's declared typed controls for another voice. Do not apply a Kokoro numeric speed to
a backend that does not support it. Optional and experimental availability is distinct from human
qualification; full native samples and failures remain retained. A different model architecture still
requires an adapter and capability/registry wiring. An audition-only wrapper is not a shipped adapter.

Keep natural speaking duration and report it. With an authored script, `--requested-duration-seconds`
checks the final duration; it does not automatically refit its pauses. Do not use that gate to reject
otherwise valid fixed-text voice auditions merely because natural speech rates differ. Preserve the
canonical audio before deriving any level-matched listening copy, and document transformations.

Run independent local speech recognition without supplying expected words as a recognition prompt.
Extract expected words from final typed SPEECH segments. Every normalized mismatch needs listening
review. A recognizer can make mistakes; waveform decodability alone cannot distinguish speech from
noise. Actual human judgments should be saved before revealing sample identities.

## Scope and portability

The core boundary is request → writer → validated script → typed speech/quiet timeline → voice
adapter → canonical PCM/render checks → derived exports. Existing explicit edits, durable request IDs,
cache identities, owned-worker cleanup and retained attempts remain authoritative. See
[speech checks](speech-screening.md), [request recovery](generation-request-ids.md), and
[worker lifecycle](worker-lifecycle.md).

The pinned portable speech plan currently requires a CPython3.11 host. Python3.12 core metadata and
separate optional child runtimes do not make CPython3.11 native wheels compatible with that host.
Wheel-tag checks reject incompatible fresh and cached materialization and bundle qualification.
See [setup](setup.md). Hosted model-free CI and actual model generation on each target are separate
evidence. MLX is a Mac-specific experimental path.

Current procedural rain/ocean mixing is a narrow optional utility. It preserves narration and labels
its sound source; it is not recorded nature or an AI music model. Existing visual rooms are static UI
scenes. Guided visualization, multi-session course continuity, clinical educational content, public
hosting and paid/API generation have different qualification needs; do not infer them from a short
single-session benchmark or a prototype control in the frontend.
