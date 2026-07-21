# Contributing To Serenity

Serenity is built as a sequence of small, reviewable pull requests. A contribution should preserve the local-first, timeline-first, adapter-driven architecture unless it explicitly proposes and justifies a design change.

## Before Starting

1. Read the architecture and the next step in the implementation plan.
2. Confirm the work has one bounded outcome.
3. Name anything intentionally left out.
4. Create a focused branch from `main`.

## Local Setup

```bash
make setup
source .venv/bin/activate
make check
```

Python 3.11 is the reference version used by CI. Phase 0 has no Node, FFmpeg, Docker, or model-download step.

## Pull Request Standard

Every PR should:

- explain the goal and why it belongs now;
- keep unrelated refactors out;
- add tests for behavior and failure cases;
- update documentation that the change makes stale;
- include exact reproduction or verification commands;
- call out assumptions, tradeoffs, and deferred work;
- keep generated media, model weights, caches, databases, secrets, and local config out of Git.

Use this description:

```markdown
## Goal

## Why

## Changes

## Acceptance criteria

## Verification

## Assumptions and tradeoffs

## Out of scope
```

## Code Placement Rules

- Domain behavior belongs in `src/serenity`, not the API or web UI.
- Pipeline code depends on ports, never concrete model adapters.
- Model quirks, prompt templates, sampling, and error translation stay in adapters.
- Timeline changes require validation tests and migration thinking.
- Public publishing code stays in `commons` and cannot be imported by the local core.
- Comments should explain constraints and reasoning; do not narrate obvious syntax.

## Definition Of Done

Run the full gate before requesting review:

```bash
make check
```

Also execute the manual verification steps named by the relevant roadmap PR. Large model and audio checks will be added only when those systems exist.
