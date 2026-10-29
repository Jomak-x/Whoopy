# Local V1 release finalization

Use this runbook only after product source, bundled frontend, configuration, and worker scripts are
frozen. Release evidence is an observation of one implementation snapshot; never reuse a receipt
merely because the project version did not change.

## Before starting

1. Confirm no Codex agent or developer is still editing the tree.
2. Run the complete quality gate.
3. Confirm no generation, model smoke, corpus, or other heavyweight process is running. MOSS,
   Qwen, Fish, Kokoro, and LLM acceptance must run sequentially.
4. Preserve failed or superseded evidence under a noncanonical name. Only replace a canonical
   receipt after its replacement validates.

The following evidence must be regenerated after behavior-changing source changes:

- wheel, sdist, and target-native application bundle;
- installed-bundle Studio lifecycle;
- localhost Studio lifecycle;
- independent current-machine prompt-to-audio report and its migration audit;
- forced-interruption recovery report;
- primary-machine soak when generation, timeline, synthesis, assembly, encoding, export, or QC
  behavior changed;
- rendered UI QA when frontend assets changed.

Checkpoint/runtime bundles and model smoke evidence remain reusable only when their own registry,
adapter, worker, runtime, model revision, or capability fingerprint is unchanged. Lab ratings remain
bound to their immutable artifacts; promotion decisions must still point to the exact current
report and rating hashes.

## Build and statically verify the frozen product

Run from the repository root:

```bash
uv run --extra dev python scripts/check.py
uv run --extra dev pytest -q \
  tests/test_offline_app_bundle.py \
  tests/test_local_v1_status.py \
  tests/test_release_acceptance.py \
  tests/test_ci_portability.py \
  tests/test_platform_aliases.py \
  tests/test_runtime_release_validation.py
uv build --offline --out-dir dist
uv run python - <<'PY'
from pathlib import Path
from whoopy.release.status import assess_package

gate = assess_package(Path.cwd(), Path("dist"))
print(gate.model_dump_json(indent=2))
raise SystemExit(0 if gate.outcome == "passed" else 1)
PY
```

Build into a new empty bundle directory. Do not overwrite the last known-good bundle:

```bash
uv run python scripts/build_app_offline_bundle.py \
  --repository . \
  --wheel dist/whoopy-0.1.0-py3-none-any.whl \
  --output-dir dist/offline-local-v1-final \
  --target darwin-arm64 \
  --python 3.11 \
  --uv uv
uv run python scripts/install_offline_bundle.py \
  --bundle dist/offline-local-v1-final/whoopy-app-0.1.0-darwin-arm64-py311 \
  --verify-only
```

If the version changes, substitute the exact built wheel and bundle names. The builder refuses
ambiguous wheel directories and existing outputs.

## Refresh current Mac observations

First confirm the heavyweight slot is free. Then run exactly one installed lifecycle:

```bash
uv run whoopy models pack unload
uv run python scripts/offline_bundle_acceptance.py \
  --bundle dist/offline-local-v1-final/whoopy-app-0.1.0-darwin-arm64-py311 \
  --output acceptance-reports/offline-app-bundle-darwin-arm64-candidate.json \
  --macos-sandbox-network-isolation \
  --models-dir models/managed \
  --lifecycle-output \
    acceptance-reports/offline-installed-studio-lifecycle-darwin-arm64-candidate.json
```

Do not launch a corpus, MOSS proof, or second acceptance lifecycle concurrently. Validate the
candidate against the current wheel before making it canonical:

```bash
uv run python - <<'PY'
from pathlib import Path
from whoopy.release.status import assess_installed_bundles, assess_package

root = Path.cwd()
package = assess_package(root, root / "dist")
wheel_sha = package.facts.get("product_wheel_sha256")
gate = assess_installed_bundles(
    (root / "acceptance-reports/offline-app-bundle-darwin-arm64-candidate.json",),
    product_version="0.1.0",
    expected_product_wheel_sha256=str(wheel_sha),
)
print(package.model_dump_json(indent=2))
print(gate.model_dump_json(indent=2))
expected_only = {
    "Missing installed-bundle receipt for darwin-x86_64.",
    "Missing installed-bundle receipt for linux-arm64.",
    "Missing installed-bundle receipt for linux-x86_64.",
    "Missing installed-bundle receipt for windows-x86_64.",
}
raise SystemExit(
    0
    if package.outcome == "passed"
    and gate.facts.get("passed_targets") == ["darwin-arm64"]
    and set(gate.blockers) == expected_only
    else 1
)
PY
```

Refresh the current Mac prompt-to-audio and migration observation after the installed lifecycle is
done and the heavyweight slot is free:

```bash
uv run python scripts/local_v1_acceptance.py \
  --scenario config/acceptance/local-v1-platform-v1.json \
  --output acceptance-reports/platform-darwin-arm64-candidate.json \
  --product-wheel dist/whoopy-0.1.0-py3-none-any.whl \
  --config-dir config \
  --runs-dir runs \
  --models-root models \
  --required-pack kokoro \
  --macos-sandbox-network-isolation
```

Before replacing any canonical receipt, generate a candidate consolidated report. A nonzero exit
is expected while the foreign-platform and human gates remain open; inspect the JSON and require
the package gate to pass, `darwin-arm64` to be the only independently passed platform, and the
installed-bundle gate to list `darwin-arm64` in `passed_targets`:

```bash
uv run python scripts/local_v1_acceptance_status.py \
  --project-root . \
  --studio-lifecycle-report \
    acceptance-reports/offline-installed-studio-lifecycle-darwin-arm64-candidate.json \
  --platform-report acceptance-reports/platform-darwin-arm64-candidate.json \
  --installed-bundle-report \
    acceptance-reports/offline-app-bundle-darwin-arm64-candidate.json \
  --verify-model-digests \
  --output acceptance-reports/local-v1-status-candidate.json
```

After that inspection succeeds, promote with same-filesystem temporary files so a crash cannot
leave a partially written canonical receipt:

```bash
cp acceptance-reports/offline-app-bundle-darwin-arm64-candidate.json \
  acceptance-reports/.offline-app-bundle-darwin-arm64-current.json.tmp
mv acceptance-reports/.offline-app-bundle-darwin-arm64-current.json.tmp \
  acceptance-reports/offline-app-bundle-darwin-arm64-current.json
cp acceptance-reports/offline-installed-studio-lifecycle-darwin-arm64-candidate.json \
  evaluations/local/.studio-lifecycle-current.json.tmp
mv evaluations/local/.studio-lifecycle-current.json.tmp \
  evaluations/local/studio-lifecycle-current.json
cp acceptance-reports/platform-darwin-arm64-candidate.json \
  acceptance-reports/.platform-darwin-arm64-current.json.tmp
mv acceptance-reports/.platform-darwin-arm64-current.json.tmp \
  acceptance-reports/platform-darwin-arm64-current.json
```

The forced-interruption suite and 20-run soak are deliberately separate, expensive observations.
Regenerate them after their covered behavior changes rather than copying old JSON. The canonical
scenario definitions are `config/acceptance/local-v1-primary-v1.json`; use all six
`interrupt-*` cases for interruption evidence and the five `soak-*` cases for four repetitions at
1, 3, 5, 10, and 20 minutes.

## Collect the four remaining independent targets

This is an operator handoff, not a CI job. Hosted CI proves that a wheel can start on a runner, but
its receipts deliberately say `ci_portability_not_release_acceptance` and cannot satisfy either
release gate. Run the following workflow on independently operated Windows x64, macOS Intel,
Linux x64, and Linux ARM64 machines. Keep the existing macOS ARM64 receipts only if they still
match the same frozen product wheel and suite.

### Release inputs and target names

Give every operator these exact inputs through a trusted transfer:

- one frozen repository snapshot containing the acceptance scripts, `config/`, `uv.lock`, and the
  bundled frontend;
- the **same** `whoopy-<version>-py3-none-any.whl` for all five machines, plus its externally
  communicated SHA-256;
- Python 3.11, `uv`, FFmpeg, and FFprobe for the target architecture.

Do not ask each machine to build a replacement product wheel. The installed-bundle gate requires
all five receipts to contain one identical `product_wheel_sha256`. Each machine does acquire its
own target-native dependency wheelhouse around that shared universal Whoopy wheel. A Git branch
name, version number, or dirty checkout is not an implementation identity; the source snapshot
must be frozen and transferred before evidence collection starts.

The only accepted target IDs and common host aliases are:

| Release target | Expected host identity | GitHub runner label (informational only) |
| --- | --- | --- |
| `windows-x86_64` | Windows `AMD64`, `x64`, or `x86-64` | `windows-2025` |
| `darwin-arm64` | Darwin `arm64`, `aarch64`, or `arm64e` | `macos-15` |
| `darwin-x86_64` | Darwin `x86_64`, `AMD64`, `x64`, or `x86-64` | `macos-15-intel` |
| `linux-x86_64` | Linux `x86_64`, `AMD64`, `x64`, or `x86-64` | `ubuntu-24.04` |
| `linux-arm64` | Linux `aarch64`, `arm64`, or `arm64e` | `ubuntu-24.04-arm` |

On the candidate host, from the frozen repository root and while networking is still available,
run:

```bash
uv sync --extra dev --locked
uv run whoopy doctor --json
uv run python scripts/validate_runtime_releases.py
uv run whoopy models install --profile standard
uv run whoopy models pack smoke-test kokoro
```

Inspect the doctor's `snapshot.operating_system` and `snapshot.architecture`. The normalized pair
must identify exactly the target in the table; Windows acceptance must use native PowerShell or
Command Prompt, not WSL. The platform suite is a real Standard/Qwen3-4B-to-Kokoro generation, so
both the Standard profile and current-machine Kokoro smoke are required. Optional Fish, MOSS, and
Qwen TTS runtime archives are not needed for portable acceptance. The repository currently
declares published optional runtime bundles only for `darwin-arm64`; do not copy those runtimes to
another target or treat their absence as a Basic/Kokoro blocker.

Build the native application bundle around the shared product wheel. Use a new, empty output
directory and substitute the target ID from the table:

```bash
uv run python scripts/build_app_offline_bundle.py \
  --repository . \
  --wheel release-input/whoopy-0.1.0-py3-none-any.whl \
  --output-dir release-output \
  --target linux-x86_64 \
  --python 3.11 \
  --uv uv
uv run python scripts/install_offline_bundle.py \
  --bundle release-output/whoopy-app-0.1.0-linux-x86_64-py311 \
  --verify-only
```

The builder refuses cross-target acquisition, so this command must run natively on the named
machine. Its artifact names are deterministic:

```text
whoopy-app-<version>-<target>-py311/
whoopy-app-<version>-<target>-py311.zip
whoopy-app-<version>-<target>-py311.zip.sha256
```

If the version differs from `0.1.0`, use the exact shared wheel version in both names. The GitHub
workflow uploads these files under `offline-application-<target>`, but those artifacts remain
CI-built packaging aids rather than independent evidence. Extract a downloaded ZIP before passing
its unpacked bundle directory to the acceptance harness.

### Run both offline observations

On Windows and Linux, disable networking outside Whoopy for the complete duration of both commands
(for example, disable every host adapter or use an independently configured deny-all firewall
while retaining loopback). Only then run the installed-product lifecycle and independent platform
suite, replacing the target and describing the control that was actually used:

```bash
uv run python scripts/offline_bundle_acceptance.py \
  --bundle release-output/whoopy-app-0.1.0-linux-x86_64-py311 \
  --output release-evidence/bundles/linux-x86_64.json \
  --models-dir models/managed \
  --lifecycle-output release-evidence/lifecycles/linux-x86_64.json \
  --network-disabled \
  --network-isolation-note "All host network adapters disabled; loopback retained"
uv run python scripts/local_v1_acceptance.py \
  --scenario config/acceptance/local-v1-platform-v1.json \
  --output release-evidence/platforms/linux-x86_64.json \
  --product-wheel release-input/whoopy-0.1.0-py3-none-any.whl \
  --config-dir config \
  --runs-dir runs \
  --models-root models \
  --required-pack kokoro \
  --network-disabled \
  --network-isolation-note "All host network adapters disabled; loopback retained"
```

PowerShell uses the same argument spelling; either enter each command on one line or replace every
shell `\` continuation above with PowerShell's backtick. `--network-disabled` is an operator
attestation: it does not disable an adapter or configure a firewall. An environment variable such
as `UV_OFFLINE=1`, proxy removal, or `--no-network` is not an OS-isolation substitute.

On either Mac architecture, the stronger harness-controlled alternative is:

```bash
uv run python scripts/offline_bundle_acceptance.py \
  --bundle release-output/whoopy-app-0.1.0-darwin-x86_64-py311 \
  --output release-evidence/bundles/darwin-x86_64.json \
  --models-dir models/managed \
  --lifecycle-output release-evidence/lifecycles/darwin-x86_64.json \
  --macos-sandbox-network-isolation
uv run python scripts/local_v1_acceptance.py \
  --scenario config/acceptance/local-v1-platform-v1.json \
  --output release-evidence/platforms/darwin-x86_64.json \
  --product-wheel release-input/whoopy-0.1.0-py3-none-any.whl \
  --config-dir config \
  --runs-dir runs \
  --models-root models \
  --required-pack kokoro \
  --macos-sandbox-network-isolation
```

This macOS mode first requires a reachable external TCP control, then re-executes the entire command
under `/usr/bin/sandbox-exec`, requires the same endpoint to fail with `EPERM` or `EACCES`, and
proves a localhost round trip. If `/usr/bin/sandbox-exec` is unavailable or the control endpoint
cannot be reached before isolation, use a real external network control plus the operator-attested
form instead. Never combine the two modes.

Do not rename a failing output to make it canonical. The two successful receipts for each target
must internally claim that same target. The installed receipt must additionally show
`offline_acceptance_passed: true`, `studio_lifecycle_passed: true`,
`workspace_model_readiness_unchanged: true`, and the shared product-wheel digest. The platform
receipt must show `current_machine_passed: true`, the exact
`local-v1-platform-v1` suite digest, one embedded `production_prompt_to_audio_case` receipt, and all
other machines as `not_run`.

### Import and aggregate without editing receipts

Copy the five platform JSON files and five installed-bundle JSON files to the frozen release
workspace. Keep the imported platform files at stable paths because the aggregate stores their
absolute paths and SHA-256 values. First build the strict platform aggregate:

```bash
uv run --extra dev python scripts/aggregate_local_v1_evidence.py \
  --scenario config/acceptance/local-v1-platform-v1.json \
  --report release-evidence/platforms/windows-x86_64.json \
  --report release-evidence/platforms/darwin-arm64.json \
  --report release-evidence/platforms/darwin-x86_64.json \
  --report release-evidence/platforms/linux-x86_64.json \
  --report release-evidence/platforms/linux-arm64.json \
  --output release-evidence/cross-platform-release.json
```

Then regenerate the consolidated status with the aggregate and all five installed receipts:

```bash
uv run python scripts/local_v1_acceptance_status.py \
  --project-root . \
  --cross-platform-evidence release-evidence/cross-platform-release.json \
  --installed-bundle-report release-evidence/bundles/windows-x86_64.json \
  --installed-bundle-report release-evidence/bundles/darwin-arm64.json \
  --installed-bundle-report release-evidence/bundles/darwin-x86_64.json \
  --installed-bundle-report release-evidence/bundles/linux-x86_64.json \
  --installed-bundle-report release-evidence/bundles/linux-arm64.json \
  --verify-model-digests \
  --output acceptance-reports/local-v1-status-current.json
```

The final command can still exit nonzero for a human-rating or promotion blocker. That is separate
from platform collection and must not be disguised by copying CI receipts into these paths.

## Consolidate without overstating portability

After validated candidate receipts have been atomically promoted to their canonical names, run:

```bash
uv run python scripts/local_v1_corpus_acceptance.py
uv run python scripts/local_v1_acceptance_status.py \
  --project-root . \
  --platform-report acceptance-reports/platform-darwin-arm64-current.json \
  --installed-bundle-report \
    acceptance-reports/offline-app-bundle-darwin-arm64-current.json \
  --verify-model-digests \
  --output acceptance-reports/local-v1-status-current.json
```

The command must remain nonzero until equivalent independent platform and installed-bundle
receipts exist for Windows x64, macOS x64, Linux x64, and Linux ARM64, and until the human corpus
and Lab decision gates pass. CI portability artifacts cannot replace those observations.
