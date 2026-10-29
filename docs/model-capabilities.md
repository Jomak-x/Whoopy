# Local Model Capabilities

This document distinguishes an implemented adapter contract from a model that is selectable and
proven on the current machine. Treat `whoopy models doctor`, `whoopy models pack list` and the
versioned local model-pack and speech-model APIs as authoritative readiness evidence for their
respective artifact paths. The retained static frontend displays only a limited `/api/status`
summary; it does not expose the full model-management or Lab controls.

## Readiness Vocabulary

| State                       | Meaning                                                                        |
| --------------------------- | ------------------------------------------------------------------------------ |
| Missing / partial / corrupt | Required pinned bytes are absent or fail verification.                         |
| Installed                   | Checkpoint files verify; a runtime and/or current playable-output proof is still missing. |
| Resource blocked            | Live RAM, disk, or required acceleration is insufficient.                      |
| Incompatible                | The reviewed pack does not declare this target.                                |
| Ready                       | This revision and runtime produced real, valid, offline audio on this machine, and the exact hash-matched playable sample is still present. |

`ready` is machine-, model-, runtime-, and fingerprint-specific. The registry also reopens the
retained mono PCM16 WAV and checks its sample rate, duration, and PCM hash against the smoke
evidence. Copying a smoke JSON file, deleting or replacing its audio, or keeping evidence after
runtime code changes cannot make a model ready.

The backend distinguishes a **Studio candidate** from a **Studio-selectable model**. Fish and MOSS
are reviewed production candidates, but they become selectable only while their current state is
`ready`. Missing, installed-but-untested, resource-blocked, incompatible and failed-smoke states
remain inspectable through `whoopy models pack list` and `/api/v1/model-packs`; they do not become
selectable generation inputs. Qwen3-TTS remains Lab-only by default, and a local development
declaration cannot promote it into Studio.

Runtime bundles have their own stricter lifecycle. `whoopy models runtime list` reports whether a
target-specific, checksum-reviewed isolated environment exists. A Git checkout or copied
development virtualenv is never promoted. An installable bundle must declare an exact upstream
revision, archive byte size, SHA-256, target, required markers, and full installed-tree digest.
Interrupted transfers remain resumable; unsafe archive paths, links, digest failures, and modified
installed files are rejected.

Published Python bundles use the `locked-wheelhouse` route: an exact tracked Git snapshot and fully
hashed target wheelhouse are verified first, then the installer creates a new venv offline on the
destination machine. The release audit and builder are documented in
[Optional Runtime Bundle Release Process](runtime-bundle-release.md). Fish has a published clean
macOS ARM64 bundle and still requires a current-machine real smoke after installation. Qwen's first
source and 0.6B CustomVoice model revisions plus its macOS ARM64 Python 3.12.12 runtime are pinned
and published; checkpoint acquisition and current-machine production audio remain separate gates.

## Voice Model Capabilities

| Model                    | Managed targets                               | Voice modes                  | Native controls                                                                                                                                   | Important limits                                                                                                                     |
| ------------------------ | --------------------------------------------- | ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------ |
| Kokoro multilingual v1.0 | Windows x64; macOS ARM64/x64; Linux ARM64/x64 | Preset voices                | Voice ID; English/Chinese; speed-derived natural through very-slow pace                                                                           | Portable speech path and configured default. Unsupported delivery/intensity intent is visibly normalized to neutral/subtle in the effective timeline; human listening and promotion remain separate. |
| Fish Speech 1.4          | macOS ARM64 in the pinned Local V1 pack       | Consented reference          | Reference audio and transcript                                                                                                                    | Experimental, heavyweight, and non-commercial under the managed pack declaration. It does not claim arbitrary delivery instructions. |
| MOSS Local 5B            | macOS ARM64; Linux x64                        | Reference or direct/designed | Eight delivery styles; subtle/moderate intensity translation; four pace intents; language; instruction; optional target duration; pronunciation data | The generation boundary derives a reviewed instruction from typed delivery. Target duration is translated to MOSS audio tokens (about 12.5 tokens/second); exact deliberate quiet remains separate Whoopy timeline events. Lab evaluation APIs may store a bounded instruction override as typed adapter metadata while holding it fixed for delivery comparisons. Requires MOSS Audio Tokenizer v2, substantial RAM/disk, and a real smoke test. |
| MOSS 8B                  | macOS ARM64; Linux x64                        | Reference or direct/designed | Same typed contract as MOSS 5B                                                                                                                    | Larger memory footprint; often resource-blocked. A static record is never enough.                                                    |

`moss-local-5b` and the catalog label “MOSS Local 5B” are established Whoopy
compatibility identifiers. The pinned upstream Local Transformer v1.5 checkpoint's
language backbone configuration matches Qwen3-4B. The retained `5B` identifier is
therefore not an exact language-backbone parameter claim; changing it would break
existing manifests, caches, run records, and evaluation evidence.

Every MOSS speech request also uses the versioned `moss-text-pace-v1` duration
budget. An explicit `target_duration_ms` becomes
`ceil(milliseconds × 12.5 / 1000)` audio tokens. Without one, Whoopy counts
NFKC-normalized non-whitespace characters, applies the upstream MOSS estimate of
`0.8673376262755219` tokens per character (`3.098411951313033` for CJK-script
text), then applies the reviewed typed-pace multiplier: natural `1.15`, unhurried
`1.30`, slow `1.50`, or very slow `1.80`. The result is rounded up and clamped to
19–1,500 duration tokens. The hard generation ceiling is
`min(1536, max(96, ceil(duration_tokens × 1.25) + 64))`. Both policy versions are
part of adapter metadata and cache identity; explicit target duration is also a
canonical per-segment cache input.

The backend marks every model that is not currently ready as non-selectable for Studio generation.
The model-pack CLI and API return the exact install, runtime, resource or re-test remedy.
Experimental and incompatible evidence remains available through the versioned Lab and model APIs.

Kokoro's shipped preset choices are `af_heart`, `af_bella`, `af_nicole`, and `am_michael`.

## Native/Managed Qwen3-TTS Adapter Family

Whoopy implements isolated adapter contracts for:

- 0.6B and 1.7B CustomVoice: preset speaker, language, delivery instruction;
- 1.7B VoiceDesign: reviewed warm, grounded, or sleep voice descriptions;
- 0.6B and 1.7B Base: consented reference audio plus matching transcript.

The model registry now contains only the first reviewed integration:
`Qwen3-TTS-12Hz-0.6B-CustomVoice` for macOS ARM64, pinned at model revision
`85e237c12c027371202489a0ec509ded67b5e4b5` and source revision
`022e286b98fbec7e1e916cb940cdf532cd9f488e`. Its checksum-reviewed runtime clean-installs offline
and passes its full import probe, but it remains **Lab-only and unavailable for Studio generation**
until the checkpoint and current-machine real offline audio smoke exist. The other native adapter
variants remain unregistered and therefore fail closed. Hand-authored model declarations and copied
development environments cannot enable generation. Versioned Lab APIs may preserve Qwen evidence
and human ratings; the generation boundary still rejects Studio use until a current saved decision
matches the exact ready voice contract. Promotion never bypasses model readiness.

Separately pinned experimental MLX CustomVoice and VoiceDesign variants have explicit
[CLI-only routes](experimental-mlx-qwen.md); they do not confer managed readiness.

CustomVoice and VoiceDesign translate all eight reviewed delivery styles and four pace intents.
Base cloning declares neutral/natural delivery because cloning a voice does not prove instruction
following. Qwen voice cloning requires the explicit scope
`local_voice_cloning_experiment_only`.

## Local Language Models

| Model                                  | Role                          | Current boundary                                                                                                           |
| -------------------------------------- | ----------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| Ministral 3 14B GGUF through llama.cpp | Explicit High writing profile | Pinned and measured on the M4 Pro. Daytime whole-session evidence exists; sleep reliability and human quality are unqualified. |
| Qwen3-4B GGUF through llama.cpp        | Standard local writing profile | Available when installed and hardware-safe. Its current `recipe_whole_session` pilot failed, so that method is unqualified on this writer. |
| Qwen3-1.7B GGUF through llama.cpp      | Lite experiment               | Did not qualify as an automatic dependable prompt fallback; must be requested explicitly for experiments.                 |
| Other larger model candidates          | Future research               | Not a supported profile until pinned, measured and documented through saved evidence.                                      |

Authored-script Basic mode has no LLM dependency.
Writing qualification and dated results belong to the
[local quality baseline](local-quality-baseline.md), not to model installation state.

## Typed Delivery Contract

Speech text and controls are separate in `timeline.json`:

- delivery: `neutral`, `warm`, `calm`, `soothing`, `grounded`, `compassionate`, `sleepy`, or
  `encouraging`;
- intensity: `subtle` or `moderate`;
- pace: `natural`, `unhurried`, `slow`, or `very_slow`;
- voice mode: `preset`, `reference`, or `designed`;
- language and optional pronunciation overrides.

Authoring controls must appear alone on a line:

```markdown
[delivery: compassionate]
[intensity: subtle]
[pace: unhurried]
[voice-mode: preset]
May you meet this moment with a little more kindness.

[pause: 6s]
```

The compiler removes controls from spoken text. Unsupported values and unknown bracket tags fail.
An adapter either translates a supported intent or exposes its narrower capability. Before
synthesis, Whoopy normalizes unsupported voice mode, delivery, intensity, pace, language, and
pronunciation controls to the adapter's declared fallback and stores those effective values in the
timeline. The requested global override remains in `resolved-config.json`, so the fallback is
inspectable rather than silently ignored. Tags are never spoken.

Exact meditation pauses remain `[pause: ...]`, `SILENCE`, or reviewed `BREATH` events. They are not
model-native pause markup.

## Voice References And Consent

The versioned local API supports PCM16 WAV import with exact transcript and rights consent, trim
start/end controls, preview, immutable replacement revisions, active revision evidence and
recoverable deletion/restore. A client may convert a microphone recording locally before import,
but the retained static frontend does not expose microphone or voice-reference management. Every
import or replacement requires the exact attestation:

> I own this voice or have permission to clone it.

Only import your own voice or a voice for which you have clear permission. A technically supported
clone is not automatically lawful or appropriate to distribute. Importing a reference does not
make Qwen Base selectable without its pinned runtime/model and current-machine smoke evidence. See
[privacy](privacy.md).

## Licenses

Licenses are saved in model manifests and run metadata. Current key declarations include
Apache-2.0 for Kokoro, MOSS, Qwen LLM, and Qwen3-TTS entries. The managed Fish Speech 1.4 pack is
declared CC-BY-NC-SA-4.0 and non-commercial. Review `config/model_packs.yaml`, `config/models.yaml`,
and the generated run's `model-metadata.json` before commercial use or redistribution; where old
registries disagree, use the exact managed pack and saved run evidence, and resolve the discrepancy
before publishing.

Kokoro's Apache model declaration does not clear its complete runtime bundle.
The inspected Sherpa and MLX paths contain eSpeak/GPL dependencies; exact wheel
source closure and full-bundle redistribution review remain incomplete. See the
[Kokoro runtime audit](kokoro-license-audit.md) for component evidence and the
distinction between commercial use, network use, and redistribution.
