# Local voice auditions

Optional trusted local auditions live under `models/voice-previews/`. No model is
loaded and nothing is downloaded by this service. A missing `index.json` produces
an empty catalogue. Operators supply recordings; the repository does not create
a production manifest or qualify test tones as speech.

The strict manifest format is:

```json
{
  "schema_version": 1,
  "previews": [
    {
      "model_id": "kokoro",
      "voice_id": "af_bella",
      "path": "kokoro/af_bella.wav",
      "sha256": "<64 lowercase hexadecimal characters>",
      "model_revision": "<actual source model revision>",
      "recorded_at": "2026-11-05",
      "human_quality": "unrated",
      "label": "Bella",
      "source": "<local recording provenance>",
      "controls": {"pace": "slow", "speed": 0.8}
    }
  ]
}
```

`label`, `source`, and `controls` are optional. Paths are canonical relative WAV
paths beneath the preview directory; absolute paths, URLs, traversal, and
symlinks are rejected. Identifiers match exact backend and voice IDs, including
case. Duplicate pairs, unknown fields, or invalid records invalidate the manifest.
The manifest is bounded to 128 KiB and 128 entries. Each WAV is bounded to 32 MiB,
180 seconds, one or two channels, and uncompressed integer PCM. Every lookup
rehashes the complete file and checks the container, header, and complete frames.
An invalid recording is omitted without hiding other valid recordings.

Use `VoicePreviewService(models_root)`. `urls(model_id)` returns verified
`voice_id: API URL` mappings. `status()` returns public metadata under `entries`,
individual file failures under `rejected`, and an optional manifest `error`.
`lookup(model_id, voice_id)` returns `None` or a `VoicePreview` with `path`, `url`,
`metadata`, and `wav_bytes`. Serve **`wav_bytes`**, rather than reopening `path`:
these are the complete original bytes checked by that lookup. The intended URL
is `/api/v1/voice-previews/{model_id}/{voice_id}`. The service does not itself add
HTTP routes.

SHA256 and PCM validation establish file integrity and playable structure. They
do not establish correct speech, pronunciation, pleasantness, or human listening
approval. Metadata always retains `human_quality: "unrated"` and
`managed_ready: false`. A preview is independent of managed model readiness.
