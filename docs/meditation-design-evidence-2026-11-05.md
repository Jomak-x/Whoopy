# Meditation design evidence and writing evaluation

Reviewed 5 November 2026. This is a design rationale and proposed editorial
evaluation, not evidence that Whoopy has clinical efficacy or Headspace-equivalent
quality. The implementation changes the whole-session writer only. Original
scripts remain generated locally; none of the sources below is a script to copy.

## Evidence and design inference

Headspace describes a useful teaching sequence: explain the practice, guide an
attempt, and suggest how to use it in ordinary life. Its public technique catalog
also includes visualization, reflection, compassion, body scanning, and open
forms of attention. The design inference is to preserve a coherent session arc
while giving each technique compatible instructions. A universal instruction to
keep returning to one anchor can conflict with a deliberate body scan, imagined
scene, or broadening of awareness. Sources: [official teaching structure](https://www.headspace.com/meditation/techniques)
and [official technique catalog](https://help.headspace.com/hc/en-us/articles/115011850767-What-are-the-techniques).

Headspace's public basics distinguish observing ordinary respiration from counting
successive breaths, and include settling, practice, reduced direction, and a
connection to the next activity. The design inference is to teach one action,
give time to try it, and reduce explanation as familiarity increases. Counting
successive natural breaths is different from prescribing seconds per breath, but
this patch does not change Whoopy's current prohibition on generated counting or
pacing. [Official basic meditation process](https://www.headspace.com/meditation/meditation-101).

Headspace describes its sleepcasts as a wind-down followed by an atmospheric tour
without conventional plot progression. This suggests a separate, opt-in narrative
experience with low cognitive demand. The current patch permits a simple original
scene in the existing visualization kind; it does not add sleep stories or claim
that scenery improves sleep. Grounding should continue to use actual experience.
[Official sleepcast description](https://www.headspace.com/sleep/sleepcasts).

The NHS describes gentle breathing with optional counting and emphasizes comfort
without force. A remote randomized trial of repeated five-minute practices found
benefits for breathwork, particularly cyclic sighing, relative to its mindfulness
comparator on mood and respiratory rate. These are specific protocols and
outcomes, not support for unrestricted generated breathwork. [NHS guidance](https://www.nhs.uk/mental-health/self-help/guides-tools-and-activities/breathing-exercises-for-stress/)
and [Balban et al., 2023](https://pubmed.ncbi.nlm.nih.gov/36630953/).

A 400-person randomized trial found no additional improvement from approximately
5.5 breaths/minute compared with a matched 12-breaths/minute intervention on stress
and related outcomes. Whoopy should not promise that one ratio is uniquely best,
that a breath instruction resets the nervous system, or that a listener will
improve. [Fincham et al., 2023](https://pubmed.ncbi.nlm.nih.gov/38092805/).

A randomized study of brief digital meditation found reported distress no more
common than internet use as usual, while some participants still experienced
distress. This does not establish the safety of generated personalized scripts.
The design inference is to retain permission to adjust, stop, or use another
permitted practice, without forcing relaxation or treating emotion as a task to
complete. [Brief-practice adverse experiences trial](https://pmc.ncbi.nlm.nih.gov/articles/PMC13693860/).

## Implemented contract

- Explicit permitted anchors precede defaults. Breath exclusion can lead to
  contact when requested, rather than always selecting sound. Excluded sound is
  also omitted from orientation and daytime return instructions.
- Reflection requires an affirmative request. A busy day or good-night greeting
  alone no longer inserts a reflective question. An explicit kind that conflicts
  with an exclusion fails before generation rather than silently overriding it.
- Sleep without a body scan uses an anchor-rest recipe. Requested reflection can
  still have a question, response space, release, and sleep taper without a scan.
- The writer receives the selected technique's progression. Visualization moves
  from settling to one optional imagined scene and then releases it. Open
  awareness broadens after settling. Neither requires simultaneous attention to
  an anchor and another competing task. Plan practice metadata records the
  selected technique.
- Context should affect the practical emphasis and ending, without inventing a
  personal history or repeatedly naming the user's problem. Imagery is reserved
  for the selected visualization kind; a listener need not form a vivid picture.
- The first full draft and at most one holistic revision remain intact. No prose
  surgery, filler insertion, budget bypass, or failed-attempt deletion is added.
  Existing breath, posture, safety, and sleep-ending boundaries remain in place.

The prompt is version 9. Existing structural recipes are version 2; the two
anchor-rest sleep variants begin at version 1. Config files are packaged directly
through setuptools data-files, so there are no separate packaged prompt/recipe
copies to synchronize.

The v8 revision reduces the shared system prompt from 1,031 to 593 words while
retaining the selected-technique contract and hard gates. It puts paragraph
targets/caps first, states concrete daytime-ending obligations, and reports the
effective global ceiling as the smaller of timing capacity and summed paragraph
caps. Shared validation now rejects evidenced actual-safety assertions such as
"you are in a safe space", while distinguishing explicit hypothetical or
conditional clauses. Preference parsing preserves comma-separated exclusion
lists; prohibiting prescribed breathing modifications does not prohibit natural
breath observation, and conditional fallback instructions do not replace the
primary anchor. The checked-in six-case corpus is covered by routing regressions.

These changes respond to a failed local Qwen3 4B run, not a successful quality
comparison. See the [retained draft annotation](meditation-v7-standard-grounding-review-2026-11-05.md).
No v8 real-model improvement is established by the deterministic tests.

Version 9 is a bounded request-coverage experiment, not a demonstrated quality
improvement. Its 634-word system prompt asks the writer to assign affirmative
requests and listener options to existing beats, preserve optional/conditional
actions, and retain or restore them during the single holistic revision. It adds
no request extractor, response fields, retry, prose transformation, or gate bypass.
The raw original request remains in both writer calls. The six-case v8 editorial
snapshot found omitted alternatives and lost context even in accepted scripts.
Tests verify delivery of the instructions, not whether a real writer follows them.

The shared sound-orientation gate also recognizes ordinary “begin/start by
noticing whatever sounds” and “notice what sounds” constructions. Bounded
negation, imagined/memory context and internal-body sound counterexamples remain
excluded. Present sound perception does not endorse invented sound examples as
good writing. Historical rejected drafts and their outcomes remain unchanged.

For the comparison, freeze both arms with this same shared validator and the
same model, requests and seeds. The original v8 prompt remains in
`evaluations/benchmarks/2026-11-05/diverse-8b-v8-seed42-1012/source-snapshot/config/prompts/whole_session_system.md`
in the primary checkout (file SHA-256
`fbb27eb9e84b97e7addaaaa0cf5dd924913efae0185816b2ca271b9955bc1301`).
Use initial seed 42/revision 43 for the first matched run; add a predeclared second
seed if resources allow. Preserve every failure and judge request coverage
separately from mechanical acceptance. Human listening is still outstanding.

Routing remains a bounded deterministic language contract, not a general semantic
parser. Regex checks catch defined failure patterns and may miss paraphrases;
the editorial review below is still necessary. Paced breathing, breath holds,
cyclic sighing, and a new sleep-story modality remain outside this patch.

## Diversified local writing corpus

Use the exact same locally available writer, seed, sampling, context, duration,
and timing settings for baseline/candidate comparisons. Run at least two seeds
when resources permit; preserve every initial draft, revision, validation error,
and model invocation. Report first-draft acceptance separately from acceptance
after revision. A resource failure is an outcome, not an editorial pass.

| Need | Duration | What the reviewer must check |
| --- | --- | --- |
| Meeting preparation; no breathing | 3 min | Specific returning practice and useful next-task cue |
| Overstimulation; no sound or breath; contact requested | 3 min | Contact chosen, no auditory orientation or return |
| Ordinary breath awareness; no imagery | 5 min | Unforced breathing, no timing or invented scenery |
| Explicit paced or box breathing request | 3 min | Unsupported behavior is not improvised or disguised |
| Worried thoughts; no imagery | 5 min | Simple noting, no analysis or treatment claim |
| Self-criticism after a setback | 5 min | Optional kindness without forced positivity |
| Seated body scan | 5 min | Ordered body areas, physical compatibility, neutral sensations |
| Sleep; no reflection and no body scan | 5 min | Genuine alternative, fewer demands, sleep taper |
| Requested good-night reflection; no body scan | 5 min | One question, response space, release, no scan |
| Explicit visualization of an original quiet place | 10 min | Clear invitation, low plot, no required vividness |
| Experienced listener; open awareness | 10 min | Coherent broadening and less instruction, not repetition |
| Tired but not sleep; no breath or sound | 3 min | Daytime ending and preserved exclusions |

## Review rubric and promotion boundary

First check non-negotiable failures: explicit exclusions, unsupported breathwork,
prescribed emotional outcomes, treatment claims, posture contradictions, wrong
ending, and failed structural/timing contracts. Passing these checks only makes a
draft eligible for editorial review.

Score each dimension 1–5 with anchored meanings: 1 contradicts the request or is
unusable; 3 is usable but generic, repetitive, or uneven; 5 is specific, clear,
coherent, and easy to follow. Record evidence from the actual text for every score
below 3 and a concrete suggested correction.

1. Request fidelity: does the practice fit the need and stated preferences?
2. Progression: does each stage build on the earlier practice?
3. Instructions: can the listener tell what to do without solving a puzzle?
4. Personalization: does context change the practice, not just its introduction?
5. Spoken language: is it natural, concise, varied, and free of stock reassurance?
6. Agency: can experience differ, attention wander, and the listener adjust?
7. Silence: is there a meaningful, manageable action before each quiet interval?
8. Ending: is it appropriate for sleep/daytime and connected to the practice?

Keep the reviewer blind to method labels where practical. Record overall
preference, willingness to replay, and whether the script could have been written
for almost anyone. Proposed promotion criteria: no hard failures, no dimension
below 3, and a clear preference for the candidate on representative needs rather
than one successful example. These are product criteria, not validated clinical
scales; do not optimize to a single aggregate number.

Only after selecting scripts should matched-voice listening assess time to act,
actual quiet, warmth, fatigue, transitions, and replay preference. Text estimates,
unit tests, ASR checks, and research citations cannot establish those qualities.
