# LLM module - shared local language model (llama.cpp)

One llama.cpp server that any project can tap over HTTP, instead of every
project embedding its own model copy (Daniel, 2026-11-01). Same pattern as
the whisper and kokoro modules: the engine lives here, projects consume it.

## Entry points

- Engine: `llama.cpp/` (upstream ggml-org/llama.cpp, built `-DGGML_VULKAN=ON`
  for the RX 9060 XT - same backend choice as the whisper build; the full
  Vulkan toolchain is already on the Fedora, no system ROCm needed).
- Service: user unit `lyra-llm.service` running `llama-server`,
  **on-demand - NOT boot-enabled**, so it never squats on VRAM the voice
  services or a training run need. Start/stop:
  `./llm-ctl start|stop|restart|status|test`
  (or `systemctl --user start lyra-llm`). Startup to /health ok: ~8 s.
- API: OpenAI-compatible, `http://127.0.0.1:8094`
  - `POST /v1/chat/completions` (model name: `lyra-llm`)
  - `GET /health` -> {"status":"ok"} when loaded
- Models: `models/` - default is
  `Qwen3-4B-Instruct-2507-Q4_K_M.gguf` (2.4 GB, unsloth GGUF mirror; the
  official Qwen GGUF repo would not serve anonymously).
  Verified 2026-11-01: ~91 tok/s generation, ~170 tok/s prompt, fully on
  GPU (-ngl 99), 8k context.

## Swapping the model

Drop another .gguf in `models/`, edit the `-m` path in
`~/.config/systemd/user/lyra-llm.service`, then
`systemctl --user daemon-reload && ./llm-ctl restart`.
VRAM budget: 16 GB total; whisper large-v3-turbo + kokoro hold ~3 GB, so
keep the LLM + its KV cache under ~10 GB (a 4B-8B Q4/Q5 fits comfortably;
a 8B Q8 or anything 14B+ gets tight).

## Rebuilding llama.cpp

cd llama.cpp && git pull && cmake -B build -G Ninja   -DCMAKE_BUILD_TYPE=Release -DGGML_VULKAN=ON -DLLAMA_CURL=OFF   && cmake --build build -j --target llama-server llama-cli

## Consumers

None yet - built 2026-11-01 as standing infrastructure ("spin one up when
a project needs one"). The single-instance rule applies: projects call
:8094, they never spawn a private llama-server.
