# Acceptance report authority

The clean-slate cleanup archived the previous active generation and acceptance receipts. The files
named by the dated audit are historical observations of an earlier source, package, model, and UI
snapshot. They are absent from this tree and do not certify the current branch.

There is no active `local-v1-status-current.json` report or complete set of canonical acceptance
inputs. The current release status therefore fails closed: `local_v1_release_ready` is false until
the consolidated report and every required input are regenerated from one current implementation
snapshot. Historical component receipts, packaging reports, CI portability summaries, model smoke
records, and individual platform reports cannot satisfy that gate.

When current evidence is intentionally collected, generate the consolidated status with:

```bash
uv run python scripts/local_v1_acceptance_status.py --verify-model-digests
```

The command fails closed and still writes a report when required evidence is missing or stale.
