# Roadmap

This roadmap turns the spec into implementation phases. Each phase has a reason for existing, not just a deliverable list.

Implementation status:

- **Phase 0 — in progress:** foundation is implemented on its review branch.
- **Phases 1–6 — planned:** no product behavior is implemented yet.

## Phase 0: Documentation And Repo Foundation

Goal:

- turn the design spec into readable project docs
- create the repo layout and configuration skeleton

Why this phase exists:

- the repository began as only a spec
- the code should start from a stable map
- new contributors need an entry point before they need features

Done when:

- the docs explain the architecture clearly
- the directory structure exists
- the baseline config files exist
- the package installs and its foundation quality gate passes
- native hardware inspection chooses a safe profile or refuses before model loading
- the same locked quality gate passes on Windows, macOS, and Linux

Evidence:

- `whoopy --help` and `whoopy config show` run successfully
- `whoopy doctor` reports structured capabilities without loading a model
- `uv run python scripts/check.py` runs linting, formatting, strict typing, and tests
- configuration precedence is covered by automated tests

## Phase 1: Local Core Skeleton

Goal:

- build the smallest possible local generation flow
- connect prompt input to a saved run record

Why this phase exists:

- it proves the control plane and worker boundaries
- it makes the architecture tangible quickly
- it surfaces missing assumptions before the system grows

Done when:

- a prompt creates a run
- a worker processes that run
- the system writes a timeline artifact

## Phase 2: Deterministic Audio Assembly

Goal:

- make the canonical timeline produce a real audio file
- ensure pauses and joins are deterministic

Why this phase exists:

- the main technical risk is audio timing, not UI polish
- explicit silence and segment joins are the heart of the product

Done when:

- a short meditation renders end-to-end
- pause timing is stable
- basic audio quality checks pass

## Phase 3: Quality, Caching, And Recovery

Goal:

- add cached per-segment synthesis
- add retry and resume behavior
- add quality checks for output integrity

Why this phase exists:

- long jobs need partial recovery
- cached segments make iterative improvement practical
- quality gates prevent regressions that are hard to hear by inspection

Done when:

- a failed segment can be regenerated without starting over
- repeated renders reuse cached work
- tests catch timing or clipping regressions

## Phase 4: Local Product Polish

Goal:

- make the local UI pleasant and reliable
- improve run visibility and editing workflows

Why this phase exists:

- a good engine is not enough if the user cannot understand it
- the local experience should feel simple enough to trust

Done when:

- the UI can launch and monitor runs
- logs and outputs are easy to inspect
- the local workflow feels coherent

## Phase 5: Public Platform

Goal:

- add the optional sharing layer
- publish rendered meditations with proper metadata and license checks

Why this phase exists:

- sharing is valuable only after the local core is stable
- public publishing introduces moderation and storage concerns

Done when:

- published items can be browsed
- audio assets are stored and delivered correctly
- license checks are enforced before publication

## Phase 6: Ecosystem Hardening

Goal:

- stabilize the system for other contributors
- make the model and backend choices easy to swap
- document operational limits and troubleshooting

Why this phase exists:

- systems become easier to extend when the boundaries are explicit
- future model upgrades should not require a rewrite

Done when:

- adapter swaps are mostly configuration changes
- documentation matches the real build
- the setup process is predictable from a clean machine

## Practical Milestone Ordering

If you want to learn while building, do not jump ahead.

Use this order instead:

1. understand the architecture
2. create the repo skeleton
3. define the timeline schema
4. build the local pipeline
5. verify audio determinism
6. add recovery and tests
7. only then think about the public platform

That sequence keeps the hardest unknowns visible early.
