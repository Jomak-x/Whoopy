# PR 13: Durable Recovery

This guide explains the recovery work in the current Whoopy PR in plain
language. All 179 local tests and static checks pass, but it is **not merged or
accepted** until remote macOS, Linux, and Windows CI pass.

The aim is simple: closing a terminal, closing the browser, a laptop sleeping,
or a model process hanging must not leave a meditation pretending to run
forever. Completed work remains available to inspect and reuse.

## The recovery model

```text
browser or CLI -> durable run UUID -> run.json + artifacts
                                      |
                                      v
                              one locked worker attempt
                                      |
                         heartbeat + stage + checkpoints
                                      |
                 finish / fail / interrupt / lease expires
                                      |
                                      v
                    inspect, reconcile, resume, or regenerate
```

Whoopy uses local files instead of a database at this stage. Each run has a
directory under `runs/`, named with a UUID. Atomic file replacement prevents a
half-written JSON file from becoming the saved record.

## UUIDs, status, and stage

A UUID is a large randomly generated identifier, for example
`7b3d85d0-40e3-4ab7-8e8b-59d5601cf0a6`. Whoopy allocates it before planning or
speech begins. The browser can therefore follow one durable run immediately,
and all later files belong to that identifier:

- `run.json` stores lifecycle state.
- `plan.json`, drafted sections, and `script.md` store generation evidence.
- `timeline.json` records exact speech and silence.
- Checkpoints and cached segments preserve reusable work.
- `narration.wav`, its manifest, and `quality.json` appear only after success.
- `events.jsonl` holds a bounded history of lifecycle events.

UUID input is validated before it becomes a path, so malformed arguments cannot
reach other directories.

`status` answers the overall question:

| Status | Meaning |
| --- | --- |
| `queued` | The request is saved but not claimed. |
| `running` | One worker has a renewable lease. |
| `interrupted` | The worker stopped; checkpoints remain resumable. |
| `completed` | Timeline, WAV, manifest, and quality report were saved. |
| `failed` | Whoopy captured an error; healthy checkpoints may still be reused. |

`stage` shows where the work is or stopped: `queued`, `planning`, `drafting`,
`compiling`, `waiting_for_model_slot`, `model_startup`, `synthesizing`,
`assembling`, `quality_check`, or `completed`.

The run record also retains owner ID, OS process ID, attempt ID, start time,
current segment, last heartbeat, lease expiry, interruption kind, and a short
user-facing message. Code comments explain the non-obvious lifecycle rules,
because an incorrect “running” label is worse than a visible failure.

## Heartbeats, leases, and locks

A worker renews its heartbeat every two seconds. Each heartbeat extends a
15-second lease: a durable statement that this particular attempt is still
alive until a particular time. A crashed process cannot renew it.

When Whoopy starts, it reconciles expired leases. An abandoned `running`
record becomes `interrupted` with an explanation that completed checkpoints
were retained. Older records without a lease use their last update time and
the same 15-second grace period.

The lease detects abandoned work. A per-run operating-system lock prevents two
live workers from writing the same run at once. The lock uses the platform’s
native advisory mechanism and the OS releases it if the process dies. The
second worker fails clearly instead of racing over checkpoints or final audio.

## Events, logs, timeouts, and cleanup

`events.jsonl` is structured diagnostic history. Both it and the human-readable
`worker.log` rotate at 5 MiB and retain one bounded predecessor, so repeated
diagnostics cannot grow forever. An event can
include its run, time, attempt, stage, segment, kind, message, and safe details.

Fish and MOSS run behind a JSON-lines subprocess controller. It has separate
startup, request, and shutdown timeouts; bounded stderr/lifecycle diagnostics;
and validation for malformed or mismatched responses. A graceful shutdown that
takes too long terminates the worker’s process group; if that still does not
finish, the group is killed. This also cleans up child processes started by
model runtimes.

Every speech adapter has `close()`. Worker cleanup calls it after success,
failure, or cancellation so a model cannot keep memory or a child process. A
timeout becomes a recorded, recoverable failure instead of an endless wait.

## Failure and recovery behavior

| Situation | Durable result | Next action |
| --- | --- | --- |
| Browser or terminal stops normally | Work remains; interruption is recorded where possible. | Resume. |
| Process is killed or laptop sleeps | The heartbeat expires. | Reconcile, then resume. |
| Model hangs or exits | Diagnostics are retained and the run fails safely. | Inspect events; resume or regenerate. |
| One segment has bad audio | Bounded retries run; bad audio is not reused. | Regenerate that segment or resume. |
| Two workers start | The second cannot acquire `worker.lock`. | Keep the first or wait for expiry. |
| Assembly or QC fails | Incomplete final output is never called completed. | Fix the cause and resume. |

Resuming does not blindly trust earlier work. Whoopy validates the timeline,
checkpoint, cache key, PCM data, and final quality artifacts before reuse.

## Commands and studio actions

Run these from the repository root. Add `--runs-dir PATH` if your runs live
somewhere else.

```bash
# Inspect one durable run.
uv run --offline whoopy run show RUN_ID

# Mark expired running records as safely resumable interruptions.
uv run --offline whoopy run reconcile
uv run --offline whoopy run reconcile RUN_ID

# Continue from verified checkpoints.
uv run --offline whoopy run resume RUN_ID

# Ask the verified local owner process to stop and preserve resumable state.
uv run --offline whoopy run cancel RUN_ID

# Rebuild only one named speech segment.
uv run --offline whoopy run regenerate-segment RUN_ID SEGMENT_ID

# Start the local studio, which reads the same durable records.
uv run --offline whoopy web --open
```

The studio surfaces durable state rather than inventing browser-only progress:
status, stage, heartbeat, lease expiry, current segment, failure message, and
recovery counters. It can cancel the child it started, resume a saved run, or
request a segment regeneration. Restarting the web server does not erase the
run; the next server reads `run.json` again. Cancellation never deletes a run,
checkpoint, or cache entry.

## Tests and review checklist

The automated suite now proves:

- records reject contradictory status, stage, ownership, or lease data;
- a browser-generated UUID is durable before its child starts;
- one attempt cannot heartbeat over a different attempt’s ownership;
- expired and legacy `running` records reconcile to `interrupted`;
- a second worker cannot acquire a run lock;
- completed checkpoints survive interruption and are reused;
- failure, cancellation, assembly, and QC do not leave `running` behind;
- events remain valid JSON lines inside their configured bounds;
- malformed, exiting, and hung model workers report diagnostics and clean up;
- browser APIs preserve durable cancel, resume, and regeneration behavior
  across a web-server restart.

It also launches a real child worker, force-kills it during the second speech
segment, reconciles its lease, and proves the first segment checkpoint is
reused. The two abandoned runs that existed before this PR were reconciled to
`interrupted` locally without deleting their model data or generated artifacts.

Manual review before merge:

1. Start a longer run and note its UUID.
2. Stop its worker during synthesis, then reopen the studio or run reconcile.
3. Confirm `interrupted`, its last stage, and completed segment count appear.
4. Resume it and confirm healthy segments are not synthesized again.
5. Start a second resume while the first is active and confirm the lock rejects
   it without changing the run.
6. Trigger a controlled model timeout; confirm diagnostics stay bounded, the
   child process is gone, and the run is resumable.
7. Run the full check and wait for macOS, Windows, and Linux CI before calling
   the work machine-verified.

## Boundaries of this PR

PR 13 makes recovery trustworthy enough for wider local model testing. It does
not choose a voice, prove meditation quality, install new model packs, or build
the final product interface. Those remain separate, reviewable steps in the
[Local-First Master Plan](./local-first-master-plan.md).
