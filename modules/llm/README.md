# Lyra LLM module - llama.cpp server (shared local LLM)

Restored 2026-11-17 at Daniel's order: Lyra's router brain is the
local model again -- Gemma 4 12B instruct, Q4_K_M GGUF, served by
llama.cpp's llama-server on the RX 9060 XT (Vulkan, all layers on
GPU). This module is shared infrastructure: the core's free router
points at it (core/lyra_core/config.py, RouterConfig), nothing is
baked into the core.

## Shape

- `llama.cpp/` - fresh clone of ggml-org/llama.cpp (NOT tracked in
  this repo; re-clone to rebuild). Built 2026-11-17 from commit
  52975342ff6b85127ff31930d39e36725fd08809:
  `cmake -B build -DGGML_VULKAN=ON -DCMAKE_BUILD_TYPE=Release`
  `cmake --build build --target llama-server -j 20`
  (build.log from that build sits beside this README, untracked.)
- Model: `data/models/gemma-4-12b-it-Q4_K_M.gguf` (7,121,861,440
  bytes) from unsloth/gemma-4-12b-it-GGUF on Hugging Face -- the
  ggml-org official GGUF repo ships no Q4_K_M, only Q4_0/Q8_0/BF16.
  GGUFs are never git-tracked.
- Service: `services/lyra-llm.service` (user unit, enabled at
  boot): llama-server on 127.0.0.1:8094, OpenAI-compatible /v1,
  `-ngl 99 -c 32768 --alias gemma4:12b`. Production brain -- it
  stays running, unlike the 2026-11 on-demand setup.
- The router sends a dummy key from core/llama_api_key.env (not a
  secret); llama-server ignores it.
