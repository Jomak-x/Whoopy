# Phase 3.6: Meditation Pacing And Fish Speech Evaluation

This change fixes a real listening problem discovered after Phase 3.5. The
audio pipeline was technically correct, but the result did not yet feel like a
good guided meditation.

## What The Log And Run Artifacts Revealed

The inspected five-minute-style run contained 698 spoken words. Kokoro
articulated them at about 174 words per minute. Only five explicit pauses
existed, totalling 35 seconds, while speech continued for about 241 seconds.
Some uninterrupted speech blocks lasted roughly 36 seconds.

This explains the listening experience: a fast paragraph, one obvious gap,
then another fast paragraph. Making that one gap longer would not solve the
problem. Both the narration itself and the spaces between individual
instructions needed to change.

The `BrokenPipeError` entries were unrelated to generation. The browser stopped
an in-progress audio download when the player was closed or repositioned. The
server now streams WAV files with HTTP byte-range support and treats a closed
player as a normal cancellation.

## Research Applied

- [Headspace says mindfulness breathing should remain natural](https://help.headspace.com/hc/en-us/articles/215058708-Is-There-a-Particular-Way-I-Should-Breathe).
  Whoopy therefore rejects breath holding and several forms of prescribed deep
  or timed breathing.
- A 2022 study using a script partly derived from an introductory Headspace
  meditation explicitly wrote pauses into the script and instructed human
  readers to speak slowly and clearly. It also found that human voices were
  rated as more enjoyable, useful, and relaxing than the tested synthetic
  voices: [Menhart and Cummings, 2022](https://assets.pubpub.org/bkxfoz76/11667413842671.pdf).
- Speech research describes guided-meditation delivery in terms of slower rates
  and prolonged pauses: [Niebuhr and Hacker, 2026](https://doi.org/10.21437/SpeechProsody.2026-164).

These sources support the direction, but they do not establish one universal
"Headspace pause duration." Whoopy's exact values are an explicit product
decision that can be measured and revised.

## The New Pacing Contract

Generated narration now uses three deterministic pause levels:

| Just spoken | Silence |
| --- | ---: |
| ordinary short sentence | 1.8 seconds |
| body, sensation, or settling invitation | 2.8 seconds |
| breath-awareness invitation | 4.5 seconds |
| section-ending practice period | 6–20 seconds |

Every generated sentence becomes one `SPEECH` segment followed by one
`SILENCE` segment. The pause markers are saved in `script.md`, compiled into
`timeline.json`, and then rendered as exact zero-valued PCM frames. This means:

- a listener hears space after each idea;
- the rhythm survives retries and resumes;
- each short sentence is independently cached;
- a reviewer can inspect every pause without listening;
- tests can verify the exact duration of every silence.

The default Kokoro speed changed from `0.9` to `0.6`. A real-device calibration
with the same 33-word passage measured approximately:

| Kokoro speed | Articulation |
| ---: | ---: |
| 0.8 | 163 words/minute |
| 0.7 | 139 words/minute |
| 0.6 | 122 words/minute |

The browser now labels `0.6` as **Meditative**, with `0.55`, `0.7`, and `0.8`
available as deliberate alternatives.

## Script Generation Changes

The local LLM is now asked for:

- one instruction or observation per sentence;
- roughly 6–14 words per sentence and never more than 22;
- three focused sections for practices up to three minutes;
- a direct section-specific opening instead of repeated generic arrivals;
- natural breathing with no breath holding.

The model is still untrusted. If it returns a verbose section, Whoopy
deterministically selects complete sentences that best match the section
purpose and fit the time budget. It never cuts a sentence in the middle. Short
plans are also compacted around the user's requested theme while preserving an
arrival and return.

## Measured End-To-End Result

The final real two-minute test is saved under run
`3c5a42ea-a8a1-47b2-90e4-4dc4f05d9f4b`.

- prompt: evening relaxation and reflection on the day;
- 145 spoken words;
- 14 short speech segments;
- 14 explicit pauses;
- 63.3 seconds of speech;
- 65.5 seconds of silence;
- 128.8 seconds total;
- clipping: zero samples;
- all deterministic audio quality checks: passed.

This is approximately half speech and half intentional space. The script also
contains actual day-reflection content instead of spending the whole duration
on generic breathing instructions.

## Fish Speech Trial

Fish Speech was tested rather than selected from marketing claims.

The current Fish Audio S2 documentation recommends at least 24 GB of GPU VRAM
and lists Linux/WSL as the supported system:
[official installation requirements](https://speech.fish.audio/install/).
Its license permits free research and non-commercial use, but commercial use
requires a separate license:
[S2 Pro license](https://huggingface.co/fishaudio/s2-pro/blob/main/LICENSE.md).
That makes it unsuitable as Whoopy's supported default today.

For a real Mac experiment, the older Fish Speech 1.4 release was installed in
an isolated environment. Its checkpoint is licensed
[CC BY-NC-SA 4.0](https://huggingface.co/fishaudio/fish-speech-1.4), so it is
also experimental and non-commercial.

The complete offline experiment is intentionally ignored by Git:

```text
models/experimental/fish-speech-1.4-runtime/
```

It occupies about 2.5 GB, including a 1.1 GB checkpoint and the isolated Python
environment. It was verified on Apple Metal after being moved to persistent
storage.

The same 33-word passage produced:

| Voice path | Duration | Articulation |
| --- | ---: | ---: |
| Fish 1.4, random voice | 9.94 s | 199 WPM |
| Fish 1.4, slow reference | 14.86 s | 133 WPM |
| Kokoro, speed 0.6 | 16.29 s | 122 WPM |

Fish was not automatically slower. It became much calmer only after receiving
a slow reference recording. That reference was generated by Kokoro, not a
consented human voice, so this experiment evaluates pacing mechanics rather
than final voice quality.

Comparison WAV files are kept locally under:

```text
runs/.experiments/fish-speech/
```

The result is a deliberate decision:

- keep Kokoro at `0.6` as the supported portable default;
- keep the adapter boundary so Fish or another TTS can be added later;
- do not ship a non-commercial Fish checkpoint as a default dependency;
- evaluate Fish again when there is a suitable licensed model, a consented
  reference voice, and a cross-platform runtime that fits ordinary laptops.

## How To Try It

Start the same private web tester:

```bash
uv run --offline whoopy web --open
```

Choose **Meditative** pace, enter a normal prompt, and generate a new run.
Existing WAV files are immutable, so an old fast run will remain fast. New
runs use prompt version 2, the sentence-level pacing rules, and the slower
default.
