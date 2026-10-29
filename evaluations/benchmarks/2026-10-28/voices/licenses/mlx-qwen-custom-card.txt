---
license: apache-2.0
library_name: mlx-audio
tags:
- mlx
- text-to-speech
- speech
- speech generation
- voice cloning
- tts
- mlx-audio
base_model:
- Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice
---
# mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit

This model was converted to MLX format from [`Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice`](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice) using mlx-audio version **0.3.0**.

Refer to the [original model card](https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice) for more details on the model.

## Use with mlx-audio

```bash
pip install -U mlx-audio
```

### CLI Example:
```bash
python -m mlx_audio.tts.generate --model mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit --text "Hello, this is a test."
```

### Python Example:
```python        
from mlx_audio.tts.utils import load_model
from mlx_audio.tts.generate import generate_audio

model = load_model("mlx-community/Qwen3-TTS-12Hz-1.7B-CustomVoice-8bit")
generate_audio(
    model=model,
    text="Hello, this is a test.",
    ref_audio="path_to_audio.wav",
    file_prefix="test_audio",
)

```