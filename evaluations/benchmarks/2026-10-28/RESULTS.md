# Local writing and voice comparison — 28 October 2026

This is evidence from one M4 Pro with 48 GiB unified memory. It establishes local
execution and bounded content checks, with human preference still pending. The
fresh benchmark is retained alongside failed attempts; older generated material
is historical. It does not establish Headspace equivalence or a listener-approved
default.

## Writing comparison

The final daytime campaign is [writing-v5-high/report.json](writing-v5-high/report.json).
Both methods use the pinned Ministral 3 14B Q4_K_M model, llama.cpp b10142, seed 42,
32,768 context tokens, temperature 0.7 and top-p 0.9. The request is identical
within each pair. The daytime legacy `plan_first` controls were **reused**, not rerun:
the v5 report names the prior source and hashes because their model, request,
settings and legacy compiler were unchanged. Elapsed times below are descriptive
receipts, not a repeated speed experiment. Attempt counts describe the saved
pipeline attempts, not comparable numbers of whole-session calls. The sleep
control is a fresh retry after the preserved resource refusal.

| Request | Legacy control | New `recipe_whole_session` | Text-duration estimate |
| --- | --- | --- | --- |
| Seated grounding after work, 3 min | 217 words, 3 sections, 5 saved attempts, 215.3 s | 159 words, 6 sections, 1 attempt, 71.1 s | Both about 180 s |
| Sound anchor before a meeting; no breath focus, 3 min | 181 words, 3 sections, 4 saved attempts, 98.6 s | 156 words, 6 sections, 1 attempt, 82.6 s | Both about 180 s |
| Lying-down good-night reflection, 5 min | Retry: 296 words, 4 sections, 5 saved attempts, 115.2 s | Failed after one holistic revision, 157.7 s | Legacy about 300 s; no accepted new script |

The actual saved prose supports a narrower conclusion than automatic acceptance:

- Grounding: the new script keeps contact as support, introduces one sound,
  rehearses returning to it, gives room to practice with less guidance, and ends
  with movement and return to the day. The legacy script briefly introduces
  nose-breath attention before switching to sound, and its last section stops at
  being present after the day rather than explicitly resuming it.
- No-breath focus: both saved scripts use sound without directing breath focus.
  The new script explicitly rehearses mind-wandering and return, then hands the
  practice back to the listener and ends with continuing the day. The legacy
  script also has a recognizable sound practice, but finishes by returning to
  the room without the requested explicit transition into the day.
- The completed legacy sleep retry has a sleep taper, but repeats eyes-closing
  and begins a reflection paragraph with an unexplained reference to a memory
  before asking the listener to recall an ordinary moment. The new sleep retry
  failed after its holistic revision: it required 213.7 s quiet against a bounded
  capacity of 10–134 s, and the reflection paragraph failed the complete-sentence
  check. There is no accepted new sleep script or new sleep audio. This failure
  limits the claim of general sleep support; source was not changed to force a pass.
- The new examples remain repetitive, and both share the title “A Steady Moment.”
  The grounding ending says to open the eyes without having asked to close them.
  Contact is secondary to sound. These are real editorial limitations to review,
  even though the structural contracts pass. Two daytime examples cannot prove
  broad personalization, teaching quality or long-session quality.

The typed timeline bounds individual new quiet events and preserves accepted
sentences. Estimated duration includes planned silence; final speech duration
still requires the actual voice and assembly. The initial sleep memory refusal
is preserved in the v5 report; the distinct
[sleep retry report](writing-v5-high-sleep-retry/report.json) records a completed
legacy control and the new method’s actual content/timing rejection. A resource
refusal and a subsequent content failure are different outcomes.
Earlier v2/v3/v4 outputs remain iteration evidence, not additional final
independent successes. The smaller Standard Qwen3 4B pilot
([writing-pilot-v1/report.json](writing-pilot-v1/report.json)) failed the new method
after its one holistic revision: paragraphs exceeded their word/time budgets,
several left negative quiet time, and orientation/ending requirements were missed.
That failed raw evidence is retained. The CLI offers both writer profiles;
these results do not qualify Standard for reliable whole-session writing.
The owner's concurrent UI experiment is excluded from this backend publication.

Official Headspace material was used only for structural research: an explanation,
practice and integration, alternative anchors and progressively less guidance
([techniques](https://www.headspace.com/meditation/techniques),
[progression after Basics](https://www.headspace.com/articles/after-headspace-basics)).
The generated prose is original; no Headspace script, teacher identity or vocal
impersonation is a benchmark target.

## Same-voice writing audio

[writing-audio/render-report.json](writing-audio/render-report.json) records the
completed grounding pair through production Sherpa Kokoro, `am_michael`, seed 42
and native speed 0.8 for both. The legacy narration is 180.204 s (106.058 s speech,
74.146 s pauses); the new narration is 183.511 s (81.072 s speech, 102.439 s pauses).
Both preserve exact source-script hashes and requested pause sequences. This
shows the new script reaching a roughly three-minute assembled result with more
quiet and fewer speech segments. Independent narration ASR passed both under the
earlier tolerance policy:
[legacy](verification/writing-grounding-3m-plan-first.json) WER 1.869%, ordered
coverage 99.065%; [new](verification/writing-grounding-3m-recipe-whole-session.json)
WER 0.637%, ordered coverage 99.363%. Minor recognition differences remain in the
reports. The derived exact-word policy now marks both **Review**: four normalized
word edits for the legacy narration and one for the new narration. See the
[status-bearing writing manifest](writing-listening-policy-v3.json). No new ASR
was run, and neither result is a word-perfect claim. Human listening is pending, so
these checks do not establish preference. No new sleep audio was rendered from a
rejected script.

A separate real production High prompt-to-script-to-audio run also completed
([integration summary](production-new-writer/integration-summary.json)) using the
new method, default Kokoro `am_michael` speed 0.6 and planning rate 95 WPM. After one
holistic revision it produced 180.000125 s: 102.500125 s speech and 77.5 s exact
silence. Individual pauses span 6.567–15.826 s, all within the 16 s cap. Audio QC
passed and the resource guard recorded no stop reason. The initial sandbox
monitor refusal remains separate. Its
[independent narration ASR](verification/production-new-writer-grounding-3m.json)
passed under the earlier tolerance, WER 2.5% and ordered coverage 99.375%.
The [derived strict-policy receipt](verification/policy-v3/production-new-writer-grounding-3m.json)
now marks it **Review** for four normalized word edits, with the original
recognition and tokenization disagreements preserved. This is one completed daytime end-to-end
validation, not broad reliability or a human quality rating.

## Voice comparison

[voices/summary.json](voices/summary.json) is the machine-readable inventory;
[voices/REPRODUCE.txt](voices/REPRODUCE.txt) declares settings and limitations.
Every full audition uses the same authored 123-word, four-paragraph
[control script](voice-script.txt), not a production meditation template. Native
paragraph outputs are concatenated with no inserted samples. The short generated
synthetic reference is conditioning only and is excluded from the full-script
comparison. It represents no named real person.

| Candidate | Independent content result | Native seconds | Synthesis seconds | Output RTF |
| --- | --- | ---: | ---: | ---: |
| Kokoro Heart, native speed 0.8 | Pass, WER 0 | 50.25 | 7.79 | 0.16 |
| Kokoro Michael, native speed 1.0 | Pass, WER 0 | 44.53 | 21.43 | 0.48 |
| Kokoro George, native speed 0.8 | Pass, WER 0 | 54.13 | 9.55 | 0.18 |
| MLX Qwen VoiceDesign male | Pass, WER 0 | 45.60 | 40.73 | 0.89 |
| MLX Qwen Ryan follow-up | Pass, WER 0 | 60.16 | 48.79 | 0.81 |
| Chatterbox Multilingual v3, synthetic reference | Pass, WER 0 | 41.68 | 62.46 | 1.50 |
| MOSS Local v1.5, synthetic reference | Pass, WER 0 | 51.44 | 41.69 | 0.81 |
| VoxCPM2 designed male | Qualified by paragraph ASR plus exact assembly | 42.56 | 141.78 | 3.33 |
| Native Qwen MPS Ryan | Fail under localized-error rule | 54.08 | 635.59 | 11.75 |
| Dots SOAR | Incomplete: memory-pressure stop, no complete waveform | — | — | — |

RTF is synthesis wall time divided by native output duration, including native
model silence; load and GPU-lock wait are excluded. Lazy initialization can affect
these fresh-process receipts. Voices, runtimes, precision and native pacing differ,
so this table is not a controlled warm-speed ranking or a voiced-phonation rate.
Kokoro is the lightest measured group; MLX Qwen, Chatterbox, MOSS and Vox required
larger allocations. Process RSS and accelerator allocations overlap on unified
memory and must not be added.

Independent local Whisper receives no expected-text prompt. Eight of nine
completed full auditions qualified; Dots did not complete. The separate fresh
production Sherpa Kokoro control (speaker 16, native speed 0.6) passed under the
earlier tolerance with 123/123 ordered words and WER 0.00813, reflecting one added
initial word. The strict policy now marks that control **Review**. This
production receipt is distinct from the MLX Kokoro candidates and does not make
their speed/voice settings identical.

Important evidence that the summary alone must not conceal:

- MPS Ryan's original full-file ASR had 74% coverage and 26% WER. Independent
  recognition of its second native paragraph matched 0/32 words. The later
  contiguous-error policy fails the same preserved evidence on a 23-operation
  disagreement span. A rendered WAV was insufficient for content acceptance.
- The final MLX Ryan follow-up passed 123/123 words. It changes backend, FP16 to
  8-bit weights and generation-budget/EOS policy, so it demonstrates a working
  alternative on this Mac, without isolating the cause of the earlier failure.
- Vox's original full-file recognizer omitted its final 28 words. All four native
  paragraphs independently matched 123/123 combined. The aggregate report checks
  that the raw file is exactly their ordered FLOAT32 concatenation (2,042,912
  frames, 48 kHz mono). The original full-file failure remains. This qualifies the
  words through segment evidence; it does not claim full-file ASR succeeded or
  that joins sound natural.
- Dots was stopped during decoding after severe system swap pressure, before a
  speech waveform returned. Its resource failure says nothing about voice quality.
  A finite MLX allocator cap did not bound total system memory pressure. The final
  MLX Ryan run used the new live RAM/swap watchdog, observed no swap growth and no
  watchdog stop. Earlier candidates predate that guard.
- Actual white-noise, silence and earlier Qwen 0.6B smoke controls failed expected
  content recognition. Nonzero RMS, a plausible duration or a nonempty ASR
  transcript alone cannot pass this check.

The prepared policy-v3 reassessment contains **8 Pass / 4 Review** across the
listening selection: eight voice Pass results, one voice Review, and three
writing Reviews. It reclassifies existing hash-bound recognition evidence; it
contains no new recognition, audio render, or word-perfect claim. Native MPS Ryan
remains a failure and Dots remains incomplete outside that selection. Vox retains
its full-file failure alongside the qualified paragraph/assembly chain.
The [voice manifest](verification/listening/manifest-policy-v3.json) and
[writing manifest](writing-listening-policy-v3.json) retain raw/proof links and
make the review-labelled auditions available for listening.

See [verification/README.md](verification/README.md) for thresholds, exact reports,
ASR limits and negative controls. Existing listening copies use constant gain
only, targeting -23 LUFS subject to -1 dB true peak; hashes and gains are saved.
Raw outputs are untouched. No denoising, time stretch or ambience conceals defects.
Human warmth, register, naturalness, teaching preference, joins and long-listen
comfort remain unscored. No voice is promoted from these automated results.

## Implementation checks

The earlier local Python gate completed Ruff, formatting, strict mypy and 804 tests
(804 passed, one third-party Starlette/httpx deprecation warning). These behavioral
and structural checks predate the subsequent review fixes and include legacy-path
restoration; they do not certify those later changes or integrate all
benchmarked voices into production or establish meditation quality. Live
script/audio receipts and human listening remain separate from this gate.

## Licenses, integration and remaining work

The owner left Whoopy's source license undecided. A public repository alone does
not grant a source license. [voices/LICENSE-AUDIT.txt](voices/LICENSE-AUDIT.txt)
and its exact source snapshots separate runtime code, weights, codecs,
conversions and reference/output rights. The inspected model/code components for Kokoro, Qwen, Vox and MOSS
have Apache declarations; Chatterbox v3 and MLX-Audio have MIT terms. The actual
Sherpa/Kokoro and MLX Kokoro runtime paths also contain eSpeak/GPL dependencies.
Their full-bundle redistribution review is incomplete; model declarations do not
clear those dependencies. See the [Kokoro runtime audit](../../../../docs/kokoro-license-audit.md)
for pinned build sources, local inspection evidence, source-closure uncertainty,
and the distinction between paid network use and conveying software.
The S3TokenizerV2 conversion has no license field, despite identified upstream
Apache evidence; that gap remains visible before bundle redistribution.

Fish Drama 3 was excluded after resource preflight (23.78 GB of pinned files
before runtime/KV/codec, with insufficient available headroom). Its Community
License also has revenue, hosted-service, attribution and other conditions whose
application depends on the future product flow. Higgs TTS3 was excluded for
license fit: research/creator permissions do not establish permission to integrate
it into a third-party-facing product. Neither exclusion is a failed voice audition.
Permissive code/model terms do not clear another person's voice, recording or all
possible outputs. No model bundle redistribution is authorized by these receipts.

The new candidate runtimes remain isolated under ignored `models/experimental/`.
They are research auditions, not production-pack integration or readiness
promotion. Existing managed Kokoro/Qwen paths retain their own runtime/readiness
boundaries. API/cloud generation, publishing, courses and ambience were excluded.
The next quality decision requires human review of the actual scripts and
matched auditions, followed by longer first/middle/last identity checks and a
written promotion decision. The sleep challenger failed as described above.
The completed daytime production run and all three earlier narration ASR passes
remain recorded; all three are now **Review** under the strict word policy.
Automated screening is not a human rating.

[cleanup-receipt.json](cleanup-receipt.json) records 334 old runs and 589 old audio
files moved into ignored `.whoopy-recovery/20261028-193733/`, with models/source and
the new benchmark preserved. The active library/catalog was empty afterward.
This is recoverable cleanup, not secure erasure. Old acceptance receipts do not
certify the fresh source/models.
