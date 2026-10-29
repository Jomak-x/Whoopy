# Whoopy Local V1 User Guide

## Start And Stop

From a source checkout:

```bash
uv run whoopy app
```

The browser opens the private Studio at `http://127.0.0.1:8765`. Stop Whoopy with Ctrl+C in the
launching terminal after active work has completed or been cancelled. The next launch reconciles an
expired worker lease rather than discarding verified checkpoints.

The retained Studio is intentionally small. It creates prompt or authored-script meditations,
selects an available speech model and its basic voice/pace controls, reports and cancels the active
task, lists recent meditations, plays finished narration, and displays saved Script,
Plan, Checks, and Timeline artifacts. Backend revision, take, export, deletion, settings,
voice-reference, and evaluation APIs are not visible controls in this frontend.

## First-Run Model Setup

Follow [Start locally](../README.md#start-locally) to acquire and prove Kokoro before
opening Studio.

The **Laptop readiness** card reports whether the Basic/Standard paths and speech models are ready;
it does not install, repair, or verify them. Use the `whoopy models pack` and `whoopy models runtime`
commands described in [setup](setup.md) for those operations. Optional Fish, MOSS, and Qwen paths
need separately pinned runtimes/models and may be unavailable on the current platform.

## Create From An Idea

The retained Studio's **Describe it** mode accepts a request, a 1–10 minute duration, and the visible
speech-model controls. It uses the established prompt-generation defaults. The new whole-session
recipe writer and its reviewed meditation-kind choice are explicit CLI options. Use the single
authoritative quickstart and qualification boundary in [README](../README.md); the browser does not
silently select that writer. A generated file still needs human listening.

## Create From A Script

Select **Use my script**, paste plain text or Markdown, choose the visible speech controls, and use
standalone controls where useful:

```markdown
[delivery: warm]
[pace: unhurried]
Take a moment to notice where the body meets the chair.

[pause: 5s]

[delivery: compassionate]
Nothing needs to be forced here.
```

**Test this voice** runs a short production sample. **Create meditation** runs the full script. Basic
script mode needs no LLM. Supported controls are removed from speech; unknown controls fail rather
than being spoken. See [model capabilities](model-capabilities.md) for accepted values.

## Monitor, Cancel, And Resume

Studio reports the current task and **Cancel** asks its worker to stop safely. Verified checkpoints
remain available. The retained library does not show a Resume action; inspect and resume a stopped
run from the CLI:

```bash
uv run whoopy run show <run-id>
uv run whoopy run resume <run-id>
```

Resume revalidates the timeline, cache, and checkpoints before reuse. Heavy model work, generation,
edits, restores, and alternative-take finalization share admission controls so a busy owner is
rejected instead of racing a second mutation.

## Listen And Inspect

**Recent meditations** lists saved Studio runs; runs with finished audio offer **Listen & inspect**.
That dialog plays `narration.wav` and shows the saved Script, Plan, Checks, and Timeline artifacts.
The run directory remains the authoritative record; the SQLite catalog is a rebuildable search
projection.

The backend also implements immutable revisions, alternative segment takes, verified finished
exports, recoverable run deletion, settings envelopes, consented voice references, and versioned
evaluation records. These contracts live under the versioned `/api/v1` loopback API and are used by
the checked-in acceptance tools, but the retained Studio frontend does not provide buttons or forms
for them. For the export request and artifact layout, see [setup](setup.md#7-finished-exports).

## Local Data And Privacy

Prompts, scripts, raw model attempts, timelines, audio, logs, model files, and evaluation evidence
remain in the configured local directories. Do not enter sensitive material you do not want saved
there. The retained Studio does not expose settings import/export, trash restore, microphone, or
voice-reference management. See [privacy and local data](privacy.md) for the authoritative data map,
loopback boundary, consent requirements, and cleanup implications.

## Breathing And Safety

Whoopy supports natural breath awareness and reviewed gentle inhale/exhale protocols without holds.
Timed breathing includes opt-out and dizziness guidance in its reviewed content contract. Do not use
generated meditation as medical care, emergency help, or a substitute for a qualified professional.
Stop any practice that causes discomfort.

## Keyboard And Accessibility

The retained Studio uses native buttons, labels, selects, textareas, audio controls, and a dialog.
Progress and error regions announce updates, and the design respects reduced-motion preferences.
Use normal Tab/Shift+Tab navigation and Enter/Space activation.

## Where To Go Next

- [Setup and offline use](setup.md)
- [Troubleshooting](troubleshooting.md)
- [Model capabilities](model-capabilities.md)
- [Privacy and local data](privacy.md)
