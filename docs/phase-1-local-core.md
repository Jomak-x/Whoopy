# Phase 1: The First Local-Core Flow

Phase 1 makes Whoopy's architecture executable without pretending that models
or audio already exist. A prompt becomes a saved run, a separate worker
processes the run, and a canonical timeline artifact appears on disk.

## The Flow

```text
whoopy run create "A calm pause."
              |
              v
LocalControlPlane.submit_prompt()
              |
              v
runs/<uuid>/run.json  [status: queued]
              |
              v
whoopy worker process <uuid>
              |
              v
run.json              [status: running]
              |
              v
timeline.json          [one SPEECH segment]
              |
              v
run.json              [status: completed]
```

The two commands are deliberately separate. The control plane accepts and
records work quickly. The worker owns slow processing. A future web API can
submit through the same control-plane service, and a future queue can invoke the
same worker boundary.

## Vocabulary

### Prompt

The user's request, such as:

```text
A ten-minute sleep meditation with slow breathing.
```

Phase 1 stores this exact text. It does not ask an LLM to turn it into a real
script yet.

### Run

One attempt to process one prompt. Every run receives a random UUID, for example:

```text
6e13a134-bd2b-407d-9ff4-e5b919758183
```

A UUID is a 128-bit identifier designed to be unique without a central counter.
Whoopy also requires a valid UUID before building a path, preventing input such
as `../../outside` from escaping the run directory.

### Run record

`run.json` is the durable state of the job. It records:

- schema version;
- run ID;
- current status;
- original prompt;
- creation and update timestamps;
- timeline filename after success;
- error message after failure.

“Durable” means the information survives after the command exits.

### Control plane

The control plane accepts commands and exposes state. In Phase 1,
`LocalControlPlane` can submit a prompt and retrieve a run. It never performs
worker processing inline.

This separation prevents a future HTTP request or UI action from needing to stay
open while models and audio tools work for several minutes.

### Worker

The worker performs the actual job. Phase 1's `LocalWorker` processes one named
run in the foreground. It is not yet a continuously running daemon.

### Artifact

An artifact is a file produced by a run. Phase 1 produces:

- `run.json`, the mutable job record;
- `timeline.json`, the validated result of successful processing.

Later phases add script, segment audio, logs, masters, and delivery files.

### Timeline

The timeline is the structured source of truth that future renderers consume.
The Phase 1 schema is intentionally minimal:

```json
{
  "schema_version": 1,
  "run_id": "6e13a134-bd2b-407d-9ff4-e5b919758183",
  "source": "phase_1_prompt_passthrough",
  "segments": [
    {
      "id": "speech-0001",
      "type": "SPEECH",
      "text": "A calm pause."
    }
  ]
}
```

The `source` field makes the limitation explicit: this is prompt passthrough,
not generated meditation prose. Later schemas add silence, voice, delivery,
breathing, music cues, and model metadata.

## Run State Machine

```text
queued ------> running ------> completed
                  |
                  +----------> failed
```

- `queued`: saved and waiting for a worker;
- `running`: claimed by the foreground worker;
- `completed`: `timeline.json` was written and referenced by the record;
- `failed`: processing raised an error and the record contains its message.

Only queued runs can be processed. This prevents accidentally processing a
completed run twice.

## Filesystem Layout

With the default `pipeline.checkpoint_dir: ./runs`, one completed run looks like:

```text
runs/
└── <run-id>/
    ├── run.json
    └── timeline.json
```

`runs/` is ignored by Git because these are local runtime artifacts, not source
code.

Important JSON files use atomic replacement:

1. write complete JSON to a temporary file beside the destination;
2. ask the operating system to replace the destination with that file;
3. remove any leftover temporary file.

This reduces the chance that another reader mistakes half-written JSON for a
valid record.

## Try It

Create a run:

```bash
uv run whoopy run create "A calm one-minute pause."
```

The status is `queued`, and no timeline exists yet. Copy the printed ID:

```bash
uv run whoopy worker process <run-id>
```

The worker writes the timeline and changes the status to `completed`:

```bash
uv run whoopy run show <run-id>
```

Add `--json` to any of these commands when another program needs
machine-readable output.

Use a temporary location without changing configuration:

```bash
uv run whoopy run create "Test prompt" --runs-dir ./scratch-runs
```

## Code Map

- `src/whoopy/control.py` — control-plane submission and lookup;
- `src/whoopy/pipeline/runs.py` — run models, UUID safety, and atomic storage;
- `src/whoopy/pipeline/worker.py` — lifecycle transitions and timeline creation;
- `src/whoopy/timeline/models.py` — validated timeline and speech segment;
- `src/whoopy/cli.py` — terminal transport for those services;
- `tests/test_runs.py` — persistence, validation, and path-safety tests;
- `tests/test_worker.py` — completion, duplicate processing, and failure tests;
- `tests/test_cli.py` — complete command-level flow.

## Honest Limitations

Phase 1 does not contain:

- LLM script generation;
- a real meditation script;
- silence or breathing segments;
- TTS or audio;
- FFmpeg;
- a polling queue;
- concurrent worker claims or file locking;
- retry or resume;
- a database, API, or UI.

It proves only the smallest valuable architectural statement: input can be
durably recorded, slow work can happen behind a worker boundary, lifecycle state
is inspectable, and successful work produces a validated timeline artifact.
