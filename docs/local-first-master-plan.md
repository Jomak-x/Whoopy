# Whoopy Local-First Master Plan

This is the authoritative near-term plan for Whoopy as of **August 3, 2026**.
If an older phase document or the long implementation backlog disagrees with
this document about what happens next, use this document.

The immediate objective is deliberately narrow:

> Make one dependable, completely local meditation studio that can recover
> from interruption, create genuinely useful practices, and compare multiple
> speech models fairly enough for Jakob to choose a voice direction.

Do not begin the permanent Phase 4 product UI, public publishing, accounts,
social features, ambience expansion, or mobile packaging until the local exit
gate at the end of this document passes.

## Status Words

Whoopy previously used “done” too loosely. This plan uses four distinct states:

- **Merged** — the code is on `main`.
- **Implemented locally** — code exists in the working tree but is not merged.
- **Machine-verified** — the real model or workflow completed on this laptop.
- **Human-accepted** — listening tests show that the result is actually good.

A feature is not complete merely because it exists or produces a WAV file.
Voice and meditation quality require human acceptance.

## Reconstructed Project Record

| Work | Repository state | What it proved |
| --- | --- | --- |
| Phase 0 | Merged | native Python foundation, configuration, hardware checks, CI |
| Phase 1 | Merged | prompt to durable UUID run to worker to timeline |
| Phase 2 | Merged | exact silence and deterministic WAV assembly |
| Phase 3 | Merged | segment cache, checkpoints, retry, resume, and audio integrity |
| Phase 3.5 | Merged through PRs 5–11 | local artifacts, llama.cpp/Qwen planning, Kokoro speech, end-to-end CLI, temporary web studio |
| Phase 3.6 | Merged through PR 12; not human-accepted | slower Kokoro, adaptive pacing prototype, technique fields, Fish 1.4, MOSS 5B/8B adapters, model controls, smoothing, and expanded tests |
| PR 13 durable recovery | 180 local tests and static checks pass; CI pending | durable lifecycle state, recovery controls, worker coordination, and bounded diagnostics |
| Permanent Phase 4 | Not started | intentionally waits for the local exit gate |

PR 12 is merged. PR 13 passes the full local check suite and now awaits remote
macOS, Linux, and Windows CI before it can merge. See
[PR 13 durable recovery](./phase-4-pr13-durable-recovery.md) for the exact
lifecycle contract, commands, artifacts, and review checklist.

## What Actually Works Today

- A local prompt can be planned by Qwen3-4B through llama.cpp.
- Every accepted plan, drafted section, script, timeline, model identity,
  segment checkpoint, WAV, manifest, and quality report is inspectable.
- An authored script can skip the local LLM and use the lighter Basic path.
- Speech and exact zero-valued silence are assembled into 24 kHz mono PCM.
- Repeated healthy segments can be reused from verified checkpoints or cache.
- The temporary web studio can start a run, show recent runs, play audio, and
  inspect important artifacts.
- The current local working tree can select Kokoro, Fish 1.4, MOSS Local 5B,
  or the MOSS 8B entry when its checkpoint becomes complete.
- Twenty durable runs in `runs/` are marked completed.

That list proves an engine exists. It does **not** prove that the meditation is
good, the selected voice is right, or recovery is reliable enough.

## Honest Model Inventory On This MacBook

The development machine has 48 GB unified memory, Apple Metal, and enough free
disk for the planned local comparison. Model memory use still needs to be
measured; disk fit alone does not prove that simultaneous inference is safe.

### Text generation

| Model | Local state | Decision so far |
| --- | --- | --- |
| Qwen3-4B Q4_K_M | Installed, about 2.5 GB | current Standard planner; works but meditation prose is not human-accepted |
| Qwen3-1.7B Q8 | Installed, about 1.8 GB | Lite candidate failed the earlier strict generation evaluation; not an automatic fallback |

### Speech generation

| Model | License | Local state | Honest status |
| --- | --- | --- | --- |
| Kokoro | Apache-2.0 | Installed | portable baseline; multiple presets; slower `0.6` path works |
| Fish Speech 1.4 | CC BY-NC-SA 4.0 | Installed, about 2.5 GB | machine-verified non-commercial experiment; expression comes from reference audio |
| MOSS Audio Tokenizer v2 | Apache-2.0 | Installed, about 7.9 GB | required shared codec for MOSS v1.5 |
| MOSS Local Transformer v1.5 5B | Apache-2.0 | Installed, about 8.5 GB | machine-verified on Apple Metal; offline startup also verified |
| MOSS-TTS v1.5 8B | Apache-2.0 | only about 4.4 GB of a roughly 17 GB download | incomplete and correctly disabled; never claim it was tested |
| Qwen3-TTS 0.6B/1.7B family | Apache-2.0 | not installed | high-priority comparison because it offers preset voices, cloning, voice design, and instruction control |
| Fish S1-mini | CC BY-NC-SA 4.0 | not installed; gated | optional non-commercial comparison, not a product default |
| Fish S2 Pro | research/non-commercial license | not installed | official path recommends at least 24 GB GPU VRAM and Linux/WSL; not a supported Apple-local candidate |

“Try all models” must mean **all credible finalists in a declared comparison
set**, not every TTS repository on the internet. The local comparison set is:

1. Kokoro as the portable baseline.
2. Fish 1.4 as the already-working non-commercial reference experiment.
3. MOSS Local 5B.
4. MOSS flagship 8B once complete and stable.
5. Qwen3-TTS 0.6B CustomVoice.
6. Qwen3-TTS 1.7B CustomVoice.
7. Qwen3-TTS 1.7B VoiceDesign.
8. Qwen3-TTS 1.7B Base voice cloning if a consented reference is available.
9. Fish S1-mini only after its access terms are accepted and its value justifies
   the non-commercial restriction.

Fish S2 remains visible as “not compatible with this Mac workflow” instead of
being falsely presented as locally testable.

## What Is Not Good Enough Yet

### Meditation content

- Producing valid JSON and fitting a word count has been mistaken for quality.
- Too much prose is generic reassurance without a concrete practice.
- Sections can feel like independent filler paragraphs rather than one guided
  progression.
- The small local LLM can repeat sentence shapes, concepts, and openings.
- Sleep, grounding, reflection, and breath-focused requests need different
  structures rather than one generic template.
- There is no fixed listening set or scoring rubric that prevents subjective
  “this seems better” changes from regressing later.

### Pauses and delivery

- The current adaptive pause rules are a useful prototype, not a chosen rhythm.
- Text heuristics do not understand every sentence's actual instructional load.
- Speech-to-silence ratios are not tailored by meditation type or duration.
- There is no automated warning for a long uninterrupted lecture-like block.
- Per-segment TTS restarts can still expose changes in tone or metallic onsets.

### Breathing practices

- Whoopy currently treats mindfulness breath awareness and deliberate
  breathwork as if they were the same safety problem.
- Natural breath observation works, but timed exercises are broadly rejected.
- Asking an LLM to invent inhale, exhale, and hold timing would be unsafe and
  nondeterministic.
- Breath cues need a typed, reviewed timing engine and an immediate opt-out.

### Reliability

There are currently **two run records stuck in `running` since July 27**. One
had completed 12 of 26 speech segments. Neither contains a final error. This
proves that checkpoints exist but lifecycle recovery is incomplete.

The missing pieces are:

- an owner lease or heartbeat for a running process;
- startup reconciliation of abandoned `running` records;
- durable worker stderr/stdout logs;
- an explicit recoverable `interrupted` state;
- signal handling for browser cancellation and terminal shutdown;
- a watchdog and stage-specific timeout;
- visible resume and segment-regeneration actions in the studio;
- preflight memory checks and one-heavy-model-at-a-time loading; and
- soak tests that kill a real run at multiple stages.

## Research Principles For Better Meditations

Whoopy may learn from public descriptions of technique and structure. It must
not copy proprietary scripts, distinctive wording, narrators, or brand style.

- Headspace distinguishes focused attention, body scan, noting, visualization,
  reflection, loving kindness, and resting awareness. That supports selecting
  one real technique rather than producing generic calm-sounding prose:
  [Headspace techniques](https://help.headspace.com/hc/en-us/articles/115011850767-What-are-the-techniques).
- Headspace describes guided practice as instruction plus periods of silence,
  including paths that gradually use more silence:
  [Headspace guided meditation](https://www.headspace.com/meditation/guided-meditation).
- Both Headspace and Calm say ordinary mindfulness should observe breathing
  without trying to change its natural rhythm:
  [Headspace breathing](https://help.headspace.com/hc/en-us/articles/215058708-Is-There-a-Particular-Way-I-Should-Breathe),
  [Calm breath meditation](https://www.calm.com/blog/breath-meditation).
- Calm describes a useful beginner arc: comfortable position, chosen anchor,
  noticing distraction, gentle return, and a clear ending:
  [Calm mindfulness meditation](https://www.calm.com/blog/mindfulness-meditation).
- Headspace's body-scan guidance moves systematically through body areas and
  pauses to observe rather than trying to fix sensations:
  [Headspace body scan](https://www.headspace.com/meditation/body-scan).

These become structural requirements and evaluation criteria, not text to
imitate.

## Meditation Quality Contract

Every generated meditation must be scored on the following dimensions before
it can be called good:

1. **Request fidelity** — it addresses the requested situation and duration.
2. **Technique integrity** — every section performs a named technique.
3. **Progression** — arrival, practice development, space, and ending form one
   coherent arc.
4. **Instruction clarity** — the listener always knows what to notice or do.
5. **Specificity** — concrete anchors and sensations replace vague filler.
6. **Non-repetition** — ideas and sentence frames do not loop.
7. **Pacing fit** — silence follows cognitive and embodied workload.
8. **Voice fit** — delivery is warm, stable, intelligible, and non-metallic.
9. **Safety** — no diagnoses, guarantees, coercion, unsafe breath timing, or
   shame; every exercise permits opting out.
10. **Ending fit** — daytime practices reorient; sleep practices taper without
    waking the listener.

The evaluation set must cover at least:

- beginner breath awareness;
- short grounding after a busy day;
- body scan;
- stress without medical treatment claims;
- self-compassion;
- focus;
- reflection on the day;
- visualization;
- sleep; and
- a deliberate paced-breath exercise.

Each candidate produces the same artifacts. Automatic checks reject structural
failures; blind human ratings decide voice and experiential quality.

## Breathing Contract

Whoopy will expose two different practice kinds.

### Breath awareness

- Breathing stays natural.
- Attention can rest at the nostrils, chest, abdomen, or whole-body movement.
- Wandering is acknowledged and attention returns gently.
- There are no required counts, holds, or deep breaths.

### Deliberate paced breathing

- Timing comes from a reviewed typed protocol, never free-form LLM output.
- The first supported patterns use gentle inhale/exhale cycles without holds.
- Instructions say not to force or strain the breath.
- The listener can return to normal breathing or stop at any time.
- The exercise warns the listener to stop if dizzy or lightheaded, consistent
  with NHS guidance:
  [Ashford and St Peter's NHS guidance](https://www.asph.nhs.uk/nervous-system-regulation).
- Cue and silence durations are exact timeline events.
- Unsupported holds, rapid hyperventilation, or extreme ratios are rejected.

Later breath patterns require their own review and tests. They are not unlocked
by asking the model for creative timing.

## The Local-Only PR Sequence

Each step below is one PR. Merge in order. Every PR must update this document's
status table, include automated tests, and include exact manual listening or
failure-injection steps where relevant.

### PR 12: Reconcile And Finish The Current Phase 3.6 Work

Status: **merged**. Its voice and meditation-quality work remains subject to
later listening acceptance; merging code does not choose a final voice.

Scope:

- commit the breathing false-positive fix;
- commit technique-aware planning and drafting;
- commit the current adaptive pause and boundary-smoothing prototype;
- commit Fish 1.4 and MOSS 5B/8B adapter/UI work;
- keep incomplete MOSS 8B disabled through full shard validation;
- add this authoritative plan and correct stale roadmap/README claims; and
- rerun CI on the complete PR, not only its original commit.

Exit criteria:

- clean working tree after push;
- CI passes on macOS, Windows, and Linux;
- Kokoro, Fish 1.4, and MOSS 5B appear ready on this Mac;
- MOSS 8B appears incomplete, not ready;
- the exact earlier `section:breathe` regression is covered; and
- no claim that meditation or voice quality is final.

### PR 13: Make Interrupted Runs Recoverable

Status: **implemented locally; checks and CI pending**.

Goal: no process death may leave a run permanently pretending to be active.

Scope:

- add worker owner ID, PID, heartbeat, and lease expiry;
- add schema migration and a recoverable `interrupted` state;
- reconcile stale `running` records when the CLI or studio starts;
- catch normal termination signals and preserve completed checkpoints;
- persist bounded worker logs per run;
- add stage and segment timeouts;
- add Resume and Retry controls to the temporary studio; and
- test forced termination during planning, synthesis, assembly, and QC.

Exit criteria:

- the two existing abandoned runs are recognized as interrupted;
- restart resumes at the first unhealthy segment;
- completed segments are not repeated;
- UI and CLI show the actual failure stage and log; and
- no forced-crash test leaves `running` behind.

### PR 14: Turn Experimental Voices Into Managed Local Models

Goal: make the current comparison reproducible and safe before adding more
models.

Scope:

- extend the artifact manifest to optional TTS packs;
- make Fish and MOSS paths configuration-driven rather than source constants;
- verify runtime, checkpoint, codec, reference, license, and complete shards;
- finish the MOSS 8B download;
- add install/status/remove commands with disk and memory preflight;
- prevent two heavyweight TTS models from being loaded simultaneously;
- keep all inference offline after installation; and
- preserve a model's exact controls and license in every run.

Exit criteria:

- a clean setup can install each declared pack intentionally;
- interrupted downloads resume safely;
- Kokoro, Fish 1.4, MOSS 5B, and MOSS 8B each pass the same short synthesis
  contract or show an honest incompatible state;
- no partial checkpoint appears selectable; and
- model removal cannot delete unrelated data.

### PR 15: Add The Qwen3-TTS Comparison Family

Goal: add the strongest current Apache-2.0 local voice-control candidates before
choosing a default.

The official Qwen release provides 0.6B and 1.7B models for preset voices and
cloning, plus 1.7B VoiceDesign with natural-language control:
[Qwen3-TTS model card](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice).

Scope:

- add an isolated Qwen3-TTS runtime and typed adapter;
- support 0.6B and 1.7B CustomVoice;
- support 1.7B VoiceDesign;
- support 1.7B Base cloning only with an explicit consented reference;
- expose only controls supported by the selected variant;
- measure load time, peak memory, real-time factor, and output integrity; and
- refuse variants that do not fit the laptop's live-memory margin.

Exit criteria:

- every compatible variant creates the standard fixed comparison clips;
- switching variants unloads the previous heavyweight model;
- runs remain offline and resumable; and
- licenses and references remain inspectable.

### PR 16: Build A Blind Voice Bake-Off In The Studio

Goal: choose a voice with evidence instead of remembering unrelated samples.

Scope:

- define one fixed comparison script set: arrival, ordinary guidance, body
  scan, breath cue, reflection, and sleep ending;
- render identical text and timeline silence for every model/voice;
- loudness-match comparison outputs without hiding clipping;
- randomize model labels during rating;
- rate warmth, calmness, naturalness, intelligibility, expression, onset
  quality, sentence endings, and overall preference;
- save ratings locally with hardware, model, seed, controls, and timing; and
- produce a comparison report and shortlist.

Exit criteria:

- at least Kokoro, Fish 1.4, MOSS 5B, MOSS 8B if compatible, and compatible
  Qwen variants are compared;
- no model sees different words or silence;
- every sample passes audio integrity before rating; and
- one portable default plus optional high-quality and low-resource fallbacks
  are chosen explicitly.

### PR 17: Add A Meditation Evaluation Harness Before Rewriting Again

Goal: make “these meditations suck” reproducible as failing criteria.

Scope:

- version the ten-prompt evaluation set listed above;
- add structural checks for technique, progression, repetition, specificity,
  ending behavior, and breath mode;
- add timeline checks for uninterrupted speech, pause distribution, and
  speech-to-silence balance;
- add a local rating screen for the ten quality dimensions;
- preserve every prompt, seed, raw attempt, score, and reviewer note; and
- establish the current system as the baseline rather than hiding its failures.

Exit criteria:

- the same command reruns the full corpus;
- failed dimensions point to exact sections and sentences;
- baseline results are committed as a small JSON/Markdown report; and
- later content changes can prove improvement.

### PR 18: Replace Generic Prose With Technique Blueprints

Goal: make the LLM guide a practice instead of writing calming filler.

Scope:

- create original reviewed blueprints for grounding, focused attention, body
  scan, noting, reflection, loving kindness, visualization, open awareness,
  and sleep;
- make each blueprint define entry, anchor, wandering/return instruction,
  practice intervals, and ending;
- allocate silence before drafting prose;
- generate sparse narration around the practice skeleton;
- reject repeated concepts and empty reassurance;
- add candidate generation/selection only if it improves the fixed corpus; and
- keep all proprietary app scripts out of training fixtures and prompts.

Exit criteria:

- all ten evaluation prompts have coherent technique-specific plans;
- every spoken sentence has a traceable job;
- sleep never re-alerts;
- body scans move systematically; and
- blind content ratings materially beat the PR 17 baseline.

### PR 19: Build Pacing Engine V2

Goal: make silence intentional at phrase, instruction, practice, and section
levels.

Scope:

- replace keyword-only pause inference with explicit pause intent from the
  validated plan;
- support micro, ordinary, embodied, reflection, practice, and extended
  silence events;
- give each meditation type a provisional speech/silence profile;
- reject overly long uninterrupted speech;
- group sentences only when the selected TTS benefits;
- add model-specific pre-roll/post-roll handling without changing canonical
  timeline timing; and
- run blind pacing comparisons rather than freezing arbitrary values.

Exit criteria:

- no benchmark contains an unexplained lecture-length speech block;
- pause decisions are visible in plan, script, and timeline artifacts;
- exact rendered silence matches the plan;
- onset artifacts do not recur at every sentence; and
- pacing ratings beat the PR 17 baseline across short, standard, and sleep
  practices.

### PR 20: Add Reviewed Breathing Exercises

Goal: support genuinely useful breath-focused sessions without weakening the
ordinary mindfulness safety rules.

Scope:

- add typed `breath_awareness` and `paced_breathing` modes;
- add a small reviewed no-hold protocol catalog;
- compile inhale/exhale cues and durations directly into the timeline;
- add comfortable-rate, opt-out, and dizziness/lightheadedness language;
- prevent the LLM from changing protocol timing;
- make breath cue voice samples part of the voice bake-off; and
- test duration, cycle count, cancellation, and unsupported-pattern rejection.

Exit criteria:

- natural awareness never becomes forced breathwork accidentally;
- paced exercises complete the exact declared number of safe cycles;
- cue timings are deterministic and inspectable;
- stopping returns immediately to ordinary breathing; and
- dedicated human listening confirms that cues and spaces feel usable.

### PR 21: Local V1 Soak Test And Acceptance

Goal: decide whether the local engine is finally good enough to build a product
around.

Scope:

- run the ten-prompt corpus across the shortlisted voice paths;
- run at least 20 end-to-end meditations of 1, 3, 5, 10, and 20 minutes;
- force crashes and cancellations at every major stage;
- repeat the entire accepted path with networking disabled;
- verify cache/resume behavior after model and setting changes;
- document memory, disk, startup, and generation-time envelopes; and
- write the final local-model and voice decision record.

Exit criteria:

- zero stale `running` records;
- zero unrecoverable tested interruptions;
- all WAV/timing/clipping checks pass;
- the fixed meditation corpus meets the agreed human rating threshold;
- at least three meaningfully different voice paths can be compared;
- one default and documented fallbacks are chosen; and
- setup and testing work from one beginner-readable local guide.

## Local V1 Exit Gate

Only move to the permanent Phase 4 UI when all are true:

- [x] PR 12 is merged.
- [ ] PR 13 passes checks and CI, and its working tree is clean.
- [x] Abandoned runs become interrupted and resumable automatically.
- [ ] Model packs install and report readiness reproducibly.
- [ ] The complete compatible voice comparison set has been rendered.
- [ ] Jakob has completed a blind voice rating and chosen a direction.
- [ ] The ten-prompt meditation corpus materially beats its baseline.
- [ ] Pacing passes both exact-timing checks and human listening.
- [ ] Breath awareness and reviewed paced breathing both work.
- [ ] Twenty-run and forced-crash soak tests pass offline.
- [ ] Documentation matches the commands a beginner actually runs.

Until this checklist passes, the current web page is a local laboratory—not
the finished Whoopy product.

## The Next Action

Verify and merge PR 13 before downloading more models. A workflow that can
leave jobs stuck in `running` makes model experiments slower and less
trustworthy. Voice acquisition follows immediately in PRs 14–16, before the
content and pacing acceptance cycle in PRs 17–20.
