# Pipeline

Orchestration, checkpointing, caching, recovery, revisions, takes, and duration
fitting belong here. This layer coordinates ports without importing
model-specific behavior.

The current durable pipeline includes:

- `runs.py` for authoritative run records and JSON artifact storage;
- `worker.py` for leased generation through validated completion;
- `cache.py` for content-addressed speech shared by runs;
- `checkpoints.py` for verified per-run speech progress;
- `revisions.py` for immutable edits, control-preserving recompilation, and restore;
- `takes.py` for retained alternative segment audio and selections;
- bounded transient retries and immediate fatal failures;
- resume from failed or interrupted runs;
- recovery/cache counters and durable ownership in run records.

Every run lives in `runs/<run-id>/`. A run becomes completed only after its
timeline, PCM WAV, frame-range manifest, and quality report validate.
