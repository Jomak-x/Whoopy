# Independent audio correctness evidence

This directory is an independent check of rendered words. It does not rank voice
warmth, teaching quality, naturalness, or personal listening preference.

Run from the repository root:

```sh
models/experimental/verification-2026-10-28/.venv/bin/python \
  scripts/verify_benchmark_audio.py path/to/mono.wav \
  --expected-file evaluations/benchmarks/2026-10-28/voice-script.txt \
  --report evaluations/benchmarks/2026-10-28/verification/candidate.json
```

Use `--mode narration` for assembled speech containing deliberate meditation
silences. This suppresses the speech-only excessive-silence review flag. It never
suppresses missing-word/content checks. Retain raw native outputs. Stereo is inspected per channel; the recognizer uses
its normal mono decode internally, without replacing the native evidence file.

The isolated runtime is in `models/experimental/verification-2026-10-28/.venv`.
Its versions are frozen in `runtime-requirements.txt`; core Whoopy dependencies
were not changed. Whisper uses the already cached `whisper-small-mlx` snapshot,
revision `45f3915923c7a79a5a5b5a7d909d39aeb0e5630e`. The harness accepts an existing
local snapshot only, enables Hugging Face offline flags, supplies no expected
words or contextual prompt to the recognizer, and obtains an exclusive
`/tmp/whoopy-benchmark-gpu.lock` before loading/inferencing. Metal access may
require execution outside the filesystem sandbox.

Each report contains audio, expected-text, model-weight, model-config and verifier
source SHA256 digests; runtime versions; full ASR transcript, segment/word
timestamps and recognizer confidence fields; ordered edit alignment; coverage,
word-error rate, omissions, excess repetitions and additions; possible missing
ending; and supplementary PCM finite/silence/clipping/noise diagnostics.

Exit status is 0 for **pass**, 2 for **review**, and 1 for **fail**. Missing ASR,
model import/inference errors, timeout, and uninspectable PCM fail closed. A pass
requires zero normalized word insertions, deletions, or substitutions and no PCM
failure/review issue. Every detected word edit requires **review**, even when
global WER is low. Existing hard failures remain: a missing ending of at least
three words, a contiguous span of eight word-level ASR errors, WER > 0.35,
coverage < 0.65, and existing PCM/recognizer failures. A mismatch alone is not
automatic rejection; a hard-fail condition still takes precedence.
These are screening
thresholds, not claims of error-free human transcription. Inspect alignment and
listen before attributing differences to the synthesizer: ASR can itself omit or
hallucinate words. Unexpected recognized words can reflect TTS errors or ASR errors.

## Actual ASR negative controls

`negative-controls-provenance.json` specifies the deterministic controls. These
are actual independent model runs, separate from the behavioral unit tests.

| Input | ASR observation | Result |
| --- | --- | --- |
| Eight seconds of seeded white noise, audible RMS about 0.115 | No recognized words; coverage 0/5, WER 1.0 | Fail |
| Eight seconds of exact silence | Recognizer hallucinated `you`; coverage 0/5, WER 1.0 | Fail |
| Earlier Qwen 0.6B smoke, 16.24 seconds | No recognized words; coverage 0/5, WER 1.0 | Fail |

The prior Qwen report retains the exact source path and SHA256 of the inspected
historical WAV. That old source may have been moved into the local recovery archive
during cleanup. Its report remains historical evidence; a successful render/smoke
receipt was insufficient to establish correct speech. ASR disagreement does not,
by itself, prove that the audio is static noise.

Noise was rejected on missing independently recognized expected words. Duration
and RMS do not establish speech. Exact silence also illustrates why even a
nonempty ASR transcript is insufficient without PCM and content agreement.

Behavioral tests in `tests/test_audio_verification.py` use explicit mocked
recognizer transcripts, generated sample arrays and WAVs; CI does not load live
models. They cover unavailable ASR, unrelated/ordered/repeated/missing words,
truncated endings, clipping, silence, nonfinite samples, intentional narration
silence, and the CLI's structured failure report.

## Fresh full-script evidence

The fixed script contains 123 normalized words. Kokoro Michael, slow George
(native speed 0.8), slow Heart (native speed 0.8), and Chatterbox v3 each passed full-file ASR
with all 123 words recognized in order and WER 0.
MLX Qwen VoiceDesign also passed 123/123 with WER 0. This is distinct from the
native Qwen Ryan content problem below.
Final MLX CustomVoice Ryan also passed the full 123/123 with WER 0; its 8-bit MLX
runtime and revised token budget are separately identified in generation evidence.

The fresh production **Sherpa Kokoro at native speed 0.6** baseline passed under
the earlier tolerance in narration mode with all 123 expected words recognized and WER 0.00813: the recognizer added
one initial `Then`. Its model is `Kokoro-multilang@v1_0-release-2026-10-26`,
Sherpa ONNX 1.13.4, speaker 16. It is not an exact-match claim; the production
runtime and speed differ from the MLX controls. The derived strict policy marks
this one-word insertion **Review**; its matched copy remains available.

Qwen Ryan's full-file report is **review** (WER 26.0%, coverage 74.0%). Independent
ASR of its second native paragraph failed with 0/32 matched words and WER 1.0,
corroborating a localized content problem. Raw/problem auditions remain available
for review; no matched good-candidate copy was created for it.
The separately retained `qwen-custom-ryan-policy-v2.json` reassesses the same
hash-bound ASR evidence under the contiguous-block rule and **fails** on a
23-operation disagreement span. The original full-file review report remains.

Vox illustrates an actual recognizer limitation. Its original full-file ASR
omitted the final 28-word paragraph. Independent ASR of that paragraph recognized
every word; all four paragraphs then independently matched exactly (123/123
combined). `build_segment_aggregate.py` verified that the raw decoded FLOAT32
samples are exactly the ordered native-paragraph concatenation: 2,042,912 frames,
48 kHz mono, no added frames. It bound generation/script/paragraph/audio/report
hashes and wrote `voxcpm2-designed-male-aggregate.json`. The original full-file
**fail** report is preserved. The aggregate qualifies the assembled words via
complete paragraph evidence plus exact PCM assembly; it does not claim that the
full-file recognizer succeeded, or that joins and voice quality are preferred.

`listening/manifest.json` records constant-gain audition copies for qualified raw
files. These target -23 LUFS, constrained to at most -1 dB true peak; only one
volume gain and PCM16 listening encoding are applied. Originals are untouched.
No denoising, EQ, time stretch, or noise-candidate amplification is performed.
Use `create_listening_copies.py --candidate REPORT_STEM` to add one newly qualified
candidate while preserving prior approved copies. Helper-source hashes have
explicit names; `expected_text_sha256` identifies the spoken input text.

## Current policy reassessment

[policy-v3](policy-v3/) contains 12 derived JSON receipts evaluated from the
existing recognizer output, not new ASR or acoustic inspection. The owner prepared
these sidecars without replacing old receipts or media. The current listening
selection is **8 Pass / 4 Review**: eight voice Pass results, one voice Review,
and three writing Reviews. Pass means zero normalized word edits in the retained
evidence and the recorded PCM qualification; it does not establish word-perfect
audio or listener preference. Vox uses its complete paragraph plus exact-assembly
chain; its original full-file failure remains. Native MPS Ryan's failure and
Dots' incomplete attempt remain separate.

The source builder reads [listening/manifest-policy-v3.json](listening/manifest-policy-v3.json)
and [writing-listening-policy-v3.json](../writing-listening-policy-v3.json),
including Review samples with explicit labels and original raw/proof links.
These manifests preserve the original manifest hashes, classifications, media
hashes and timestamps. They were copied as metadata only; no audio or old
verification receipt was rewritten. Current source changes postdate the measured
benchmark runs and do not retroactively certify them.
