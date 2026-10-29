# Adapters

Concrete integrations implement the stable contracts from `whoopy.ports`.

- `llm/llama_cpp.py` invokes the verified native CLI in a bounded subprocess.
- `tts/sherpa_onnx.py` lazily loads the verified Kokoro model through
  sherpa-onnx;
- `tts/fish_speech.py`, `tts/moss_tts.py`, and `tts/qwen3_tts.py` keep their
  optional model runtimes behind the same speech contract.

These modules do not load a model during import or adapter listing. Model-specific
prompts, subprocess flags, voice controls, and runtime errors stay inside these
implementations. An implemented adapter is not a readiness claim: optional
runtimes remain unavailable until their pinned files, platform support, and
production smoke evidence verify on the current machine.
