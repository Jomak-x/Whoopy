# Local writer source evidence

Selected official Mistral Ministral 3 14B Instruct 2512 Q4_K_M for a stronger practical writer candidate. The official model card identifies Apache-2.0 licensing, a 13.5B language model plus optional vision encoder, native JSON output, and recommends temperature below 0.1 for production. Selection is provisional until paired whole-session benchmarks assess writing quality and constraints.

- Official repository: https://huggingface.co/mistralai/Ministral-3-14B-Instruct-2512-GGUF
- Pinned revision: `74fac473c43357d7fb2671713608183cc72496d0`
- Pinned model card: https://huggingface.co/mistralai/Ministral-3-14B-Instruct-2512-GGUF/blob/74fac473c43357d7fb2671713608183cc72496d0/README.md
- Pinned API metadata: https://huggingface.co/api/models/mistralai/Ministral-3-14B-Instruct-2512-GGUF/revision/74fac473c43357d7fb2671713608183cc72496d0?blobs=true
- Download: https://huggingface.co/mistralai/Ministral-3-14B-Instruct-2512-GGUF/resolve/74fac473c43357d7fb2671713608183cc72496d0/Ministral-3-14B-Instruct-2512-Q4_K_M.gguf
- License: https://www.apache.org/licenses/LICENSE-2.0

The manifest records the provider-published LFS SHA256 and size, which were both verified against the completed streamed download. This is a comparison against the official repository metadata, not merely a locally generated checksum. Full API response, original model card, and Apache license text are saved alongside this file. No vision projector was downloaded because Whoopy needs text generation.

Runtime version from the installed executable: llama.cpp b10142 (`3d1c3a897`), AppleClang 21.0.0.21000101 for Darwin arm64. One short native JSON smoke uses a 32768-token context, full GPU offload, and the embedded model-native chat template. The GPU lock serializes this with other local benchmark jobs. See `smoke-result.json` and raw logs for measured runtime support, timings, output and limits. The smoke is not a whole-session writing-quality benchmark.

Smoke result: successfully loaded and generated with the requested full GPU offload setting and 32768 context. Wall time 12.73 seconds, reported generation 9.7 tokens/second, macOS child peak RSS 13,660,536,832 bytes (13.66 decimal GB). Extracted JSON has all required fields and a 38-word spoken opening; breath and medical-claim constraints pass manual review. The raw assistant output includes a Markdown JSON fence despite the instruction forbidding Markdown, so strict raw formatting fails. The saved reproduction code is `smoke.py.txt` to exclude evidence-only code from repository Python quality scans. A sandbox-only attempt failed immediately to obtain a local port; the successful run used an approved execution outside the sandbox and held the shared GPU lock.
