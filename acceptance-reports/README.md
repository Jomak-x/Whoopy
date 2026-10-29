# Acceptance report authority

`local-v1-status-current.json` is the only consolidated Local V1 release-status report. It is
authoritative only after it has been regenerated from the current source tree, bundled frontend,
distribution artifacts, and the canonical evidence inputs below. Source changes make its package
freshness result stale until the distributions and status report are rebuilt.

Canonical local inputs are:

- `../evaluations/local/corpus-acceptance-current.json` for the ten-case corpus gate;
- `soak-exports-2026-08-05.json` for the verified 20-run, four-format export soak;
- `forced-interruption-local-2026-08-05.json` for the six-stage interruption/recovery matrix;
- `../evaluations/local/studio-lifecycle-current.json` for the local Studio lifecycle.

Additional current, scope-limited local evidence is recorded in:

- `rendered-ui-qa-darwin-arm64-2026-08-05.json` for rendered desktop/narrow Studio and Lab QA;
- `offline-app-bundle-darwin-arm64-current.json` and
  `offline-installed-studio-lifecycle-darwin-arm64-current.json` for fresh-environment bundle
  installation, installed-runtime Kokoro smoke, and the complete installed Studio lifecycle.
  They prove package-index isolation, bundle integrity, real voice synthesis, generate/play/edit/
  export/resume/delete, and bundled UI launch on this Mac. They do not prove OS network isolation,
  artifact authenticity, independent-platform acceptance, or LLM prompt generation.

The matching current application artifact is under `../dist/offline-current/`. Its `.zip.sha256`,
embedded `bundle-manifest.json`, and `offline-app-bundle-darwin-arm64-current.json` receipt must all
identify the same archive, bundle ID, dependency lock, and product-wheel digest. Directories named
`dist/offline/` or `dist/offline-final/` and reports suffixed `-packaging` or `-final` are retained
development snapshots, not current acceptance inputs.

Component receipts prove only their named scope. Packaging receipts, CI portability summaries,
model smoke records, individual interruption reruns, and individual platform reports cannot by
themselves set `local_v1_release_ready`.

## Historical reports

`local-v1-current.json` and `local-v1-soak.json` are superseded development-run reports. Their
names predate the consolidated status format; neither is current release authority and neither is
a canonical input to `scripts/local_v1_acceptance_status.py`. The dated single-stage and rerun
interruption reports are also retained only as diagnostic history. Do not rename or copy any of
these over a canonical input.

Regenerate the consolidated status with:

```bash
uv run python scripts/local_v1_acceptance_status.py --verify-model-digests
```

The command fails closed and still writes a report when required evidence is missing or stale.
