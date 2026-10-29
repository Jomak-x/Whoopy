# Audio

The deterministic audio implementation lives here:

- `fixture.py` turns speech text into an audible test tone and creates exact silence;
- `synthesis.py` defines the replaceable speech boundary, cache inputs, and error taxonomy;
- `renderer.py` assembles timeline segments into one PCM WAV;
- `models.py` defines frame-range manifests and quality reports;
- `quality.py` validates segment PCM plus timing, hashes, joins, headroom, and the finished WAV;
- `exports.py` creates and verifies the finished WAV, FLAC, M4A/AAC, and Opus set.

The fixture is test support and must never be presented as narration quality.
Production workers use verified speech adapters, deterministic processing, and
FFmpeg/FFprobe export validation while preserving these timeline and artifact
contracts.
