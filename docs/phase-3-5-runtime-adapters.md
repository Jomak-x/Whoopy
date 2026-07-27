# Phase 3.5 PR 2: Runtime Ports And Native Adapters

This PR makes the first real models usable from Python without coupling Whoopy's
pipeline to either model. It does not yet change the normal worker command;
PR 3 connects real Kokoro speech to a script-file flow.

## Port And Adapter In Plain Language

A **port** is a promise made by Whoopy:

> “Given this typed request, return this typed result or one of these known
> errors.”

An **adapter** translates that promise into the controls of one concrete tool.
The current implementations are:

```text
ScriptGenerator port
  -> LlamaCppScriptGenerator
      -> verified llama-cli subprocess
          -> verified Qwen GGUF

SpeechSynthesizer port
  -> SherpaOnnxKokoroAdapter
      -> lazily imported sherpa-onnx
          -> verified Kokoro model directory
```

The worker and future UI depend on the ports. They do not need branches such as
“if Qwen” or “if Kokoro.” A future model implements the same contract.

## Shared Metadata

Every adapter exposes `AdapterMetadata`:

- adapter identifier;
- exact model identifier and revision;
- runtime name and version;
- license identifier;
- selected device or execution provider; and
- every configured setting that affects behavior.

The metadata produces a deterministic `cache_identity`. For speech, changing
the Kokoro voice, speaker ID, speed, language, provider, thread count, runtime,
or model revision changes that identity and therefore misses the Phase 3 cache.

Per-request values such as an LLM seed and output-token limit remain in the
typed request. Later run artifacts save both the request and adapter metadata.

## Error Types

Adapters translate backend-specific exceptions into four public meanings:

| Error | Meaning | Pipeline behavior |
|---|---|---|
| `TransientAdapterError` | a timeout or temporary runtime failure | retry only within a bounded policy |
| `FatalAdapterError` | missing files, incompatible version, invalid setup, or deterministic process failure | stop immediately |
| `InvalidAdapterOutput` | the process completed but violated its output contract | reject the output |
| speech-specific subclasses | the same meanings for segment synthesis | use the existing Phase 3 retry/checkpoint flow |

Unexpected exceptions are not silently treated as retryable.

## llama.cpp Adapter

`LlamaCppScriptGenerator`:

1. resolves the exact runtime and model for a profile;
2. performs a full artifact and extracted-tree verification;
3. locates exactly one `llama-cli` executable;
4. writes the prompt and optional system prompt to private temporary files;
5. invokes llama.cpp with a list of arguments rather than a shell string;
6. forces offline, bounded, single-turn generation;
7. applies a timeout;
8. parses llama.cpp's transcript wrapper to return only assistant text; and
9. deletes the temporary directory automatically.

The prompt is not placed in process arguments. The adapter runs llama.cpp in a
child process, so a native crash does not directly corrupt the Python worker.

The model is not loaded when the module is imported or the adapter is listed.
It loads only when `generate()` starts the subprocess.

## sherpa-onnx Kokoro Adapter

`SherpaOnnxKokoroAdapter`:

1. resolves and fully verifies the Kokoro archive and both platform wheels;
2. checks that one complete model directory contains the model, voices, tokens,
   English lexicon, and eSpeak data;
3. records voice, speed, language, provider, and thread settings;
4. imports sherpa-onnx only on the first `synthesize()` call;
5. checks that the installed Python runtime is exactly version `1.13.4`;
6. constructs the Kokoro engine once and reuses it for later segments;
7. requests speaker ID `3` (`af_heart`) at speed `0.9`;
8. rejects missing, empty, non-finite, or non-24-kHz output; and
9. converts normalized floating-point samples to mono 16-bit little-endian PCM.

The resulting `PcmAudio` enters the same renderer, cache, checkpoint, and
quality paths already tested with fixture speech.

## Contract Tests

Reusable helpers assert that every script adapter:

- satisfies the runtime-checkable `ScriptGenerator` protocol;
- returns non-empty typed text;
- returns its exact metadata; and
- records non-negative elapsed time.

Every speech adapter must:

- satisfy the `SpeechSynthesizer` protocol;
- derive cache identity from metadata;
- return the declared sample rate;
- return audible, structurally valid PCM; and
- pass the existing PCM integrity check.

Normal CI uses fake process and sherpa implementations. It never downloads or
loads real weights. Separate local smoke tests proved the same production
classes against the verified Standard artifacts.

## Verified Development-Machine Results

The real smoke tests on the macOS arm64 development machine produced:

- Qwen3-4B through llama.cpp/Metal: `Welcome to the moment.`;
- a bounded llama.cpp process that exited after one response;
- Kokoro initialized only on first use;
- Kokoro output at 24,000 Hz with 41,161 frames for the test sentence; and
- no PCM integrity error.

These are integration proofs, not final quality-bake-off results.

## Deliberate Boundary

This PR does not:

- select adapters through the normal worker CLI;
- parse a meditation script into multiple sections;
- trim TTS edge silence;
- expose a `whoopy generate` command; or
- claim the provisional models are permanent defaults.

Those behaviors belong to PRs 3–6 so each review stays understandable.
