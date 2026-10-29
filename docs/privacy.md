# Privacy And Local Data

Whoopy Local V1 is designed to operate on one computer without accounts, outbound telemetry, cloud
inference, or public publishing. It does save local hardware and performance measurements in run
evidence so results can be reproduced and compared without sending them anywhere.

## What Stays Local

Prompts, authored scripts, meditation plans, raw model output, timelines, audio segments, reference
voices, model files, ratings, finished audio, and logs are stored in local filesystem paths. The app
does not contain a cloud fallback endpoint, and `hardware.allow_remote_fallback` defaults to false.

Network access may occur when you explicitly install Python packages or model files. After
acquisition, local inference is configured to run offline. `uv run --offline` prevents package
fetches; disabling the operating system's network adapter or using a firewall is the stronger test
that no process can reach a network.

## Default Data Map

Paths are relative to the directory where Whoopy is launched unless overridden:

| Path                               | Contents                                                                        | Authority                         |
| ---------------------------------- | ------------------------------------------------------------------------------- | --------------------------------- |
| `runs/<uuid>/`                     | Complete meditation inputs, state, checkpoints, revisions, audio, exports, logs | Authoritative                     |
| `runs/.cache/segments/`            | Reusable verified speech PCM and metadata                                       | Rebuildable optimization          |
| `runs/.whoopy-catalog.sqlite3`     | Library search, local UI settings, and versioned Lab search rows                 | Rebuildable projection            |
| `runs/.whoopy-lab/`                | Experiment definitions, reports, blind assignments, ratings, decisions          | Authoritative Lab evidence        |
| `runs/.trash/`                     | Recoverably removed runs                                                        | Recoverable until manually purged |
| `models/managed/`                  | Pinned downloads, installed runtimes/models, verification/smoke evidence        | Managed local assets              |
| `models/managed/voice-references/` | Consented reference audio, transcript, trims, revisions, previews               | Sensitive authoritative data      |

The versioned loopback API can report resolved paths and export the catalog's JSON settings. The
retained Studio frontend does not expose those controls. A settings envelope contains settings and
path information, not model weights or meditation audio.

## Loopback Security

`whoopy app` binds to `127.0.0.1` by default and accepts only loopback hostnames. Mutating requests
with a foreign Origin are rejected, bodies are bounded, and managed paths are validated. This
prevents ordinary remote access; it does not protect against malware or another process already
running as your operating-system user.

Use a locked OS account, disk encryption, and normal backup hygiene when prompts, reflections, or
voice references are sensitive. Do not expose port 8765 through a reverse proxy, tunnel, container
port mapping, or router.

## Voice Data

Reference audio can be identifying biometric-like data. The voice-reference API requires a
transcript and the exact rights attestation before import. Managed references preserve revisions and
move deletions to recoverable trash, so deletion does not immediately erase every byte.

Before importing a voice:

- obtain clear permission for local cloning and intended uses;
- avoid recordings containing unrelated people's voices or private background speech;
- keep transcripts accurate;
- review the selected model's license;
- do not impersonate, deceive, harass, or bypass consent.

The backend accepts an existing PCM16 WAV of up to 60 seconds plus its exact transcript and consent
attestation; preview generation remains local. The retained Studio frontend does not include a
microphone or voice-reference workflow. Deleting through the API moves the managed reference to
trash; it is not immediate secure erasure.

To fully purge a reference or run, first stop Whoopy, identify the exact managed trash entry, and
remove it using an appropriate recoverable/secure-delete policy for your storage device. Backups may
retain older copies.

## Logs And Evidence

Worker logs and raw model attempts are intentionally preserved for diagnosis and reproducibility.
They can contain prompt or generated text. Acceptance reports record commands, output tails, paths,
hardware class, model state, and tool versions; review them before sharing publicly.

Model readiness uses a one-way machine identifier derived from local host/platform facts. It scopes
smoke evidence to one machine without storing the raw hostname in the evidence record.

## Sharing Finished Audio

Local creation does not grant redistribution rights. A finished file can include model/title/date
metadata, while its adjacent run contains richer provenance. Before sharing:

- listen for unsafe or misleading content;
- confirm all voice and model licenses permit the use;
- avoid sharing raw reference audio, transcripts, prompts, or logs by accident;
- remember that Fish Speech's managed Local V1 declaration is non-commercial;
- keep a copy of `model-metadata.json` and `export-manifest.json` when provenance matters.

Commons, public publishing, user accounts, and cloud storage are not part of Local V1.
