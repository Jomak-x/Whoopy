# Adapters

Concrete integrations implement the stable contracts from `whoopy.ports`.

- `llm/llama_cpp.py` invokes the verified native CLI in a bounded subprocess.
- `tts/sherpa_onnx.py` lazily loads the verified Kokoro model through
  sherpa-onnx.

Neither module loads a model during import or adapter listing. Model-specific
prompts, subprocess flags, voice controls, and runtime errors stay inside these
implementations. MLX and future alternatives remain optional adapters behind
the same ports.
