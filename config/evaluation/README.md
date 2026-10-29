# Evaluation Lab fixtures

`local-v1.yaml` is the reviewed, versioned ten-case corpus used by the permanent local
product. `phase-3-5.yaml` remains available only so historical script-model bake-off
reports can be reproduced.

An experiment definition selects exactly one comparison type:

- `llm`: only `condition.llm_model_id` may differ.
- `writing_method`: only `condition.writing_method` may differ.
- `voice`: the complete named `condition.voice` configuration may differ while spoken
  text and deterministic silence hashes must stay identical.
- `pacing`: only `condition.pacing_profile_id` may differ while spoken text and the
  complete voice configuration stay identical.

Every successful trial must come from the normal production run pipeline and retain
its run manifest, playable audio, adapter metadata, hardware, controlled-input hashes,
and individually named metrics. The Lab deliberately has no combined automatic score.
Use a single corpus case before queuing a full batch. Heavy experiments run through the
FIFO sequential queue so model loads do not overlap.

The repeatable Meditation Engine V2 automatic run is:

```console
whoopy evaluate --profiles lite \
  --writing-method blueprint_first_sparse \
  --evaluation-set config/evaluation/local-v1.yaml \
  --output-dir evaluations/local/corpus-lite-blueprint-current
python -m scripts.meditation_corpus_report \
  --bakeoff-report evaluations/local/corpus-lite-blueprint-current/report.json \
  --output acceptance-reports/meditation-engine-corpus-current.json
```

Run it only while the heavyweight-model slot is free. The first command preserves the
prompt, seed, raw attempts, plans, sections, scripts, and timelines. The second command
checks blueprint/kind fidelity, beat progression, reviewed techniques, endings, breath
mode, editorial and safety issues, sentence length, uninterrupted speech, deliberate
quiet, and ±5% duration. It reports exact failed sections and sentences and never turns
missing human review into an automatic pass.

Blind-review projections expose randomized left/right audio without candidate labels.
Ratings are stored separately on a 1–5 scale. Promoting a result into Studio requires a
versioned decision record tied to a saved report hash and human ratings. The prior
accepted report remains the baseline for named per-metric regression comparisons.
