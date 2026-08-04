# Phase 3.6: Voice And Meditation Lab

This document explains the changes made after the first real listening review.
The review found four concrete problems:

1. harmless natural-breath language could fail generation;
2. every sentence had the same stop-start rhythm;
3. independently normalized speech blocks exposed a synthetic onset;
4. the prose often sounded like generic relaxation filler instead of a guided
   contemplative technique.

It also asked Whoopy to compare Kokoro, Fish Speech, and the larger MOSS-TTS
v1.5 checkpoints from one local interface.

## What “Open Source” And “Non-Commercial” Mean

Source availability and permission are different questions.

Fish Speech 1.4 publishes code and weights, but its checkpoint is
[CC BY-NC-SA 4.0](https://huggingface.co/fishaudio/fish-speech-1.4). That
license permits sharing and adaptation for uses that are not primarily
intended for commercial advantage or monetary compensation. Attribution and
ShareAlike obligations still apply. A personal, local, non-commercial Whoopy
experiment fits that purpose. Charging for the app, putting it behind a
subscription, using it in paid client work, or monetizing it with ads requires
a separate license review. This is a technical project explanation, not legal
advice.

Kokoro and the official MOSS-TTS v1.5 checkpoints used here are Apache-2.0,
which permits commercial use subject to its notice and license requirements.
The web studio displays the selected model and its license so these boundaries
do not disappear behind one “voice” menu.

## Why The Breathing Section Failed

The rejected drafts said things such as:

> Let each inhale feel natural.

Whoopy's narrow safety checker had accidentally classified every phrase
beginning with “let each inhale” as prescribed breath control. That was too
broad. Observing an inhale is not the same as requiring a deep breath, timed
exhale, or breath hold.

The broad rule was removed. The focused rules still reject:

- breath holding;
- required deep breaths;
- filling the lungs; and
- commands to inhale or exhale slowly, deeply, or for a fixed interval.

A regression test now proves that natural observation is accepted while the
actual breath-control cases remain rejected.

## Technique-First Meditation Writing

Whoopy does not copy a proprietary app's scripts or voice. It uses established
guided-meditation structures described in public educational material.

- Headspace describes techniques including focused attention, body scan,
  noting, visualization, reflection, loving kindness, and resting awareness:
  [Headspace techniques](https://help.headspace.com/hc/en-us/articles/115011850767-What-are-the-techniques).
- Headspace's breathing guidance says to let breathing remain natural and
  observe its rising and falling:
  [Headspace natural breathing](https://help.headspace.com/hc/en-us/articles/215058708-Is-There-a-Particular-Way-I-Should-Breathe).
- Its body-scan description moves attention through the body while observing
  sensations without trying to change them:
  [Headspace body scan](https://www.headspace.com/content/expert-guidance/body-scan/17).

Every generated plan now names one primary technique per section. Sleep
requests receive a dedicated sleep-transition ending instead of being
re-alerted by the ordinary return-to-the-room sequence. This is not only a
prompt suggestion: deterministic validation rejects a sleep plan ending in
`return` and asks the model to repair it. Ordinary practices must end in
`return`, and every practice must begin with `arrival`. Python also adds
reviewed, technique-specific drafting guidance. For example, a body scan must
move through concrete body areas, while reflection must ask one meaningful
question and leave the answer to the listener. The prompt explicitly rejects
empty phrases such as “embrace the calm” and repeated reassurance.

The model still writes the prose, but it writes inside a clearer practice
structure. Whoopy continues to validate and budget the result before it can
reach audio.

## Adaptive Pacing

One fixed pause after every sentence sounds mechanical. Whoopy now assigns
silence by function:

| Just spoken | Default silence |
| --- | ---: |
| short continuation | 1.0 seconds |
| ordinary thought | 1.6 seconds |
| embodied observation | 2.4 seconds |
| natural-breath observation | 3.6 seconds |
| reflection or visualization question | 4.8 seconds |
| explicit “stay here” practice | 6.0 seconds |
| end-of-section practice | 6–20 seconds |

Two related short sentences may remain in one speech segment. This reduces
model restarts and lets punctuation create a natural micro-pause. Questions,
breath observations, and explicit practice invitations keep their own longer
silence. The decisions remain deterministic and visible in `script.md` and
`timeline.json`.

## Smoother Speech Boundaries

Previously, every small segment was amplified independently to the same peak.
That could magnify low-level synthesis noise and make the beginning of each
block sound metallic. The processor now:

- keeps 60 ms of edge context instead of 25 ms;
- uses a 40 ms boundary fade instead of 10 ms;
- limits upward gain to 3 dB instead of aggressively amplifying every quiet
  segment; and
- groups some related short sentences, reducing the total number of model
  restarts.

All values are part of the speech cache identity. An old cached segment cannot
silently bypass the new processing contract.

## Fish Speech

### Fish 1.4

Fish 1.4 is installed locally as an optional Apple Metal experiment. Whoopy
keeps one persistent worker alive for a render, conditions it on a slow local
reference recording, converts its native 44.1 kHz result to Whoopy's canonical
24 kHz mono PCM, then sends it through the ordinary cache, retry, processing,
assembly, and quality pipeline.

Fish 1.4 does **not** document the square-bracket expression syntax requested
for newer Fish models. Whoopy does not send fake unsupported controls to it.
Its expression comes from the reference recording.

### Fish S1-mini And S2

The current [Fish S2 documentation](https://github.com/fishaudio/fish-speech)
supports bracket tags such as `[whisper]`, `[excited]`, and `[angry]`, including
free-form tags. Its
[installation requirements](https://speech.fish.audio/install/) target
Linux/WSL and recommend 24 GB of GPU memory. Its research license is also
non-commercial unless a separate commercial license is obtained.

Fish S1-mini supports parenthetical delivery markers such as `(relaxed)`,
`(comforting)`, and `(soft tone)`, not S2's square brackets. The checkpoint is
gated and requires a Hugging Face account to accept its terms. Neither model is
silently substituted for Fish 1.4. The studio labels S2 as unavailable on this
Mac workflow.

## MOSS-TTS v1.5

The official
[MOSS-TTS Local Transformer v1.5 model card](https://huggingface.co/OpenMOSS-Team/MOSS-TTS-Local-Transformer-v1.5)
documents:

- a 5B Local Transformer;
- Apache-2.0 licensing;
- 31 languages with explicit language tags;
- zero-shot voice cloning;
- free-form delivery instructions;
- token-level duration control;
- pronunciation control through Pinyin or IPA;
- `[pause X.Ys]` inline pause markup; and
- native 48 kHz stereo output using 12 RVQ codebooks.

The [MOSS-TTS collection](https://huggingface.co/collections/OpenMOSS-Team/moss-tts)
also provides an 8B flagship v1.5. These are the two larger current
single-narrator v1.5 models relevant to Whoopy, so both have adapter and UI
entries:

| Studio choice | Architecture | Published download | Local status |
| --- | --- | ---: | --- |
| MOSS Local v1.5 | local depth transformer | 5B / 9.1 GB | complete and synthesis-tested |
| MOSS flagship v1.5 | delay-pattern transformer | 8B / 17.0 GB | 4.4 GB partial; disabled |

Both speech checkpoints use the separate MOSS Audio Tokenizer v2. It occupies
about 7.9 GB locally, so the working 5B setup uses about 16.4 GB of allocated
disk space in total. Finder or `du` can differ from a model card's decimal
download size because filesystems report allocated binary units.

The 8B download could not be completed before the current network-approval
quota closed. Whoopy parses its safetensors index and checks every referenced
shard, so that partial directory cannot be mistaken for a usable model. Its UI
entry stays visible for comparison but disabled until all shards are present.
The adapter uses the same official v1.5 processor interface, but this PR does
not claim an 8B synthesis result that was not run.

MOSS-TTSD is a multi-speaker dialogue model, MOSS-SoundEffect is not speech
narration, and MOSS-TTS-Realtime/Nano are smaller latency-focused models. They
are not mislabeled as larger meditation voices.

Both selected models receive their own:

- language tag;
- reference-voice or direct-voice choice;
- free-form delivery instruction;
- deterministic seed and cache identity;
- 48 kHz stereo-to-24 kHz mono conversion; and
- persisted model/license metadata.

The official runtime documents CUDA and CPU. Whoopy prefers Apple Metal on a
capable Mac and otherwise retains a much slower CPU fallback. Metal remains an
experimental Whoopy path, so a model appears “ready” only after its complete
checkpoint, runtime, audio tokenizer, and reference are present. A real voice
test is still the final compatibility check for a particular laptop.

## Web Studio

Start the local studio with:

```bash
uv run --offline whoopy web --open
```

Then choose a speech model before generating, or use **Test this voice** for a
short two-sentence comparison. MOSS Local 5B is ready on the development
MacBook. Its first test must load roughly 16 GB of model and codec data, so it
can appear quiet for a while even though later speech segments reuse the same
persistent worker. The 8B option remains disabled while its download is
incomplete.

The speech-model menu now distinguishes:

- Kokoro, the portable default;
- Fish 1.4, the non-commercial reference-conditioned experiment;
- MOSS Local Transformer v1.5;
- MOSS flagship v1.5; and
- Fish S2, shown but disabled with its hardware limitation.

When a MOSS model is selected, the page reveals its language, voice-source, and
delivery-instruction controls. “Test this voice” renders a short sample through
the same production pipeline. Recent run cards show which speech model made the
audio, and `resolved-config.json` plus `model-metadata.json` preserve the exact
settings and license.

Useful MOSS instruction experiments include “Speak softly with a warm,
low-energy bedtime delivery” and “Use a grounded, spacious delivery with
gentle sentence endings.” These are model-native free-form instructions. Fish
1.4 instead derives expression from its reference voice; the newer Fish S2
bracket tags are deliberately not presented as if they worked in 1.4.

Large runtimes and checkpoints stay under `models/experimental/`, which Git
ignores. The PR contains adapters, tests, documentation, and controls—not
multi-gigabyte weights.
