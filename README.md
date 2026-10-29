# Whoopy — local meditation generation

Whoopy creates guided meditation audio on your own computer. The current goal is
reliable, original single-session meditations with a coherent teaching arc and a
choice of local voices. Courses, ambience, accounts, publishing and cloud/API
generation are future work.

The backend includes durable generation, cached speech, exact timeline pauses,
interruption recovery, revisions, QC and audio exports. This update focuses on the
CLI, backend and benchmark evidence. The owner's concurrent UI experiment is
excluded; the bundled interface is retained from the base commit. Use the explicit
CLI options below for the new writer. Courses are future work.

## Start locally

Requires Python 3.11 or 3.12, [uv](https://docs.astral.sh/uv/), and FFmpeg/FFprobe.

```bash
uv sync --extra dev --locked
uv run whoopy doctor --json
uv run whoopy models install --profile basic
uv run whoopy models pack smoke-test kokoro
uv run whoopy models install --profile high
uv run whoopy app
```

Studio binds to `127.0.0.1:8765`; API documentation is at `/api/docs`. After models
and dependencies are acquired, generation uses local files. No cloud fallback or
outbound application telemetry is part of local generation.

Kokoro is archive-based: acquire it with `models install --profile basic`, not
`models pack install kokoro`. Optional models have separate runtime requirements.
Present files alone do not establish correct speech or current readiness.

## Flexible whole-session writing

`recipe_whole_session` is the new method. Recipes contain structural beat purposes,
posture/anchor/ending contracts and timing budgets, never finished meditation
paragraphs. The writer receives the user request and the entire session contract
in one call, then may make one complete revision with all feedback. Accepted prose
is preserved intact rather than shortened by sentence selection.

```bash
uv run --offline whoopy draft \
  "A seated grounding practice after work, using sound and contact." \
  --minutes 3 --profile high --writing-method recipe_whole_session

uv run --offline whoopy generate \
  "A gentle good-night reflection after a busy day, lying down in bed." \
  --minutes 5 --profile high --writing-method recipe_whole_session
```

The new path supports daytime/sleep needs and external anchors when breath focus
is declined. Box breathing is explicitly unsupported; this slice uses optional
natural breathing. Recipe and prompt hashes bind checkpoints to their actual
content. Quiet periods compile into typed timeline events. Longer sessions need
room for the full draft and revision; the CLI reserves 32K context for this method.

The CLI offers the larger Ministral 3 14B High profile or smaller Qwen3 4B
Standard profile. The High writer is pinned at
about 8.24 GB and requires at least 16 GiB available memory; its presence does not
claim a listener-approved default.

Legacy methods remain available for controlled comparison. Historical CLI records
retain their `plan_first` default; pass the new method explicitly. Automatic
structural acceptance does not establish good teaching or listener preference.

## Evaluation

```bash
uv run --offline python scripts/benchmark_writing_methods.py \
  --output evaluations/benchmarks/local-writing
```

This development benchmark uses identical model/request/settings for old and new
writing, a shared GPU lock, preserved failed attempts and no invented human score.
Voice comparisons use one authored control script solely as a benchmark fixture,
not a production template. Experimental dependencies stay isolated under ignored
`models/experimental/` rather than being added to the core application.

`scripts/verify_benchmark_audio.py` independently transcribes raw audio with local
Whisper and compares it to intended words, supplemented by PCM checks. Expected
text is never supplied to the recognizer as a prompt. Missing ASR fails closed.
Noise or incomplete speech cannot pass merely because a WAV has plausible RMS.
ASR cannot judge warmth, teaching quality or long-listen comfort.

See [the quality workflow](docs/local-quality-baseline.md) for commands and limits.

## State, cleanup and development

`runs/` is active work/cache; `models/` is local checkpoints/runtimes/references.
Generated benchmark audio remains local. These are ignored by Git. At the user's
request previous generations were moved into ignored `.whoopy-recovery/` storage;
this is recoverable cleanup, not secure erasure. Old status documents and receipts
are historical, not fresh acceptance evidence for the current source/models.

```bash
uv run --extra dev python scripts/check.py
```

The Python gate runs Ruff, formatting, strict mypy and behavioral tests. Live model
benchmarks/listening are separate from CI. Current interactive accessibility needs
real browser testing; served-document checks do not prove keyboard/focus/mobile
behavior. Cross-platform release certification is separate from this Mac's quality
work. The concurrent UI work has its own build and validation. Phase documents,
the old roadmap/master plan and `system-design.md` remain
historical design/implementation records.

## Licenses

The owner deliberately left the project-wide source license undecided. A public
repository is not by itself a license grant. Runtime code, weights, tokenizers and
reference voices have separate terms; inspect the per-candidate license audit.
Local evaluation does not imply permission for a future paid or hosted service.
