# Configuration

Whoopy uses one configuration path for the CLI, worker, and future API. Keep machine-specific values in `local.yaml` or environment variables; both `.env` and `local.yaml` are ignored by Git. Phase 0 does not automatically load `.env`, so source one through a shell or process manager when needed.

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
- `models.yaml` is the future adapter registry. A model choice is metadata and configuration, not pipeline logic.
- `pacing_profiles.yaml` contains product pacing presets.
- `runtime_profiles.yaml` maps live RAM and disk safety margins to Basic, Lite, Standard, High, and Studio capabilities.
- `prompts/` holds versioned system prompts once model-backed generation is added.

Validate the resolved settings with:

```bash
whoopy config show
whoopy doctor
whoopy models doctor
```

`models.yaml` remains the adapter-registry skeleton. `artifacts.yaml` and
`runtime_profiles.yaml` are active: the former answers “which immutable files?”
and the latter answers “can this laptop safely use them?” Neither doctor
command imports a runtime or loads a model.
