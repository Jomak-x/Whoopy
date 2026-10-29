# Whoopy Local V1 User Guide

## Start And Stop

From the Whoopy directory:

```bash
uv run whoopy app
```

The browser opens the private Studio at `http://127.0.0.1:8765`. Stop Whoopy with Ctrl+C in the
launching terminal after active work has completed or been cancelled. The next launch reconciles an
expired worker lease rather than discarding checkpoints.

## First-Run Model Setup

The **Current machine** card lists production voice paths. A model is selectable only when it says
**Playable**:

- **Install** acquires and verifies declared files;
- **Test** performs real production synthesis and saves current-machine evidence;
- **Verify** rechecks a ready model's files/evidence;
- **Free memory** or **Incompatible** explains why the action is disabled.

Start with Kokoro. Optional Fish/MOSS voices need more disk/RAM and may not support your platform.
Open **Expressive-model runtimes** in the same card to inspect the separate Fish, MOSS, and Qwen
runtime state. When a checksum-reviewed bundle exists for this computer, **Install** or **Resume**
runs its pinned installation in the shared model-operation slot and **Verify** rechecks its complete
installed-tree digest. An **Unavailable** runtime is deliberately disabled and explains which
reviewed release is missing; Studio never offers a copied development environment as a shortcut.

## Create From An Idea

1. Select **From an idea**.
2. Describe the intended outcome and context without entering sensitive information you do not want
   saved locally.
3. Choose 1–20 minutes.
4. Choose one reviewed practice: grounding, focused attention, breath awareness, body scan, noting,
   reflection, loving kindness, self compassion, visualization, open awareness, or sleep.
5. Choose a reviewed meditation-pacing profile, then a Playable voice model, voice where available,
   and delivery. The profile controls both narration speed and its reviewed silence policy; its
   version and checksum are saved so a later profile edit cannot silently change a resumed run.
6. Select **Create meditation**.

Whoopy creates a versioned technique plan, drafts sparse narration around deterministic practice
space, validates content, compiles the timeline, synthesizes per segment, assembles exact quiet, and
runs quality checks. Daytime practices reorient; sleep practices taper without a daytime ending.

The LLM cannot invent breath holds, ratios, pause lengths, or unsupported exercise types.

## Create From A Script

1. Select **From my script**.
2. Paste plain text or Markdown.
3. Add exact standalone controls when useful:

```markdown
[delivery: warm]
[pace: unhurried]
Take a moment to notice where the body meets the chair.

[pause: 5s]

[delivery: compassionate]
Nothing needs to be forced here.
```

4. Select **Test this voice** for the same short production pipeline used by full generation.
5. Select **Create meditation**.

Basic script mode needs no LLM. Tags are typed controls and are removed from speech. Use only the
values described in [model capabilities](model-capabilities.md).

## Monitor, Cancel, And Resume

The progress message comes from the durable run stage rather than a cosmetic timer. It can report
planning, drafting, compiling, waiting for the model slot, model startup, synthesis, assembly, or
quality checking.

**Cancel** asks the worker to stop safely and preserves verified work. An interrupted or failed
library card offers **Resume**. Resume validates the timeline, cache, and checkpoints before reuse.
Errors remain on the run and in bounded logs for diagnosis.

## Use The Local Library

The library is rebuilt from authoritative run directories. Search by title/model/status text and
filter completed, interrupted, or failed work. Studio shows only runs whose durable audience is
`studio`; Lab trials and automated acceptance runs stay with their owning surfaces instead of
cluttering **Your meditations**. Their run directories remain inspectable and recoverable. Open a
meditation to access:

- **Plan:** selected technique and pacing/breath/ending intent when generated from a prompt;
- **Script:** editable narration and controls;
- **Timeline:** typed speech/quiet/breath events and visual proportions;
- **Delivery:** per-speech delivery selection;
- **Listen:** narration master, revisions, and finished exports.

The SQLite catalog makes search fast but does not replace run files.

## Edit And Restore

In **Script**, edit the text and select **Save & render**. In **Delivery**, change one speech block's
delivery. Each accepted edit creates an immutable revision before rerendering.

Whoopy reuses healthy work where identities still match:

- quiet-only changes reassemble without TTS;
- delivery or speech changes rerun affected TTS segments and downstream assembly;
- prior revisions remain available in the Listen view.

Choose a revision from **Restore revision** to reactivate and render it. Validation occurs before an
edit or restore can replace active artifacts.

The Timeline view exposes exact silence controls, reviewed breathing protocols, segment/section
audition, and alternative-take management. The Delivery view exposes typed style, intensity, pace,
and supported pronunciation controls. The Plan view allows only semantic edits permitted by the
active reviewed blueprint.

## Listen And Export

The Listen player streams `narration.wav`. **Export WAV** downloads that narration master directly.
The PCM-derived waveform distinguishes speech, exact silence, and reviewed breath cycles; overlays
show section boundaries, and the Timeline identifies whether each speech block is the original
production take, has alternatives, or currently uses a regenerated take.

Select **Create all exports** for a verified set:

- finished WAV with PCM audio;
- FLAC;
- AAC in an M4A container;
- Opus;
- `export-manifest.json` containing hashes, codec/decode/duration evidence, metadata, and FFmpeg
  version.

After creation, each format appears as a download button. Creating exports again atomically replaces
the complete previous set only after every new file passes validation.

## Remove A Meditation

Select **Move to trash** and confirm. Only stopped runs can be moved. The run and all revisions move
to recoverable local trash rather than being immediately erased. Expand **Local data and settings**,
find **Recently deleted meditations**, and select **Restore** to return an intact run to the library.
Every deletion has a durable receipt tied to its run ID. Whoopy refuses a missing, altered,
duplicated, or symlinked receipt entry instead of restoring ambiguous data.

## Settings And Local Paths

Expand **Local data and settings** to see the exact run, model, Lab-evidence, and catalog locations.
**Export settings** downloads the versioned catalog-settings envelope. **Import settings** validates
and atomically replaces those settings. It does not import model weights, runs, Lab evidence, or
audio. Experiment definitions, reports, ratings, and decisions remain authoritative JSON; the
version-keyed SQLite rows are rebuilt projections and may be discarded safely.

The same section contains **Consented voice references**. To add one:

1. enter a display name;
2. enter the exact words spoken in the recording;
3. select an existing PCM16 WAV file or record up to 60 seconds with the microphone;
4. optionally set trim start/end times and preview the edited source;
5. confirm that you own the voice or have permission to clone it for local experiments;
6. select **Import reference**.

Studio validates and stores a managed revision, then lists its duration and local audio preview.
**Replace** creates a new immutable revision of an existing reference; **Delete** moves it to
recoverable managed trash. Browser microphone recordings are converted locally to PCM16 WAV before
upload. Importing or recording a reference does not make Qwen cloning ready by itself; Qwen still
requires its separately pinned runtime/model and real smoke evidence.

## Evaluation Lab

Switch to **Lab** to see the controlled experiment types and honest candidate readiness. Lab is for
changing one variable at a time and recording evidence, not normal meditation creation.

Choose LLM, writing method, voice, delivery, or pacing; select one reviewed case; enter a baseline and
challenger; then select **Start with one case**. After reviewing that trial, **Run remaining** queues
the other cases in a new experiment revision, while **Run all** reruns the complete evaluation set.
The default local production executor queues experiments sequentially and sends each trial through
ordinary durable generation. The evidence inspector provides blind review, playable audio,
waveforms, automatic metrics, and verified production artifacts.

An active or queued batch can be **Paused** or **Cancelled** from its evidence card. Both actions
preserve completed trial artifacts. If a production run is active, Whoopy requests its ordinary
durable cancellation so the run becomes interrupted and resumable; it does not discard healthy
segments. **Resume** continues the saved case selection and interrupted run. After an application
restart, a previously queued or running batch is shown as paused and never starts until you choose
Resume. **Incomplete** means execution stopped because of an error and may also be resumed after
the cause is fixed; **cancelled** records an intentional stop and does not delete the definition.

The underlying Lab also supports blind A/B presentation, human ratings, baseline comparison, and
saved promotion decisions through its versioned API. LLM promotions must name their product role:
**Portable default**, **Low-resource fallback**, or optional **Higher quality**. Local V1 requires
separate, evidence-backed portable and fallback candidates; a historical role-less decision remains
readable but cannot satisfy release acceptance. A fallback assignment is stored separately and does
not overwrite the portable Studio default. Not every arbitrary candidate string is a production
capability. Unsupported LLMs, writing methods, voices, or pacing profiles are recorded as failed
trials; they are never replaced with static output.

For failed or interrupted prompt generation, open the saved meditation. The collapsible **Rejected
generation attempts** section shows bounded local copies of raw drafts and their validation reasons.
These diagnostics are read only from the run's managed generation workspace and are never accepted
as the finished script.

An accepted pacing decision is a real production change: Studio selects the same reviewed profile,
passes its speech speed into drafting and synthesis, and applies its allowlisted deterministic
silence strategy to the canonical timeline. Promotion does not collapse the decision into a display
label or an unrelated speed preference.

### Finish the Local V1 human gate

The Lab shows a live **Local V1 human gate** for the selected ten-case evaluation-set version. It is
computed from immutable report and rating files, so refreshing or restarting the app does not lose
progress. Complete the gate without the CLI as follows:

1. Start with one LLM or writing-method case and confirm the comparison is worth expanding.
2. Select **Run remaining**. Review the resulting revision containing the remaining cases as well as
   the original one-case revision; acceptance combines their version-bound evidence.
3. For every case, listen to A and B without inspecting candidate artifacts. Save both a **content**
   and **voice** score from 1–5. The content judgment must also record A, B, or tie as the preferred
   result. The score describes the quality of the preferred sample, or the shared quality when tied.
4. Watch the gate reach 10/10 content ratings, 10/10 voice ratings, and 10/10 blind baseline
   preferences. Passing also requires both medians to be at least 4, every required score to be at
   least 3, and more challenger than baseline content preferences.
5. Candidate identities become inspectable only after every required rating for the current report
   is saved. Promotion remains unavailable until one experiment revision itself covers the entire
   versioned evaluation set; use **Run all** when you want a single promotable decision record.
6. Record distinct accepted LLM decisions for **Portable default** and **Low-resource fallback**.
   Then record accepted writing-method, voice, and pacing decisions. The decision checklist shows
   what is saved. Optional higher-quality and delivery decisions do not block Local V1.

A one-case trial can be rejected, but cannot become a product default. Ratings are immutable per
blind pair and dimension; if you need to repeat a judgment, run a new experiment revision rather
than silently replacing evidence.

Voice comparisons keep typed delivery, intensity, language, and voice mode fixed while changing a
model, preset, or consented reference. Delivery comparisons instead freeze the model, voice
identity, mode, language, LLM, writing method, text, exact silence, and pacing. Enter delivery
candidates as `style|intensity`, such as `calm|subtle` and `sleepy|moderate`; only expressive MOSS,
Qwen CustomVoice, and Qwen VoiceDesign adapters are accepted for this axis.

## Breathing And Safety

Whoopy supports natural breath awareness and reviewed gentle inhale/exhale protocols without holds.
Timed breathing includes opt-out and dizziness guidance in its reviewed content contract. Do not
use generated meditation as medical care, emergency help, or a substitute for a qualified
professional. Stop any practice that causes discomfort.

## Keyboard And Accessibility

Controls use native buttons, labels, selects, textareas, audio controls, and a dialog structure.
Progress/error regions announce updates, and the design respects reduced-motion preferences. Use
normal Tab/Shift+Tab navigation and Enter/Space activation. If focus becomes trapped after a browser
or extension issue, close the run dialog and reopen it.

## Where To Go Next

- [Setup and offline use](setup.md)
- [Troubleshooting](troubleshooting.md)
- [Model capabilities](model-capabilities.md)
- [Privacy and local data](privacy.md)
