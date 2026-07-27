# Blind Kokoro Voice Listening Rubric

Automatic audio checks can detect clipping, corruption, incorrect pauses, and
container errors. They cannot decide whether a voice feels warm, calming, or
comfortable over several minutes. That decision requires listening.

## Prepare Anonymous Samples

The pinned Kokoro v1.0 bundle contains 53 speaker IDs. Whoopy exposes four
reviewed English candidates from the official mapping: `af_bella` (2),
`af_heart` (3), `af_nicole` (6), and `am_michael` (16).

Generate anonymous A/B/C/D files from the same script and settings:

```bash
uv run --offline python scripts/prepare_voice_bakeoff.py \
  --script-file examples/first-meditation.md \
  --models-dir models/managed \
  --output-dir evaluations/local/voices-v1
```

Do not open `answer-key.json` until the review is complete. The script refuses
to overwrite an existing directory, shuffles labels with a recorded seed,
renders every candidate through the same trim/fade/normalization path, and
writes a quality report beside each WAV.

The speaker names and IDs are documented by the
[official sherpa-onnx Kokoro v1.0 page](https://k2-fsa.github.io/sherpa/onnx/tts/pretrained_models/kokoro.html).
The upstream Kokoro voice card describes `af_heart` as grade A and
`af_bella` as A-, while explicitly noting that listener preference is
subjective. Those upstream grades are useful candidate filters, not Whoopy's
listening result.

## Listening Procedure

Use the same headphones or speakers, volume, room, and playback application.
Avoid looking at waveform lengths or metadata. Listen once in randomized order,
take a break, then listen in a different order. A three- to five-minute script
is preferable to a very short phrase because meditation comfort is sustained.

For every anonymous sample, rate 1 (poor) through 5 (excellent):

| Criterion | What to notice |
|---|---|
| Naturalness | Does it sound like continuous human speech rather than stitched phonemes? |
| Warmth | Does the tone feel welcoming without becoming theatrical? |
| Calmness | Is energy suitable for meditation without sounding flat or sleepy by accident? |
| Intelligibility | Are words, names, and sentence endings clear? |
| Pacing | Does the voice rush or drag within spoken sections? |
| Pause transitions | Do speech edges enter and leave explicit silence comfortably? |
| Absence of artifacts | Listen for clicks, buzz, warble, skipped words, or odd emphasis. |
| Long-listen comfort | Would this remain comfortable for a five-minute meditation? |

Also record:

- preference rank with no ties;
- any sentence that sounded incorrect;
- whether a different speed might change the ranking; and
- the listening device and approximate volume.

Only after saving `review.json` should the reviewer open `answer-key.json`.

## Decision Rule

Do not choose a voice from one overall average alone. A candidate must:

- pass every machine audio-quality check;
- have no repeated severe pronunciation or artifact issue;
- score at least 4 for intelligibility and pause transitions;
- score at least 3 for every other criterion; and
- be preferred after a second listening session.

If no voice clears the rule, keep `af_heart` as a provisional integration
default and record that human selection is incomplete. Changing the default
changes cache identity but does not change the timeline, worker, renderer, or
model port.
