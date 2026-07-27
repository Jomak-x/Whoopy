# Phase 2: Deterministic Audio Assembly

Phase 2 makes a canonical Whoopy timeline produce a real, playable audio file.
The narration is not a human voice yet. `SPEECH` segments become soft,
deterministic fixture tones so timing and assembly can be tested without a TTS
model.

## The Complete Flow

```text
prompt
  |
  v
run.json [queued]
  |
  v
foreground worker
  |
  +--> timeline.json
  |      SPEECH -> SILENCE -> SPEECH
  |
  +--> fixture PCM for each SPEECH
  +--> zero-valued PCM for each SILENCE
  |
  +--> narration.wav
  +--> audio-manifest.json
  +--> quality.json
  |
  v
run.json [completed]
```

The worker marks a run completed only after all four output artifacts have been
written and the WAV has passed the quality gate.

## Audio Vocabulary

### Sound sample

Digital audio stores a rapid sequence of numbers. Each number describes the
speaker position at one instant. One number is a sample.

### Sample rate

Whoopy's fixture audio uses 24,000 samples per second:

```text
sample_rate = 24,000 Hz
```

Therefore, exactly 1,500 milliseconds of silence contains:

```text
1.5 seconds × 24,000 samples = 36,000 samples
```

### Frame

A frame contains one sample for every channel at one moment. Phase 2 is mono, so
one frame contains one sample. In stereo, one frame would contain left and right
samples.

### Bit depth

Each Phase 2 sample is a signed 16-bit integer. Its possible range is roughly
`-32768` to `32767`. Every mono frame therefore occupies two bytes.

### PCM

PCM means pulse-code modulation. It is the direct sequence of sample numbers
before compression.

### WAV

WAV is a container. It places a small header around PCM data so players know the
sample rate, channel count, sample width, and audio length.

Phase 2 writes:

```text
24 kHz, mono, signed 16-bit PCM WAV
```

This is intentionally simple and lossless. Later renderers add production
sample rates, FLAC masters, and AAC/Opus delivery files.

## Canonical Timeline Version 2

Phase 2 adds exact `SILENCE` segments:

```json
{
  "schema_version": 2,
  "source": "phase_2_fixture_meditation",
  "segments": [
    {
      "id": "speech-0001",
      "type": "SPEECH",
      "text": "A calm grounding meditation."
    },
    {
      "id": "silence-0001",
      "type": "SILENCE",
      "duration_ms": 1500
    },
    {
      "id": "speech-0002",
      "type": "SPEECH",
      "text": "The deterministic Phase 2 fixture is complete."
    }
  ]
}
```

Schema version 1 remains readable so runs created during Phase 1 do not suddenly
become invalid.

## Fixture Speech

The fixture synthesizer converts each speech segment into a soft triangle-wave
tone.

Its properties are intentional:

- no model or internet connection is required;
- duration is derived deterministically from word count;
- frequency is derived deterministically from UTF-8 text bytes;
- integer arithmetic gives repeatable PCM across supported systems;
- a short fade at both ends reduces clicks;
- amplitude remains far below clipping.

It does not simulate voice quality. If you hear tones separated by silence, the
fixture is doing its job.

## Exact Silence

A `SILENCE` segment is not punctuation and not an instruction to a model.
Whoopy converts its milliseconds into a whole frame count:

```text
frames = round(duration_ms × sample_rate / 1000)
```

It then writes exactly that many zero-valued samples.

The quality gate reads the completed WAV back and confirms every sample in the
declared silence range is zero.

## Audio Manifest

`audio-manifest.json` maps every timeline segment to an exact WAV range:

```json
{
  "sample_rate": 24000,
  "total_frames": 93840,
  "segments": [
    {
      "segment_id": "speech-0001",
      "start_frame": 0,
      "end_frame": 20160,
      "frame_count": 20160
    }
  ]
}
```

The next segment must start exactly where the previous segment ended. This makes
gaps and overlaps detectable.

## Quality Gate

`quality.json` records individual checks:

- mono channel count;
- 16-bit sample width;
- 24 kHz sample rate;
- expected total frame count;
- expected duration;
- contiguous segment joins;
- exact zero-valued silence;
- audible fixture data in speech ranges;
- no clipped samples.

The report has `passed: true` only when every check passes. Tests deliberately
corrupt a silence sample to prove that the gate catches it.

## Run Record Version 2

A completed Phase 2 `run.json` references:

```json
{
  "timeline_artifact": "timeline.json",
  "audio_artifact": "narration.wav",
  "audio_manifest_artifact": "audio-manifest.json",
  "quality_artifact": "quality.json"
}
```

A queued, running, or failed record cannot claim to own completed artifacts.
Older Phase 1 records remain valid with only `timeline_artifact`.

## Try It

```bash
uv run whoopy run create "A calm one-minute grounding meditation."
uv run whoopy worker process <run-id>
uv run whoopy run show <run-id>
```

Open:

```text
runs/<run-id>/narration.wav
```

You should hear a tone, exactly 1.5 seconds of silence, and a second tone.

## Code Map

- `src/whoopy/timeline/models.py` — speech/silence timeline contract;
- `src/whoopy/audio/fixture.py` — deterministic tone and zero-silence PCM;
- `src/whoopy/audio/renderer.py` — timeline-order WAV assembly;
- `src/whoopy/audio/models.py` — manifest and quality report models;
- `src/whoopy/audio/quality.py` — read-back audio checks;
- `src/whoopy/pipeline/runs.py` — versioned artifact persistence;
- `src/whoopy/pipeline/worker.py` — end-to-end worker orchestration;
- `tests/test_audio.py` — timing, reproducibility, and corruption checks.

## Honest Limitations

Phase 2 does not include:

- human speech or a TTS model;
- LLM script generation;
- editable pause cues;
- breathing or music segments;
- FFmpeg, FLAC, AAC, or Opus;
- segment cache files;
- retry, resume, or parallel rendering;
- loudness mastering or advanced acoustic QC;
- a committed web interface.

The purpose is narrower: prove that timeline timing survives conversion into
real audio before introducing model and production-renderer complexity.
