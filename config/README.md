# Configuration

Whoopy uses one configuration path for the CLI, worker, and local API. Keep machine-specific values in `local.yaml` or environment variables; both `.env` and `local.yaml` are ignored by Git and excluded from built wheels. Whoopy does not automatically load `.env`, so source one through a shell or process manager when needed.

## Precedence

Settings are resolved from lowest to highest priority:

1. `default.yaml`
2. optional `local.yaml`
3. `WHOOPY_*` environment variables
4. CLI flags

Nested environment variables use two underscores. For example, `WHOOPY_TTS__VOICE=af_heart` overrides `tts.voice`.

## Files

- `default.yaml` contains safe, versioned application defaults.
- `artifacts.yaml` locks exact model/runtime files, byte sizes, SHA-256
  digests, licenses, platforms, and profile component sets.
- `models.yaml` is the provisional adapter registry. A model choice is metadata and configuration, not pipeline logic.
- `pacing_profiles.yaml` is the strict, versioned pacing-policy registry used by
  production and Lab evidence. It owns speech speed and the exact timeline
  mutation allowlist; breath timing remains in reviewed breath protocols.
- `runtime_profiles.yaml` maps live RAM and disk safety margins to Basic, Lite, Standard, High, and Studio capabilities.
- `runtime_packs.yaml` declares only published target archives with immutable size/SHA-256 and
  truthful unavailable reasons when no reviewed archive exists.
- `runtime_build_recipes.yaml` pins the source revision, Python patch, source/import markers,
  supported targets, excluded Git submodules, and reviewed per-target dependency lock paths used
  to build clean optional runtime releases.
- `prompts/` holds the versioned system prompts used by model-backed generation.

Validate the resolved settings with:

```bash
whoopy config show
whoopy doctor
whoopy models doctor
```

`artifacts.yaml` answers “which immutable files?” and `runtime_profiles.yaml`
answers “can this laptop safely use them?” Inspection commands do not import a
runtime or load a model; readiness requires the separate production smoke test.
