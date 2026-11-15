# Optional local word screening

Completing narration means PCM rendering completed. It does not establish that
spoken words match the timeline. Studio's explicit **Check spoken words** action
starts an independent, local check; it does not change the run's completed state,
canonical audio, timeline, export eligibility, or human-listening assessment.

The current implementation supports English on macOS Apple Silicon using an
already installed MLX Whisper environment and local model snapshot. Other hosts,
missing installations, unsupported languages, cancellation, timeout, and process
errors remain **unverified**. They are not synthesis failures. No packages or
weights are downloaded and no paid service is called.

## Local installation record

A trusted local operator supplies `<models-root>/verification/speech-screening.json`.
The browser cannot choose an interpreter, model path, or decoding controls. The
strict JSON format is:

```json
{
  "schema_version": 1,
  "engine": "mlx-whisper",
  "python_executable": "/absolute/existing/asr-venv/bin/python",
  "python_sha256": "<64 lowercase hex characters>",
  "model_directory": "/absolute/existing/immutable-model-revision",
  "weights_sha256": "<64 lowercase hex characters>",
  "config_sha256": "<64 lowercase hex characters>"
}
```

Choose the existing virtual environment's `bin/python` path (retain its venv
symlink), and an exact local snapshot containing `weights.npz` and `config.json`.
Do not use a repository identifier, URL, or mutable `refs/main` lookup. The model
path is resolved to its local directory before the request is frozen. Hashes pin
file contents; they are not a subjective qualification of this Whisper model or
its environment. Availability means the configured local files are present;
full content checks happen in the isolated coordinator before ASR starts.

For an already selected installation, this command prints a candidate record
without importing MLX, running a model, or changing configuration. Review and save
its output to the path above using the same models root passed to Studio:

```sh
python3 - /absolute/asr-venv/bin/python /absolute/model-snapshot <<'PY'
import hashlib, json, pathlib, sys
python, model = map(pathlib.Path, sys.argv[1:])
def sha(path):
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()
print(json.dumps(dict(schema_version=1, engine='mlx-whisper',
    python_executable=str(python.absolute()), python_sha256=sha(python),
    model_directory=str(model.resolve(strict=True)),
    weights_sha256=sha(model/'weights.npz'),
    config_sha256=sha(model/'config.json')), indent=2))
PY
```

No live installation record is created by tests or by starting Studio. Runtime
package versions, interpreter digest, model hashes/revision, source hashes, exact
decoding settings, and timestamps are retained for attribution. Pinning Python's
binary does not pin every installed package; recorded versions make this limit
visible. Model files and Python are rehashed at launch, model files also after
recognition. Expected words are never sent to Whisper as a prompt.

## Operation and evidence

HTTP endpoints under `/api/v1/runs/{run_id}`:

- `GET speech-screen`: independent operation and word status.
- `POST speech-screen` with `{}`: start a fresh attempt (202).
- `POST speech-screen/{attempt_id}/cancel` with `{}`: write a cancellation marker.
- `GET speech-screen/{attempt_id}/report`: evidence for that exact attempt.

The CLI equivalent is `whoopy speech-screen RUN_ID --runs-dir RUNS --models-dir MODELS`;
add `--status` to read current status without starting work. Starting is asynchronous.
Reloading or restarting the API does not own or terminate the coordinator. Queued
and running checks block run deletion and additional heavyweight generation.
The coordinator holds the run lock and heavyweight slot. Only the ASR leaf takes
the shared benchmark GPU flock, so waiting for GPU access counts toward the
15-minute operation deadline. It admits at least 4 GiB available RAM, then stops
on available RAM below 2 GiB sustained for two seconds or swap growth over 4 GiB.
Cancellation/deadline/resource stops terminate only children launched by this
coordinator. Saved PIDs are checked with host and process creation time for status;
HTTP cancellation never signals a PID recovered from disk.

Each fresh `speech-screening/<attempt-id>/` retains frozen expected text, timeline,
actual ASR input WAV, source snapshots, request, unprompted ASR JSON, comparison
report, final receipt, process commands/timings, stdout/stderr, and sampled system
memory. Existing evidence is never overwritten. `latest.json` selects the newest
attempt. A worker that died without a final receipt is interrupted, not passed.

Word verdicts use the existing `normalized-word-mismatch-review-v3` verifier:
normalized exact words may pass if PCM and other checks pass; any normalized word
edit requires review; substantial mismatch, missing words, or other hard checks
fail. ASR can misrecognize legitimate audio: **review/fail require listening** and
are not human approval or definitive attribution to the synthesizer. The report
retains both transcripts, alignment, and reasons. `human_review` stays
`not_assessed`.

Every status/report read checks the current canonical WAV, timeline, and expected
text hashes against that attempt plus frozen proof hashes and the required word
policy. Changed canonical inputs make historical verdicts **stale**. Tampered or
inconsistent proof is **unverified**. Removing/updating the installed runtime does
not invalidate otherwise intact historical evidence; it only prevents a new
check. Older reports using other word policies are never relabeled as current-v3
passes. Historical receipts retain their recorded model revision and check date.
