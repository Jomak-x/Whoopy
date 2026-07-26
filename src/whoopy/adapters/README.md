# Adapters

Concrete LLM, TTS, ambience, renderer, storage, and publishing integrations belong here. The universal baseline will use llama.cpp/GGUF for text and sherpa-onnx/Kokoro for speech. MLX and other accelerators remain optional adapters. Model-specific prompts and behavior must not leak into the pipeline.
