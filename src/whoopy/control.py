"""Transport-independent local control-plane service.

The CLI calls this service today. A future FastAPI route can call the same
methods without taking ownership of persistence or worker behavior.
"""

from __future__ import annotations

from whoopy.pipeline.runs import RunRecord, RunStore


class LocalControlPlane:
    """Accept prompts and expose saved run state without doing worker work."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def submit_prompt(self, prompt: str) -> RunRecord:
        """Save a queued run; deliberately do not process it inline."""

        return self.store.create(prompt)

    def get_run(self, run_id: str) -> RunRecord:
        """Return the current durable state for a run."""

        return self.store.load(run_id)
