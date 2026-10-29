# Meditation Engine Reset

Status: proposed restart boundary, 2026-08-06.

This document replaces the assumption that a schema-valid, correctly timed script is
a good meditation. The first target is one excellent single meditation. Courses,
bulk generation, and a large user-facing technique selector remain out of scope.

## Product contract

The user supplies:

- what they need in natural language;
- duration;
- optionally, whether they are sitting or lying down.

Whoopy infers the practice. Studio does not expose eleven meditation techniques,
breath ratios, pause types, or generation methods.

Most ordinary guided sessions follow a recognizable Whoopy grammar:

1. choose a comfortable position and retain permission to adjust;
2. orient through contact or sound;
3. arrive through natural breathing;
4. optionally practise one reviewed paced-breathing variant;
5. develop one main practice suited to the request;
6. allow a quieter integration period;
7. reorient for daytime or taper for sleep.

Consistency comes from this grammar. Variety comes from reviewed breath variants,
anchors, main practices, imagery, reflection prompts, and language—not from asking
the model to invent a new session structure every time.

## What the current evidence says

The current automatic corpus is not quality evidence:

- Qwen3 1.7B's corpus reports 10/10 automatic acceptance while human review is pending.
- The actual scripts contain posture contradictions, repeated filler, meaningless
  instructions, and unprepared 70–120 second silences.
- The real editorial checker accepts the visibly poor sleep, reflection, and body-scan
  scripts.
- Forty-five meditation and pacing tests pass at the same time.
- Running the same corpus through Qwen3 4B improves fluency, but still produces
  repeated questions, posture errors, invented "energy," dangling references, and
  poor silence cadence.

The authored comparison script remains coherent through the same downstream audio
pipeline. TTS is therefore not the cause of the writing failure.

## Root causes to remove

1. The current blueprints are labels, not timed session recipes.
2. The same generic purpose is repeated across every beat.
3. Breathing protocols exist in code but are not used by prompt generation.
4. Each section is written without the preceding prose or future beat context.
5. Automatic sentence selection can delete context and damage coherence.
6. Sparse prose is rewarded, then missing duration is filled with huge silences.
7. Regex and schema validators cannot judge whether guidance is physically sensible.
8. Studio defaults do not correspond to a human-promoted evaluation winner.

## Replacement architecture

```text
Natural-language need + duration + optional posture
  -> hidden intent and risk router
  -> versioned human-reviewed session recipe
  -> deterministic posture, breathing, beat, and silence contracts
  -> one whole-session LLM draft with full context
  -> structural and safety rejection (no prose surgery)
  -> one holistic revision with all feedback
  -> human content review
  -> deterministic timeline compilation
  -> MOSS and Qwen rendering
  -> blind listening review
```

The LLM writes natural language. It does not choose breathing ratios, holds, posture,
beat order, silence duration, or ending behavior.

## Hidden routing

The router chooses a recipe rather than a user-facing technique label.

- sleep or bedtime -> lying-down wind-down, gentle no-hold breathing, body scan or
  low-stakes imagery, sleep taper;
- day reflection -> settling and breathing, one bounded event, one real question,
  integration, daytime return;
- overstimulation or stress -> external orientation, optional natural breath, sensory
  grounding, focused anchor, daytime return;
- anxious thoughts -> external grounding first, breath or sound choice, simple noting,
  concrete return;
- focus -> settling, even breathing or natural breath, one attention anchor, repeated
  return practice;
- setback or self-criticism -> settling, natural breath, acknowledgment, modest
  self-compassion practice, daytime return.

Duration changes repetitions and silence—not the number of unrelated techniques.

## Breathing contract

Natural breathing is always available. The listener is never required to change the
breath.

Initial reviewed variants:

- natural observation;
- gentle 4-second inhale / 4-second exhale, no hold;
- gentle 3-second inhale / 4-second exhale, no hold;
- optional box breathing only when explicitly requested, with a reviewed short ratio,
  few cycles, opt-out, and no use as the default for sleep or acute anxiety.

Exact phases compile to typed timeline events. The LLM cannot invent seconds or holds.
The listener is told to return to ordinary breathing if timing feels strained and to
stop if dizzy, lightheaded, breathless, panicked, or uncomfortable.

## First vertical slice

Build only one recipe first:

**Ten-minute good-night reflection**

1. lying-down comfort and permission to adjust;
2. contact with the bed and ambient sound;
3. natural breathing followed by an optional reviewed 3-in/4-out sequence;
4. ordered, neutral body scan;
5. reflection on one bounded moment from the day;
6. one question with genuine response space;
7. release the reflection and return to bodily contact;
8. progressively sparser sleep taper with no waking reorientation.

The already-liked authored meditation is a quality reference, not text to copy. The
new recipe and prose must remain original.

## Evaluation reset

Automatic checks are gates, not a quality score.

Keep roughly:

- ten hard safety and recipe-contract tests;
- six deterministic compiler/timeline tests;
- three semantic golden regression cases;
- one real-model single-case bakeoff with saved human ratings.

Retire implementation-detail tests for sentence surgery, fixed pause-allocation
arithmetic, and mocked "good prose" that only proves parsing.

Hard failures include:

- wrong posture or ending;
- missing or invented breathing phases;
- unreviewed holds or timing;
- medical promises or forced persistence through discomfort;
- missing recipe beat or wrong order;
- unexplained long silence;
- corrupt structured output.

Human ratings, each 1–5:

1. sounds like an experienced meditation teacher;
2. coherent and useful practice arc;
3. natural spoken language;
4. appropriate guidance and silence balance;
5. enough time to follow each instruction;
6. would voluntarily finish and use again.

Promotion requires no hard failure, median at least 4 on every dimension, no rating
below 3, and a blind preference win against the current baseline.

## Model decision order

1. Compare the current section-based method with recipe + whole-session generation on
   Qwen3 4B using three prompts.
2. If the new method still lacks language quality, test one larger challenger at a
   time. First candidate: Ministral 3 14B Instruct. Second candidate only if needed:
   Qwen3 30B-A3B Instruct.
3. Do not fine-tune until recipe-first prompting and human evaluation establish a
   reproducible baseline.

Suggested controlled prompts:

- ten-minute good-night meditation after a busy day;
- six-minute steadying practice before an important meeting;
- five-minute after-work practice for someone explicitly requesting box breathing.

## Research basis

- [NCCIH: meditation effectiveness and safety](https://www.nccih.nih.gov/health/meditation-and-mindfulness-effectiveness-and-safety)
- [MBI-TAC teaching criteria](https://www.mindfulnessstudies.com/wp-content/uploads/2021/04/MBI-TAC-2017.pdf)
- [NHS MBSR exercises](https://www.guysandstthomas.nhs.uk/health-information/mindfulness-based-stress-reduction-mbsr/mbsr-exercises)
- [NHS gentle breathing guidance](https://www.nhs.uk/mental-health/self-help/guides-tools-and-activities/breathing-exercises-for-stress/)
- [VA diaphragmatic breathing guidance](https://www.va.gov/WHOLEHEALTHLIBRARY/docs/The-Power-Of-Breath-Diaphragmatic-Breathing.pdf)
- [VA grounding and trauma-sensitive guidance](https://www.ptsd.va.gov/gethelp/coping_stress_reactions.asp)
- [American Heart Association breathing precautions](https://www.heart.org/en/healthy-living/healthy-lifestyle/stress-management)
- [Headspace's public ten-minute structure](https://www.headspace.com/articles/how-to-meditate-in-ten-minutes)
- [Headspace's public Sleepcasts description](https://www.headspace.com/sleep/sleepcasts)
- [Ministral 3 14B official model card](https://huggingface.co/mistralai/Ministral-3-14B-Instruct-2512-GGUF)
- [Qwen3 30B-A3B official model card](https://huggingface.co/Qwen/Qwen3-30B-A3B-Instruct-2507)
