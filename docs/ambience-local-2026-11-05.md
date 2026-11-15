# Optional local ambience auditions — November 5, 2026

The standalone utility makes **original procedural synthetic noise**, not natural
rain/ocean field recordings and not AI-generated audio. `rain` is soft filtered
pink noise; `ocean` applies slow swells to a darker filtered noise. Neither has
been rated for naturalness, comfort, or meditation quality by a listener.

This is an optional offline asset/mix tool. It is not wired into the application,
generation workers, timelines, narration caching, ASR, or production exports.
It requires an existing `ffmpeg` and `ffprobe` on PATH and standard-library Python.
It installs nothing, uses no model/API/account, and performs no network requests.

## Create and compare auditions

From the checkout root, use fresh output directories whose parent already exists:

```bash
python3 scripts/prepare_ambience.py generate --kind rain --seconds 60 --seed 17 --output /tmp/whoopy-rain-a
python3 scripts/prepare_ambience.py generate --kind ocean --seconds 60 --seed 17 --output /tmp/whoopy-ocean-a
python3 scripts/prepare_ambience.py mix --narration /absolute/path/narration.wav --bed /tmp/whoopy-rain-a/bed.wav --output /tmp/whoopy-mix-a
```

`generate` writes `bed.wav`; `mix` writes `mix.wav` and its prepared `loop.wav`.
Each also writes `manifest.json`. Existing output paths are rejected, even empty
directories. Input paths must identify local PCM WAVs, mono/stereo, 16/24/32 bit.
Other formats, URLs, playlists and model identifiers are not accepted. Generated
assets have gentle beginning/end fades for auditioning. Loop preparation takes a
stable body plus a linear tail/head crossfade; it does not establish a good loop
or remove a recording's internal fades, identifiable events or repeating rhythm.

The mixer accepts bed lengths of 0.5–600 seconds and narration up to one hour.
Generated durations are 0.25–3600 seconds; very short outputs are test fixtures.
Bed preparation uses a bounded in-memory 48k stereo PCM loop. Ten minutes is
approximately 115MB of source PCM, plus Python working buffers; ordinary shorter
assets are preferable. Long generated beds must be trimmed separately before mixing.

## Levels, timing and preservation

The default mixer applies **-3dB to narration and -18dB to the source bed**, with
no automatic ducking, normalization or limiter. Override with
`--narration-gain-db` / `--bed-gain-db` from -60 to 0. These are relative gains,
not a measured -37 LUFS promise. Generated assets may need a higher bed gain to
be audible under a particular narration; audition conservatively. Constant bed
level avoids speech-boundary pumping. The output fails rather than silently
publishing detected PCM clipping. Source clipping or distracting transients
still require review, and this check does not measure intersample true peaks.

Canonical narration is never rewritten. Narration is resampled once into a
separate **48kHz stereo 16-bit PCM** mix. Its length equals the nearest 48k sample
to the source WAV duration (at most half a target sample rounding), enforced by
padding/trimming after resampling. Timeline/ASR evidence continues to refer to
the original narration, not the derived mix. Ambience intentionally continues
through narration pauses, so the original exact-silence checks do not apply to
the mixed file.

Manifests record input hashes, frame/rate metadata, generated seed/channel seeds,
filter/loop recipes, gains, FFmpeg version, output hashes, frame count, duration,
peak, sample clipping and successful full decoding. Mixes independently rehash
both inputs before publishing. Hash reproducibility is supported only with the
same recipe, inputs and FFmpeg build; no cross-platform identity is promised.
Failed verification leaves no completed output directory.

Before accepting an asset: listen to several loop boundaries and at least ten
minutes under representative narration; check headphones and mono, voice clarity,
repeated wave timing, hiss, abrupt stereo movement and silence/fade dips. Passing
decode/PCM checks establishes file integrity, not naturalness or listening quality.

## Optional model research, not installed or tested here

The first future local generation trial remains **Stable Audio 3 Small-SFX**:
the official repository documents 44.1k stereo, a 120-second maximum, and an
Apple Silicon MLX runtime without PyTorch/Transformers. Its runtime page contains
vendor speed/memory reports; these are not measurements from this utility.
The official LiteRT/TFLite CPU path documents macOS/Linux/Windows support and
selective component downloads. No model/runtime download, authentication, license
acceptance, inference, or listening comparison was performed for this change.

- [Official model family, supported lengths and format](https://github.com/Stability-AI/stable-audio-3)
- [Small-SFX model card and access gate](https://huggingface.co/stabilityai/stable-audio-3-small-sfx)
- [Official MLX runtime](https://github.com/Stability-AI/stable-audio-3/blob/main/optimized/mlx/README.md)
- [Official portable CPU runtime](https://github.com/Stability-AI/stable-audio-3/blob/main/optimized/tflite/README.md)

SA3 application code is MIT; checkpoints have separate Stability Community terms
and T5Gemma brings Gemma terms. The agreement linked by the model gate requires
commercial registration, includes an aggregate annual-revenue threshold of $1M,
and has distribution/notice/attribution obligations. The vendor separately lists
API providers under Enterprise. Generated-output ownership does not guarantee
copyright, exclusivity or non-infringement. Reconfirm the accepted terms for the
actual intended deployment before choosing that optional model.

- [Code license](https://github.com/Stability-AI/stable-audio-3/blob/main/LICENSE)
- [Agreement linked by the Small-SFX gate](https://huggingface.co/stabilityai/stable-audio-open-1.0/blob/main/LICENSE.md)
- [Vendor licensing](https://stability.ai/license)
- [Gemma terms](https://ai.google.dev/gemma/terms)

For the dependable first natural-sound library, curate long recordings made by
the owner or acquired under per-file CC0/CC BY terms. Retain the source, creator,
license/version, original hash, edits and required credits. CC BY needs credit,
a license link and modification disclosure. Freesound API access has separate
commercial terms from reuse of an acquired sound. TangoFlux and Sony Woosh
checkpoints have non-commercial restrictions and are not commercial defaults.

- [CC0 terms](https://creativecommons.org/publicdomain/zero/1.0/)
- [CC BY terms](https://creativecommons.org/licenses/by/4.0/)
- [Freesound asset FAQ](https://freesound.org/help/faq/#licenses)
- [Freesound API terms](https://freesound.org/help/tos_api/)
- [TangoFlux card](https://huggingface.co/declare-lab/TangoFlux)
- [Sony Woosh licenses](https://github.com/SonyResearch/Woosh/blob/main/README.md)

These sources were checked live during research on November 5. AI generation
would be optional asset authoring with human selection, not a prerequisite for
session playback. Slower systems can play the same accepted WAVs or use the
procedural utility without any model memory or download.
