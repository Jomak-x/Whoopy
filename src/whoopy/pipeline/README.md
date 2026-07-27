# Pipeline

Orchestration, checkpointing, caching, recovery, and duration fitting belong
here. This layer coordinates ports without importing model-specific behavior.

Phase 1 introduced the first executable slice:

- `runs.py` owns durable run records and JSON artifact storage;
- `worker.py` moves a queued run through `running` to `completed` or `failed`;
- every run lives in `runs/<run-id>/`;
- the worker writes `timeline.json` before marking the run completed.

Phase 2 extended completion to a validated timeline, PCM WAV, frame-range
manifest, and passing quality report.

Phase 3 adds:

- `cache.py` for content-addressed speech shared by runs;
- `checkpoints.py` for verified per-run speech progress;
- bounded transient retries and immediate fatal failures;
- resume from failed or interrupted runs;
- recovery/cache counters in schema-v3 run records.

There is still no daemon, concurrent claim/lease, real model, or production
audio adapter.
