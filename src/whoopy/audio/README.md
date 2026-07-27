# Audio

The dependency-free deterministic audio implementation lives here:

- `fixture.py` turns speech text into an audible test tone and creates exact silence;
- `synthesis.py` defines the replaceable speech boundary, cache inputs, and error taxonomy;
- `renderer.py` assembles timeline segments into one PCM WAV;
- `models.py` defines frame-range manifests and quality reports;
- `quality.py` validates segment PCM plus timing, hashes, joins, headroom, and the finished WAV.

The fixture is not a TTS adapter and should never be presented as narration
quality. Later production rendering may use FFmpeg and real speech adapters
while preserving the timeline and artifact contracts proven here.
