# Audio

Phase 2's dependency-free deterministic audio implementation lives here:

- `fixture.py` turns speech text into an audible test tone and creates exact silence;
- `renderer.py` assembles timeline segments into one PCM WAV;
- `models.py` defines frame-range manifests and quality reports;
- `quality.py` reads the finished WAV back and verifies its basic integrity.

The fixture is not a TTS adapter and should never be presented as narration
quality. Later production rendering may use FFmpeg and real speech adapters
while preserving the timeline and artifact contracts proven here.
