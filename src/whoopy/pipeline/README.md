# Pipeline

Orchestration, checkpointing, caching, recovery, and duration fitting belong
here. This layer coordinates ports without importing model-specific behavior.

Phase 1 introduces the first executable slice:

- `runs.py` owns durable run records and JSON artifact storage;
- `worker.py` moves a queued run through `running` to `completed` or `failed`;
- every run lives in `runs/<run-id>/`;
- the worker writes `timeline.json` before marking the run completed.

There is intentionally no daemon, concurrent claim/lease, retry loop, model, or
audio behavior yet.
