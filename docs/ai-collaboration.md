# Working With AI On This Project

This project is a good fit for AI-assisted development, but only if you keep the work bounded and reviewable.

## The Main Rule

Ask AI to do one small thing at a time.

Why:

- large prompts hide mistakes
- small tasks are easier to verify
- the system has enough architectural complexity that unbounded help becomes noise

## Recommended Model Strategy

Use OpenAI/Codex for most day-to-day work:

- docs
- scaffolding
- implementation steps
- tests
- small refactors

Reserve Claude for heavier cases:

- long-context architecture review
- high-risk tradeoff analysis
- unusually large refactors

Why:

- you want to save the more expensive or limited budget for work that truly benefits from it
- most tasks here should be handled with focused, bounded prompts

## What To Ask For

When you prompt AI, ask for all of these:

- what it is changing
- why the change is needed
- what assumptions it is making
- how the parts interact
- how it would be tested
- what could go wrong

Why:

- the answer becomes a design review, not just a patch
- you learn the reasoning, not only the result

## How To Challenge A Decision

Use these questions whenever the AI proposes a design choice:

- What problem does this solve?
- What does it cost?
- What breaks if we do nothing?
- What breaks if we do it a different way?
- Is this decision reversible?
- Does it preserve the timeline-first and local-first constraints?

If the answer cannot explain the tradeoff clearly, the decision is not mature yet.

## Prompt Template For Implementation Tasks

```text
Implement only the next bounded step from the roadmap.

Context:
- current state: ...
- files to touch: ...
- constraints: ...
- acceptance criteria: ...

Please:
- make the smallest correct change
- explain the reasoning for each design choice
- list any assumptions or tradeoffs
- include tests or validation steps
- do not expand the scope
```

## Prompt Template For Architecture Review

```text
Review this design decision against the project constraints.

Decision:
- ...

Constraints:
- local-first on Apple Silicon
- deterministic audio timing
- canonical timeline as source of truth
- license-safe public sharing

Please:
- evaluate the tradeoff
- name the risks
- suggest alternatives
- say whether the decision should be kept, changed, or deferred
```

## How To Use AI Well Here

1. Start with the spec, not with code generation.
2. Make the model restate the system in its own words.
3. Ask for a small implementation plan before asking for code.
4. Require reasoning for every important choice.
5. Run a review pass before merging anything.
6. Keep the timeline schema stable unless there is a strong reason to change it.

## Good Habits For Learning

- Ask AI to separate facts from assumptions.
- Ask for examples of failure cases.
- Ask how the system will be debugged later.
- Ask what would make the design simpler.
- Keep a decision log for anything that feels irreversible.

## General System Design Advice

These are patterns worth remembering beyond this project:

- Make the most important data structure explicit and durable.
- Keep slow or failure-prone work behind a queue.
- Separate orchestration from execution.
- Put backend selection behind interfaces.
- Make outputs reproducible before making them scalable.
- Optimize for the path you will actually run every day.

This is why the Serenity design keeps returning to timelines, adapters, and local execution.
