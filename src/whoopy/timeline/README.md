# Timeline

The canonical timeline schema, validation, and prose-to-timeline compiler belong
here. The timeline remains the source of truth for deterministic rendering.

Phase 1 implements a deliberately small schema: a versioned timeline containing
one or more `SPEECH` segments. Its worker writes one prompt-passthrough segment
so storage and orchestration are real without pretending script generation has
been implemented. Later PRs add silence, breath, music cues, serialization
migrations, and the deterministic cue compiler.
