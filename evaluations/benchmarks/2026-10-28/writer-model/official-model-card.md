---
library_name: vllm
language:
- en
- fr
- es
- de
- it
- pt
- nl
- zh
- ja
- ko
- ar
license: apache-2.0
inference: false
base_model:
- mistralai/Ministral-3-14B-Instruct-2512
extra_gated_description: >-
  If you want to learn more about how we process your personal data, please read
  our <a href="https://mistral.ai/terms/">Privacy Policy</a>.
tags:
- mistral-common
---

# Ministral 3 14B Instruct 2512 GGUF

The largest model in the Ministral 3 family, **Ministral 3 14B** offers frontier capabilities and performance comparable to its larger [Mistral Small 3.2 24B](https://huggingface.co/mistralai/Mistral-Small-3.2-Instruct-2506) counterpart. A powerful and efficient language model with vision capabilities.

This model includes different quantization levels of the instruct post-trained version in **GGUF**, fine-tuned for instruction tasks, making it ideal for chat and instruction based use cases.

The Ministral 3 family is designed for edge deployment, capable of running on a wide range of hardware. Ministral 3 14B can even be deployed locally, capable of fitting in 24GB of VRAM in FP8, and less if further quantized.

Learn more in our [blog post](https://mistral.ai/news/mistral-3) and [paper](https://arxiv.org/abs/2601.08584).

## Key Features
Ministral 3 14B consists of two main architectural components:
- **13.5B Language Model**
- **0.4B Vision Encoder**

The Ministral 3 14B Instruct model offers the following capabilities:
- **Vision**: Enables the model to analyze images and provide insights based on visual content, in addition to text.
- **Multilingual**: Supports dozens of languages, including English, French, Spanish, German, Italian, Portuguese, Dutch, Chinese, Japanese, Korean, Arabic.
- **System Prompt**: Maintains strong adherence and support for system prompts.
- **Agentic**: Offers best-in-class agentic capabilities with native function calling and JSON outputting.
- **Edge-Optimized**: Delivers best-in-class performance at a small scale, deployable anywhere.
- **Apache 2.0 License**: Open-source license allowing usage and modification for both commercial and non-commercial purposes.
- **Large Context Window**: Supports a 256k context window.

### Recommended Settings

We recommend deploying with the following best practices:
- System Prompt: Define a clear environment and use case, including guidance on how to effectively leverage tools in agentic systems.
- Sampling Parameters: Use a **temperature below 0.1** for daily-driver and production environments ; Higher temperatures may be explored for creative use cases - developers are encouraged to experiment with alternative settings.
- Tools: Keep the set of tools well-defined and limit their number to the minimum required for the use case - Avoiding overloading the model with an excessive number of tools.
- Vision: When deploying with vision capabilities, we recommend maintaining an aspect ratio close to 1:1 (width-to-height) for images. Avoiding the use of overly thin or wide images - crop them as needed to ensure optimal performance.

## License

This model is licensed under the [Apache 2.0 License](https://www.apache.org/licenses/LICENSE-2.0.txt).

*You must not use this model in a manner that infringes, misappropriates, or otherwise violates any third party’s rights, including intellectual property rights.*