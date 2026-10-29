# Timeline

The canonical timeline schema, validation, and prose-to-timeline compiler belong
here. The timeline remains the source of truth for deterministic rendering.

The versioned schema represents typed `SPEECH`, exact `SILENCE`, and reviewed
`BREATH` events while retaining legacy timeline readability. Speech carries
delivery, intensity, language, voice mode, pace, target timing, and
pronunciation intent. The deterministic compiler removes supported standalone
controls from spoken text and rejects unknown controls.
