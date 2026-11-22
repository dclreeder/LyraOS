# embed — the memory embedding service

NVIDIA Nemotron-3.5-Embed-1B (BF16), text tower only, served by
llama-server on 127.0.0.1:8092 (unit: services/lyra-embed.service).
Asymmetric prefixes per the model card: "query: " for questions,
"passage: " for stored text (applied inside lyra_core.memory's
Embedder, never by callers). Mean pooling, 2048-dim vectors.

## How the GGUF was made (2026-11-21)

Per the memory plan's sourcing rule, the GGUF was converted HERE
from NVIDIA's official BF16 checkpoint -- not downloaded from a
third party:

- Source: huggingface.co/nvidia/Nemotron-3.5-Embed-1B-BF16
  (safetensors sha256 ad3e1b8bbda04bbd8c969f19df0b66ea672f915f
  624800ba0b982c5c01bf92af, verified after download).
- Converter: llama.cpp's convert_hf_to_gguf.py, whose
  conversion/mistral3.py carries a dedicated
  Mistral3EmbedNemotronModel class for this exact checkpoint --
  it maps the Ministral3 text tower only (146 tensors, 2.3 GB;
  the vision tower is not in the file) and stamps mean pooling
  + the nemotron-3.5-embed tokenizer pre into the GGUF.
- Output: data/models/nvidia_Nemotron-3.5-Embed-1B-BF16.gguf
  (--outtype bf16).
- Build env: modules/embed/venv (llama.cpp's converter
  requirements). NOTE: transformers had to be upgraded to 5.x in
  that venv -- the checkpoint's tokenizer_config declares
  tokenizer_class "TokenizersBackend", unknown to the pinned
  transformers 4.57. The venv is a build tool only; nothing in
  production imports it.
- The HF download itself (hf/) and the venv are gitignored
  build artifacts; the checkpoint can be re-fetched and the
  conversion re-run from this README alone.
