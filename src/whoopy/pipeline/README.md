# Pipeline

Orchestration, checkpointing, caching, recovery, and duration fitting belong
here. This layer coordinates ports without importing model-specific behavior.

Phase 1 introduced the first executable slice:

- `runs.py` owns durable run records and JSON artifact storage;
- `worker.py` moves a queued run through `running` to `completed` or `failed`;
- every run lives in `runs/<run-id>/`;
- the worker writes `timeline.json` before marking the run completed.

Phase 2 extends worker completion to require a validated timeline, PCM WAV,
frame-range manifest, and passing quality report. There is intentionally no
daemon, concurrent claim/lease, retry loop, model, or production audio behavior
yet.
