# Experimental local MLX Qwen generation

The two explicit CLI backends `mlx-qwen3-custom-voice-1.7b-8bit` and
`mlx-qwen3-voice-design-1.7b-8bit` use the normal durable generation pipeline.
They are experimental local installations, not managed model-pack READY states.
The local API also exposes them as explicit Lab-only experimental choices when
the pinned installation verifies on macOS ARM64 with at least 16 GiB available
RAM. `ready` means that local preflight passed; `managed_ready` and
`studio_eligible` remain false, and no listening approval is implied. Status
snapshots are cached briefly; every new launch rechecks the installation and
resources. Native Qwen, MOSS, and portable Kokoro keep their existing paths.

API requests use `surface: "lab"`, the backend ID, `qwen_language: "English"`,
and either `qwen_speaker` (Ryan, Aiden, or Serena) for CustomVoice or
`qwen_voice_design_preset` for VoiceDesign. VoiceDesign presets are
`warm_meditation`, `grounded_meditation`, and `sleep_meditation`. Both consume
reviewed `delivery`, `intensity`, and `pace` controls. Numeric `speed`, reference
recordings, and free-form voice instructions are unsupported. For prompt
generation, select `writing_method: "recipe_whole_session"` and an explicit
writer profile; this interface does not silently substitute a model or backend.

`config/experimental_tts.yaml` pins the currently installed machine-local MLX
runtime, interpreter, every installed distribution's actual contents, and all
checkpoint files. The resolver performs read-only verification. Any change
fails closed; there is no install, download, arbitrary executable option, or
cloud/native fallback. Refreshing these pins is a reviewed source change. The
manifest is not a portable runtime bundle or redistribution clearance.

The worker rejects unsupported hosts before any model/runtime import; this local
backend requires macOS ARM64. Portable protocol and helper tests use explicit
fakes and run without MLX/NumPy.

A worker runs in isolated Python with a fresh bytecode cache prefix, imports
MLX only in that process, uses local checkpoints with offline flags, and holds
`/tmp/whoopy-benchmark-gpu.lock` for its lifetime. It requires 16 GiB available
RAM and sets an allocator guideline of `min(24 GiB, available RAM - 6 GiB)`.
MLX can exceed this guideline; it does not guarantee that headroom remains free.
The worker exits on
sustained available RAM below 2 GiB or swap growth above 4 GiB. Startup and
request deadlines are bounded. Run writer, voice, and ASR jobs sequentially;
do not hold this same advisory lock around the whole CLI invocation because
the speech child must acquire it. The writer stage does not acquire that lock. Each llama.cpp invocation exits
before returning; the HIGH writer model is not resident when MLX admission runs.

New native attempt sidecars include `memory_measurements` version 1: MLX allocator
active/cache/cumulative peak bytes and system available/swap bytes at `before_load`,
`after_load`, `before_generation`, and each `after_generation_and_decode` boundary.
The final observation follows the worker's existing audio evaluation. No extra
evaluation, synchronization, cache clearing, or peak reset is performed. Allocator
counters are not process RSS, and peaks are cumulative across the worker's lifetime,
including earlier segments. System snapshots are not continuous minima or maxima.
The pinned nonstreaming API yields already decoded audio, so separate code-generation
and decoder peaks are explicitly unavailable. Missing, unsupported, or malformed
counters are `null` with an `unavailable_reasons` entry; unavailable optional metrics
do not make otherwise valid audio fail. The sidecar hash binds these observations
to that immutable attempt. Earlier sidecars are not backfilled or reinterpreted;
existing worker-hash drift checks remain authoritative for reuse and resume.

Historical memory evidence is distinct from these new, not yet live-measured
observations. The October 28 raw voice reports measured 7.707 GiB CustomVoice and
7.491 GiB VoiceDesign allocator peaks despite roughly 3.25 GiB RSS and 2.864 GiB
of model/codec weight files. The earlier VoiceDesign run exceeded its 6.386 GiB
allocator guideline. October 29 fixed-script voice runs showed system available-RAM
drops of about 7.90–11.85 GiB with no observed swap growth. These examples establish
neither a lower safe admission threshold nor readiness promotion. The 16 GiB guard
remains unchanged. See `evaluations/benchmarks/2026-10-28/voices/` raw reports and
the October 29 campaign `generation/resources.jsonl` files for the original evidence.

For whole-session live work use the qualified HIGH Ministral profile; Standard
Qwen4B is not a qualified whole-session baseline. From a synchronized checkout:

```sh
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=src .venv/bin/python -m whoopy generate \
  'A calm grounding meditation after a busy day.' \
  --writing-method recipe_whole_session --minutes 3 --profile high \
  --tts-model mlx-qwen3-custom-voice-1.7b-8bit --qwen-speaker Ryan \
  --qwen-language English --delivery calm --pace slow --seed 42 \
  --models-dir /Users/jakob/codestuff/Whoopy/models/managed \
  --runs-dir /Users/jakob/codestuff/Whoopy/evaluations/benchmarks/2026-10-29/e2e/runs \
  --audience lab --json
```

CustomVoice accepts Ryan, Aiden, or Serena. Always choose the intended speaker
explicitly (`--qwen-speaker` is shared with native Qwen and retains its existing
Serena default). Deep male VoiceDesign uses
`--tts-model mlx-qwen3-voice-design-1.7b-8bit --qwen-voice-design-preset grounded_meditation`.
No reference recording is involved. A fixed-script comparison can use the saved
`script.md` with `generate --script-file ... --requested-duration-seconds 180`.
The prompt-to-audio and fixed-script cases must be labeled separately.

The run contains the usual script, plan, timeline, model metadata, execution
telemetry, narration, manifest, and checkpoints. Its durable TTS configuration
also records experimental status, exact installation, model/runtime, adapter,
and worker hashes. Resume refuses drift. Experimental speech caches are
run-local so a copied global PCM hit cannot lose its native provenance.

Every inference attempt retains untouched native mono 24 kHz FLOAT audio at
`native-speech/<canonical-native-key>-<attempt-uuid>/native.wav`, with immutable
`metadata.json` recording text, typed segment ID, seed, instruction, effective
generation arguments, elapsed inference, codec EOS/token evidence, and hashes.
The worker records both the observed codec EOS and completion validity. The
pinned generator checks EOS before appending a code, so EOS on its final allowed
iteration is accepted (code count is then budget minus one). Missing locals,
contradictory EOS/count evidence, and exhausted budgets fail closed.
Failed EOS attempts remain available but are not accepted as speech. Partial
folders from interrupted writes also remain historical evidence and are never
selected without a complete, verified receipt.
The immutable sidecar includes `pcm_s16le_sha256` and the exact float32 clip,
scale, ties-to-even conversion policy. The parent independently parses the
native FLOAT WAV and reproduces that conversion before accepting returned PCM;
it does not trust the worker-supplied hash alone.
`native-speech/cache-references/<processed-cache-key>.json` (schema 2) identifies
the selected native attempt, native input key, full typed segment, processing
settings, and processed PCM hash. Inner/native and processed cache keys are
intentionally different. Reuse recomputes both keys and deterministically
processes the verified native samples to confirm the cached PCM hash. Use this mapping, rather than
all attempt folders, to identify delivered speech. Both cache and checkpoint
reuse verify that the selected native WAV and sidecar still match their hashes.
Original native samples and processed narration are distinct artifacts.

Use `scripts/verify_benchmark_audio.py` after generation exits. Expected text
must be extracted from final typed SPEECH segments, never supplied as an ASR
prompt. Verify both selected native segments and final narration with the
existing local Whisper snapshot. Any normalized word edit needs review; missing
content, repetition, truncation, failed EOS, or invalid audio is not an accepted
result. A render succeeding is not a speech-content or listening-quality pass.

Qwen model declarations are Apache-2.0; MLX-Audio code is MIT. Runtime dependency
licenses, model terms, and output/reference rights remain separate. This feature
does not choose a Whoopy source license or clear runtime redistribution.
