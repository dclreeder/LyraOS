# Lyra core — the Fedora-native voice core (PRODUCTION since 2026-11-11)

The Lyra core is the speaker box's only controller. It took the
ESPHome API seat from Home Assistant at the bold cutover on
2026-11-11 (~12:41 EST); HAOS was shut down gracefully and nothing
on the OptiPlex was deleted — rollback is: power the OptiPlex on,
then stop lyra-core.service. Production runs as the user service
`lyra-core.service`:

    python -m lyra_core.production_main

under the ESPHome venv interpreter
(`~/Documents/esphome-probe/venv/bin/python`) with `~/lyra-core` on
PYTHONPATH.

## What runs

- `lyra_core/production_main.py` — the composition root: HTTP
  endpoint, pipeline driver, voice session + supervisor, the
  Fedora-native alarm, the phrase router (session's on_command
  hook), and the Phase 4 core API. SIGTERM/SIGINT exits through
  the supervisor's clean unsubscribe-then-disconnect seat release.
- `lyra_core/config.py` — JSON config (`config.json`, defaults
  built in; see `config.example.json`). The box Noise PSK is
  resolved lazily from the ESPHome production secrets file at
  connect time only.
- `lyra_core/box_client.py` — ESPHome-API client for the Lyra Flex
  on aioesphomeapi 46.12.0: connect + voice-assistant subscription
  (API audio) + the full pipeline-event emitter (incl. STT VAD
  start/end and the firmware's special-cased error codes) +
  announcements + an `is_connected` liveness property.
- `lyra_core/wyoming_client.py` — minimal Wyoming wire client
  (asyncio, stdlib only; framing mirrors wyoming 1.11.0). Keeping
  it stdlib-only is deliberate: the whole core runs under the
  esphome venv, which has aioesphomeapi but not the wyoming
  package, and no second environment is introduced.
- `lyra_core/pipeline.py` — HA's old client role against the
  production Wyoming services (wake tcp://127.0.0.1:10400, STT
  :10300, TTS :10200): describe / detect_wake / transcribe_audio /
  synthesize_speech.
- `lyra_core/audio_http.py` — HTTP endpoint (stdlib
  ThreadingHTTPServer, one per bind address). Serves generated
  speech at `http://192.168.12.144:8099/audio/<id>.wav` — the URL
  form the box fetches for pipeline TTS, announcements, and the
  alarm tone route. Binds loopback + the Fedora's LAN address
  only (config `http.bind`; NOT 0.0.0.0 since the 2026-11-11
  dial-in), `/healthz` for liveness. The audio store is bounded:
  10-minute TTL, 32-entry cap, oldest-first eviction.
- `lyra_core/endpointer.py` — core-side speech endpointer: 30 ms
  frames, adaptive minimum-statistics noise floor, hysteresis;
  emits STT VAD start/end from the mic stream's energy. The box's
  beam lock releases on VAD_END, so the ~700 ms hangover matches
  HA's old behaviour. Self-check: `python -m lyra_core.endpointer`.
- `lyra_core/session.py` — the composed voice session: box mic ->
  wake server; on Detection, RUN_START/STT_START, capture +
  endpoint the utterance, verify (below), route, speak the
  confirmation via TTS + the HTTP endpoint, TTS_START/END,
  RUN_END — HA's event order and timing. Failed/maybe verifies
  end as the firmware-ignored `stt-no-text-recognized` code with
  no reply (silent drop).
- `lyra_core/verify.py` — the verify-then-fire gate, in-path: the
  combined wake+command clip is transcribed directly through the
  shared whisper.cpp (:8093) and classified by the PRODUCTION
  clip_collector (imported, not forked); verdicts file clips to
  the training pool in live mode, and the gate claims + discards
  its own wake-server spool entry — selected by the spool second
  the session stamps at Detection — so no turn is ever filed
  twice.
- `lyra_core/router.py` — the LLM router: the shared local LLM
  (lyra-llm, Qwen3-4B-Instruct-2507) classifies each verified
  command into a closed set of 17 intents under a JSON-schema
  grammar; slots are re-validated in the router, and routed
  commands run the readback-verified tools in `tools.py` (light /
  monitor / alarm / clock / weather) and speak confirmations from
  the actual result — model text is never spoken. Garbled
  commands get one "didn't catch that"; no-match commands (and
  any LLM failure) get the spoken "I don't understand." line.
  (Replaced the deterministic phrase matcher 2026-11-11.)
- `lyra_core/alarm.py` — the Fedora-native alarm (06:00 /
  Weekdays as migrated): persisted state, catch-up rule, Shelly
  light, box volume save/ring-volume/restore, 15-minute tone,
  15-minute hard stop, snooze, and the token-authenticated HTTP
  kill endpoint for the iOS Action Button. Scheduler math
  self-check: `python -m lyra_core.alarm`.
- `lyra_core/api.py` — the core API on the same :8099 endpoint:
  POST /tool executes the router's own tool objects and returns
  their verified ToolResults as JSON; POST /announce speaks a
  statement through the box. Token auth shared with the kill
  endpoint; handler timeouts cancel the underlying work.
- `lyra_core/supervisor.py` — HA-parity reconnect/resubscribe
  loop: retry-forever with capped exponential backoff, liveness
  via `APIClient.is_connected`, exactly one exit path.
- `lyra_core/preflight.py` + `__main__.py` — `python -m lyra_core
  --preflight` (or `preflight.sh`): config/Wyoming/engine/HTTP
  checklist that never contacts the box. There is intentionally
  no run mode in `__main__`; production is `production_main`.
## Benches

The Phase 1–4 bench modules (`bench.py`, `session_bench.py`,
`phase2_bench.py`, `phase3_bench.py`, `phase4_bench.py`,
`alarm_bench.py`, with their `.sh` wrappers) are the historical
proofs the production composition was built from. Caution: several
bind the production :8099 or drive service-shaped pieces — run
them only with production stopped or on alternate ports/paths,
and never against the real box without Daniel's explicit go.

## Operations

    systemctl --user status lyra-core.service
    systemctl --user restart lyra-core.service
    journalctl --user -u lyra-core.service -f

State the system owns: `alarm_state.json` (the alarm),
and the training clip pool under
`~/Documents/modules/wakeword/` (written by the production
collector through the verify gate).
