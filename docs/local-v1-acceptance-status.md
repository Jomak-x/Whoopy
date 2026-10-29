# Consolidated Local V1 Acceptance Status

Whoopy keeps production evidence in separate durable receipts so a test in one area cannot silently
grant another. The consolidated status command reads those receipts, validates their internal
claims, hashes every source it uses, and derives one release status:

```bash
uv run python scripts/local_v1_acceptance_status.py
```

The command always writes `acceptance-reports/local-v1-status-current.json`. It exits zero only when
every required gate passes; an incomplete report is still written and exits one. Never edit
`local_v1_release_ready`: the schema rejects `true` unless all required gates and all independently
observed targets pass.

The canonical local inputs are
`evaluations/local/corpus-acceptance-current.json`,
`acceptance-reports/soak-exports-2026-08-05.json`,
`acceptance-reports/forced-interruption-local-2026-08-05.json`, and
`evaluations/local/studio-lifecycle-current.json`. The older
`acceptance-reports/local-v1-current.json` and `acceptance-reports/local-v1-soak.json` files are
superseded development-run reports, not release authority or valid substitutes for those inputs.
See [`acceptance-reports/README.md`](../acceptance-reports/README.md) for the complete evidence
hierarchy.

## Required evidence families

The report keeps these gates separate:

- all ten corpus cases, explicit safety passes, content and voice ratings for every case, medians at
  least 4/5, no required rating below 3/5, and blind content preferences against a declared
  LLM/writing-method baseline for every case with more challenger than baseline preferences;
- exactly 20 primary-machine runs—four each at 1, 3, 5, 10, and 20 minutes—with decoded WAV, FLAC,
  AAC, and Opus exports;
- observed interruption plus successful recovery at planning, drafting, synthesis, assembly,
  encoding, and QC;
- a localhost Studio lifecycle containing health, generation, playback, editing, all exports, and
  recoverable deletion;
- current model files, runtime probe, and playable-synthesis evidence for every Studio voice;
- complete production-backed LLM, writing-method, voice, and pacing comparisons;
- durable blind ratings and hash-bound promotion decisions for the selected Studio defaults,
  including distinct `portable_default` and `low_resource_fallback` model roles; an optional
  `higher_quality` role is validated when present but is not required;
- a fresh target-specific application-bundle install that completes the full Studio lifecycle with
  externally disabled networking on every release target;
- one complete acceptance report generated independently on Windows x64, macOS ARM64, macOS x64,
  Linux x64, and Linux ARM64;
- explicit OS-network-disabled evidence on every independent target;
- a current-version wheel and sdist whose packaged bytes match the implementation tree.

The default model scan is intentionally cheap and reports `unverified`. The release invocation must
hash every declared checkpoint:

```bash
uv run python scripts/local_v1_acceptance_status.py --verify-model-digests
```

This may take substantial time for MOSS. Historical `runtime.json`, `smoke.json`, or a static ready
badge cannot bypass the current registry fingerprints.

## CI targets are not observed platforms

CI portability receipts prove that a fresh wheel starts and exercises lightweight behavior on the
declared runner target. They explicitly use the scope `ci_portability_not_release_acceptance`, do
not prove OS network isolation, and are informational in the consolidated report:

```bash
uv run python scripts/local_v1_acceptance_status.py \
  --ci-portability-summary portability-summary.json
```

The five `declared_ci_targets` therefore remain separate from
`independently_observed_platforms`. Even a fully passing CI summary cannot change the independent
platform or offline gates.

Collect complete reports on their own machines with `scripts/local_v1_acceptance.py`, copy the JSON
receipts to the release workspace, and supply each one:

Use `config/acceptance/local-v1-platform-v1.json` for these five independent reports. It contains a
bounded real prompt-to-audio smoke; the much larger primary-machine soak and interruption suite is
verified by the separate primary gates rather than repeated on every target.

Each run must include `--network-disabled --network-isolation-note "..."`. This is stored explicitly
as operator attestation; it never claims the harness configured or independently measured the host
firewall. The report remains invalid if the note is absent. Each independent run must also receive
`--product-wheel /path/to/whoopy-<version>-py3-none-any.whl`. All five operators use the exact same
transferred wheel; the receipt embeds its SHA-256, and aggregation rejects an unbound legacy report,
a different same-version wheel, or a digest that differs from the current packaged and installed-
bundle evidence.

```bash
uv run python scripts/local_v1_acceptance_status.py \
  --platform-report acceptance/windows-x86_64.json \
  --platform-report acceptance/darwin-arm64.json \
  --platform-report acceptance/darwin-x86_64.json \
  --platform-report acceptance/linux-x86_64.json \
  --platform-report acceptance/linux-arm64.json \
  --verify-model-digests
```

The platform aggregator validates the canonical digest of the exact suite content and current
product version, exact case coverage, soak repetition counts,
interruption markers and recovery, migrations, local-only matrix ownership, and offline assertions.
Missing machines remain `not_run`; duplicate or foreign claims fail closed.

## Installed-bundle lifecycle evidence

Independent platform reports and their network flags do not prove that the packaged product can be
installed and used without the source checkout. On every target, run
`scripts/offline_bundle_acceptance.py` against that target's fresh bundle with the full Studio
lifecycle enabled, then supply the five self-contained receipts separately:

```bash
uv run python scripts/local_v1_acceptance_status.py \
  --installed-bundle-report acceptance/bundles/windows-x86_64.json \
  --installed-bundle-report acceptance/bundles/darwin-arm64.json \
  --installed-bundle-report acceptance/bundles/darwin-x86_64.json \
  --installed-bundle-report acceptance/bundles/linux-x86_64.json \
  --installed-bundle-report acceptance/bundles/linux-arm64.json \
  --platform-report acceptance/platforms/windows-x86_64.json \
  --platform-report acceptance/platforms/darwin-arm64.json \
  --platform-report acceptance/platforms/darwin-x86_64.json \
  --platform-report acceptance/platforms/linux-x86_64.json \
  --platform-report acceptance/platforms/linux-arm64.json \
  --verify-model-digests
```

Each installed-bundle receipt is bound to the current product version, bundle, wheel, dependency
lock, target interpreter, localhost API/frontend probes, and embedded generate/play/edit/export/
resume/delete evidence. It also requires an explicit operator attestation describing the external
OS network control. Package-index isolation alone, a health-only launch, or network assertions in a
platform report cannot satisfy this gate.

## Package freshness

Package freshness compares the current project version, Python modules, bundled static frontend,
and packaged resource YAML byte-for-byte with the wheel. Both wheel and sdist must be newer than all
package inputs, and the sdist must contain the current `pyproject.toml`. Rebuild after any product
change:

```bash
uv build --offline
uv run python scripts/local_v1_acceptance_status.py
```

The report records absolute evidence paths, SHA-256 digests, timestamps, structured facts, and the
exact blockers for every non-passing required gate.
