# Timeline

The canonical timeline schema, validation, and prose-to-timeline compiler belong
here. The timeline remains the source of truth for deterministic rendering.

Phase 1 implemented a deliberately small speech-only schema. Phase 2 adds
versioned `SILENCE` segments with exact positive millisecond durations and keeps
schema-v1 timelines readable. The fixture worker writes
`SPEECH -> SILENCE -> SPEECH`; later PRs add voice metadata, breath, music cues,
serialization migrations, and the deterministic cue compiler.
