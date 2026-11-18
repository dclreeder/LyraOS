# streamstt — Nemotron streaming ASR sidecar

`nemotron_stream_server.c` wraps parakeet.cpp's cache-aware streaming
session for `nvidia/nemotron-3.5-asr-streaming-0.6b` (GGUF in
`~/Documents/parakeet.cpp/models/`) in a tiny TCP server on
127.0.0.1:8097. It exists because parakeet.cpp's streaming surface is
its C API, not the HTTP server the batch Parakeet TDT path uses.

Used by: Lyra's RTC mode and the phone call feature
(`core/lyra_core/stream_stt.py` + `rtc.py` +
`production_main.py`'s call transcription), since 2026-11-18.
WakeGate keeps the batch TDT path; if this sidecar is down, RTC
falls back to Endpointer + TDT per session and call turns fall
back to TDT per utterance. The server serves ONE connection at
a time: a second connection waits in the backlog until the
first closes, and clients time out into their fallback.

Build: `./build.sh` (links the parakeet.cpp build tree; output in
`build/`, gitignored). Run: `lyra-nemotron-stream.service`.

Protocol: framed client messages (1-byte type + 4-byte big-endian
length) — `A` s16le 16 kHz PCM, `F` flush/finalize, `R` reset,
`P` ping — and newline-JSON replies (`ready`, `text` with
`last_word_end`, `final`, `reset`, `pong`, `error`). Angle-bracket
control tokens (the model's trailing `<en-US>` locale tag) are
stripped server-side so they can never reach the router. The model
fires no EOU events in practice; turn end is the caller's decision.
