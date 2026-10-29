# Whoopy ambient-bed research — 2026-10-29

Research only: no model downloads, inference, dependencies installed, or checkout edits. Runtime numbers below are publisher reports, not measurements on Jakob's machine. Sources were read live and restricted to official model cards/repos/vendor documents.

## Recommendation

For a meditation product, retain a dependable procedural rain/wind bed and curate a few long, clean stereo ocean/rain field recordings. Treat local generation as an optional asset-authoring step, audition its outputs, then ship/cache accepted WAVs. Do not make a quiet 20-minute session depend on inference. This is an engineering recommendation, not a measured comparison of audio quality.

The first local model worth testing now is **Stable Audio 3 Small-SFX through the official MLX runtime**. Stable Audio Open 1.0 is no longer the best starting point: SA3 adds longer clips and purpose-built Apple paths. Medium is the second candidate only if Small-SFX's listening quality is insufficient. No evidence reviewed establishes that either reliably produces uninterrupted rain or seamless long-session loops.

## Narrow shortlist

| Option | Published format / length | Mac feasibility and limits | Product fit |
|---|---|---|---|
| Original procedural DSP | Choose 44.1/48 kHz stereo; arbitrary duration | CPU, negligible model memory; no downloads | Predictable, indefinitely varying rain/wind; ocean needs shaped swells and detail to avoid sounding like modulated hiss. Own code avoids model-weight terms. |
| Curated field recordings | Source-specific stereo WAV/FLAC, preferably minutes long | Ordinary playback; stream from disk | Strong natural detail. Require provenance, per-file license, and audition for speech, boats, birds, clipping, mic wind and repeat cues. |
| SA3 Small-SFX | 44.1 kHz stereo, documented maximum 120s; 433M DiT (HF counts ~0.6B total) | Official pure MLX/Metal; CPU LiteRT/TFLite and CoreML routes also exist | First optional generative trial. Explicit SFX model; natural sounds still require listening QA and loop preparation. |
| SA3 Medium | 44.1 kHz stereo, documented maximum 380s; 1.4B DiT | Main Python path emphasizes CUDA; official MLX supports Medium on Apple Silicon | Longer candidate assets, more runtime/memory. Need comparative listening to justify extra setup. |
| SA Open 1.0 | 44.1 kHz stereo, up to 47s | Official stable-audio-tools / Diffusers; sampled examples choose CUDA or CPU | Established older baseline, short repeating beds; no reviewed official Mac benchmark. |

SA3 formats/limits/features: [official repository](https://github.com/Stability-AI/stable-audio-3). SA Open details: [official card](https://huggingface.co/stabilityai/stable-audio-open-1.0). SA3 component and text-encoder details: [Small-SFX card](https://huggingface.co/stabilityai/stable-audio-3-small-sfx).

### Apple runtime evidence and caveats

[Official MLX documentation](https://github.com/Stability-AI/stable-audio-3/blob/main/optimized/mlx/README.md) says pure MLX inference uses no PyTorch/transformers at runtime, supports Small-SFX and Medium, and defaults to freeing stages between conditioning, sampling and decoding. Its M4 Pro/48GB sample benchmark reports Small-Music (not SFX) 30s in 1.53s at 1.94GB peak, 120s in 4.12s at 2.38GB; Medium 120s in 14.68s at 5.21GB. These are upstream claims, not independent results. Small-Music's 380s benchmark exceeds Small's documented 120s limit: use 120s as supported horizon, not the benchmark as a quality promise.

The MLX CLI supports a fixed seed, negative prompt, guidance, audio-to-audio and inpainting. Some flags may change with commits. Its Gradio launcher enables a public share link by default: use CLI, or explicitly local-only UI. [Official CPU runtime](https://github.com/Stability-AI/stable-audio-3/blob/main/optimized/tflite/README.md) offers another Mac fallback. Prefer MLX over spending time debugging MPS operator support; MPS is a separate PyTorch backend and wasn't verified here. The root README documents a Medium static-glitch failure associated with flash-attention on its CUDA path; this is not evidence of the same failure in MLX.

## License separation

### SA3 application code

[Code LICENSE](https://github.com/Stability-AI/stable-audio-3/blob/main/LICENSE): **MIT License**. Preserve notices. This grants no blanket rights to checkpoints, text encoder or transitive dependencies. A distributable runtime still needs an inventory of its resolved dependencies; none was installed/audited here.

### SA3 checkpoints and commercial use

The agreement linked by the [SA3 Small-SFX access gate](https://huggingface.co/stabilityai/stable-audio-3-small-sfx) currently points to the [public July 5, 2024 agreement in the Stable Audio Open 1.0 repository](https://huggingface.co/stabilityai/stable-audio-open-1.0/blob/main/LICENSE.md). This is the verified contract source; a standalone SA3 repository LICENSE file was not independently verified. It permits free limited commercial use subject to terms; commercial registration is required; exceeding $1M aggregate annual revenue across licensee/affiliates terminates this grant and requires a separate license. Distribution/product use carries agreement, notice and attribution duties. Exact short quotes: **“You must register with Stability AI”**; **“Powered by Stability AI”**. These are open weights under restrictions, not unrestricted MIT weights. Keep weights outside Whoopy's own open-source code license. Confirm the exact accepted agreement and deployment terms before distribution.

That agreement explicitly includes hosted services/APIs in commercial purposes. However, the [current licensing page](https://stability.ai/license) lists API providers under Enterprise. Do not promise that a public paid generation API qualifies automatically under the small-business grant; confirm that deployment separately. A local authoring tool or ordinary player with pre-generated assets is a different architecture.

The agreement's definition of model derivatives excludes outputs. Stability's [FAQ](https://stability.ai/license) says users own generated outputs as between user and Stability, with law/AUP obligations. This is not a warranty of copyright, exclusivity or non-infringement. Output WAVs and distributing an inference runtime/weights require separate decisions.

### SA3 text encoder / dependencies

The [model card](https://huggingface.co/stabilityai/stable-audio-3-small-sfx) identifies T5Gemma-b-b-ul2 and requires agreeing to Gemma terms alongside Stability terms. [Gemma agreement](https://ai.google.dev/gemma/terms) permits distribution subject to passed-through restrictions, agreement/notice and modified-file requirements; hosted services also count as distribution. Exact quote: **“Google claims no rights in Outputs you generate using Gemma.”** Do not omit Gemma notices when bundling a working stack.

### Models to exclude from a paid-product shortlist

* [AudioGen Medium card](https://huggingface.co/facebook/audiogen-medium): 1.5B, 16kHz EnCodec, CC-BY-NC-4.0 weights. AudioCraft code licensing does not erase the NC checkpoint restriction.
* [TangoFlux card](https://huggingface.co/declare-lab/TangoFlux): exact quote **“The TangoFlux checkpoints are for non-commercial research use only.”** Multiple inherited licenses also apply. Not a shortcut to commercially usable Stable Audio.
* [MMAudio checkpoint card](https://huggingface.co/hkchengrex/MMAudio): CC-BY-NC-4.0 checkpoints, despite [MIT source code](https://github.com/hkchengrex/MMAudio). Video synchronization isn't needed for these beds.
* [AudioLDM2 official repo](https://huggingface.co/cvssp/audioldm2/tree/main): CC-BY-NC-SA-4.0. Do not choose this for a future paid app merely because local inference is possible.

No claim is made that every NC license automatically covers all outputs; the reason to exclude these is that their checkpoint use itself is not a suitable commercial inference foundation without another grant.

### Field-recording rights and API rights

[Freesound FAQ](https://freesound.org/help/faq/#licenses): per-file CC0 allows broad reuse including sale; CC-BY needs attribution; CC-BY-NC does not suit paid use. Prefer CC0 or own recordings, with the original URL, uploader, license/version, acquisition date, file hash, modifications and credit text recorded. User uploads can be mislicensed; audition and provenance review remain useful. No specific asset was selected or downloaded.

[Freesound developer page](https://freesound.org/help/developers/) distinguishes sound reuse from access to its API. Exact quote: **“Freesound API usage is free for non-commercial use”**. [API terms](https://freesound.org/help/tos_api/) say commercial access terms are negotiated case by case. A CC0 WAV does not make a live commercial Freesound API integration automatically free. Locally bundled, properly acquired CC0 audio avoids that runtime API dependency.

## Continuity and comfort: implementation judgment

Native stereo and longer generation do not imply seamless loops, stable level, or independence of the two channels. Prompting “seamless” cannot establish endpoint continuity. Ocean waves reveal repeating wave timing; a transient or spectral change across a crossfade can draw attention even without a click. A 120s file repeated ten times is still a two-minute pattern.

Use offline selection and conservative level matching. Reject generated birds, speech/music hallucinations, abrupt gusts, piercing hiss, pumping, clipping, fade-to-silence endings and sudden stereo movement. Build a loop from a stable middle region; test a several-second equal-power crossfade and tune by ear. Multiple quiet, decorrelated long layers can reduce repetition, but should not turn a simple player into a live generation system. Keep noise filter state continuous in procedural playback. Listen in mono as well as headphones; anti-phase width can collapse. Voice intelligibility and uninterrupted playback matter more than prompt fidelity.

## Next smallest test (future, not executed)

After core voice work passes: use an isolated /tmp or dedicated tools environment, pinned official SA3 commit and Small-SFX MLX bundle; accept required licenses through the normal authorized account flow, never extract credentials. Download only the selected model path, no paid API. Generate three 60s seeds each for steady soft rain and distant gentle surf, without thunder/voices/music. Save prompt, seed, exact versions, elapsed time and peak memory. Compare blind against procedural/recorded baseline, repeat each accepted candidate with a crossfade for at least 10 minutes underneath representative narration, and check seam, peak/headroom, spectrum, mono and stereo comfort. Drop generation if it adds distracting artifacts. Do not infer sample quality or Mac success from the published runtime table.

Ambient research worker's historical Neon attempt: `neon_worker.py models` from canonical Whoopy failed with gateway DNS error (`nodename nor servname provided`). This worker selected no Neon model and executed no ambient review request; no model-generated ambient secondary review is claimed. Root subsequently reported successful escalated network discovery and a separate bounded Opus 4.8 adapter review. That later work is outside this research report; the earlier DNS failure does not describe current gateway availability.
