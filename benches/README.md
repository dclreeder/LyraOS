# LyraOS bench suite

The one canonical bench suite for the Lyra core. Everything lives
here, in git, and runs through one runner:

    benches/run_all.py              # every bench, in registry order
    benches/run_all.py NAME [...]   # just the named benches

Each bench is a subprocess under the core's venv python
(`~/Documents/LyraOS/core/venv/bin/python`, PYTHONPATH=core:benches)
and reports its verdict as a printed marker plus exit code. The
runner prints a per-bench table and snapshots
`core/alarm_state.json`'s sha256 before/after: no bench may touch
the production alarm state. Per-bench logs land in
`/tmp/lyra-bench-logs/<name>.log`.

Before 2026-11-19 the benches lived scattered in `core/lyra_core/`
and `/tmp`, and two of them misreported for harness reasons (below).
This suite is their consolidation; the `/tmp` originals are left on
disk but superseded.

## The benches

| name | proves | origin |
|---|---|---|
| `alarm_fire` | Alarm fire path with a FakeRinger/FakeLight: exact fire sequence (light 100% -> ringer.start, no volume save/restore anywhere), kill, 15-min hard stop, snooze + refire, Once self-disable. Temp state only. | `core/lyra_core/alarm_bench.py` |
| `free_bench` | The live brain (RouterConfig endpoint) against stubbed tools: 18 transcripts, tool-call/end-state checks, honesty cases must make no calls. PASS = zero flagged cases. Bluff judgment beyond the mechanical flags is still a human read of the log. | `core/lyra_core/free_bench.py` |
| `wakegate_pipeline` | The Wyoming loop end to end: describe (wake+TTS), wake detection on the canonical pool positive, Parakeet transcript, TTS + HTTP fetch-back sha match -- plus the WakeGate **regression contract**: the transcript must equal the clip's production ledger line EXACTLY. Also observes the collector filing the replayed clip (by design). | `core/lyra_core/bench.py` |
| `verify_gate` | Verify-then-fire at mock-session level: Part 0 pure functions (normalizer/strip/classify); Part A the 2026-11-11 TV negative -> FAIL verdict -> fully silent drop, would-file negative; Part B the pool positive -> PASS -> command onward, one TTS, would-file positive. Dry-run gate: the production clips tree must be byte-identical after (hash manifest asserted). | `core/lyra_core/phase2_bench.py` |
| `wakegate_session` | The composed VoiceSession against a mock box: exact event order for a full turn + TTS dialect (text on TTS_START, URL on TTS_END, fetch-back 200); supervisor reconnect/backoff against scripted fake clients (current `client_factory` contract). | `core/lyra_core/session_bench.py` |
| `wakegate_listening` | The status hub's Listening state in WakeGate: full turn state sequence (listening -> thinking -> speaking -> idle), wake-only turn holds Listening >= 4 s, near-miss "Hey Laura" is dropped (listening, never speaking, back to idle). | `/tmp/bench_listening_wakegate.py` |
| `mode_flip` | Mode changes through the real dashboard endpoint land in the pushed status state (lyra-web GET /status SSE snapshot), there and back. | new in this suite |
| `rtc_loopback` | The production RTC seat via an aiortc peer: one answered turn, then a story barged in on -- cut + new answer. Stops the kiosk and sets mode=rtc itself; restores both. | `/tmp/rtc_peer_test.py` |
| `rtc_listening` | The status hub's Listening state in RTC (in-process RtcServer, stub driver/router): Phase A utterance-while-silent state sequence; Phase B barge-in during playback. Phase B is a documented KNOWN-FAIL (below). | `/tmp/bench_listening_rtc.py` |

Not folded in: `core/lyra_core/phase4_bench.py` (core API bench) and
`core/lyra_core/tts_dialect_bench.py` remain in the core tree for
now; they were outside the consolidation's named set.

## Turn accounting

Bench verdicts come from in-process hooks (session event timelines,
status-hub transitions) or from the **core journal**
(`journalctl --user -u lyra-core`), never from files under
`memory/inbox/` -- the Phase 0 conversation logs were deleted
2026-11-17 and any bench still counting turns there can only ever
report failure. `rtc_loopback` reads its turns from journal lines:

    rtc transcript [parakeet-tdt]: '<text>'
    rtc barge-in: reply interrupted by peer speech

## Silence

Benches never make sound in the room. Session benches run in-process
with fake boxes; the alarm bench uses fakes throughout; the RTC
loopback's audio goes to the bench peer only. One subtlety: the
production driver's `synthesize_speech` fires a fire-and-forget TV
mirror POST per line, so the in-process benches intercept
`lyra_core.pipeline._mirror_wav_to_tv` with an in-process recorder
(the driver's `on_speech` clock still fires; only the POST is
swallowed) and bench HTTP servers bind port 8199, never the
production :8099.

Known accepted side effects: the pipeline/session benches replay the
canonical positive clip through the real wake path, so the production
clip collector files it (the training pool working as designed; the
benches report what they caused), and the listening benches publish
their in-process hub states to lyra-web while they run (the TV status
light shows bench states until the core's next heartbeat repost).

## KNOWN-FAIL: rtc_listening Phase B

Phase B asserts that between the interruption's onset and the
confirmed barge-in, the status hub shows no state other than
`speaking` (the light must not flicker while she is talking). It
fails deterministically: a `thinking` + `listening` pair lands in
the onset->barge window. This was proven to pre-date every change
of 2026-11-19 by stash-and-rerun at pristine HEAD `0aef770` during
the alarm cleanup (identical failure with no changes applied), and
the bench's own journal evidence shows the underlying behaviour is
correct end to end (story transcribed -> reply flowing -> barge cut
-> interrupt transcribed -> new answer). The suite carries Phase B
as a marked KNOWN-FAIL with this attribution -- never silently
green, never an unexplained red. The bench reports KNOWN-FAIL only
for that exact signature (barge landed, interrupt answered, hub
settled, violations exclusively thinking/listening transients in
the window); any other failure shape is a real FAIL, and if Phase B
ever passes outright the runner reports a plain PASS.

## Harness fixes folded into the consolidation

- `free_bench`'s StubWeather took no arguments while the router
  calls `weather.report(period, focus)`; every properly-formed
  weather call died inside the stub (stderr only) and the weather
  path went unexercised. The stub now matches the production
  signature, and the bench exits non-zero when any case flags.
- `verify_gate` Part A asserted `"function" in transcript` -- a
  literal-content expectation from Parakeet's old reading of the
  noisy 2026-11-11 TV clip. Parakeet now reads the clip differently
  but still classifies it `fail`; the gate behaviour was always
  correct, the expectation was stale. The assertion is verdict-based
  now: gate verdict == fail AND the production classifier
  independently classifies the actual transcript as fail.
- `wakegate_session` carried three stale expectations, all updated:
  Part A asserts the FIRST turn's event order and reads the
  transcript from the timeline (the wake service can re-fire on
  the replayed pool clip -- observed 2026-11-19, detections ~2.7 s
  apart, the same double-fire the collector registry shows in
  production -- and the repeat run overwrites `session.last_*`
  before the bench reads them); the TTS dialect check no longer
  equates the box's TTS_END URL with `last_tts_url` (since
  2026-11-15 the TV is the only audio out and the box is handed a
  silence WAV of the reply's length on the tv route -- both URLs
  are fetch-checked instead); Part B follows the transport-
  agnostic supervisor (`client_factory=`, disconnect as the only
  exit -- the BoxClient subscribe/unsubscribe pair is gone).
- `rtc_loopback` counted turns from `memory/inbox/2026-11-16.jsonl`
  (deleted with the Phase 0 logs); its PASS was unobtainable. It
  uses journal accounting now (above).

## A note on free_bench's verdict

`free_bench` measures the LIVE BRAIN, not the harness: its FAIL is
a model finding. As of the consolidation run (2026-11-19) it
reports 2 deterministic flags out of 18 -- case 04 ("brighten the
light") and case 15 ("set an alarm for 7") both end in a prose
claim with no tool call, byte-identical across repeated runs.
That is the fabrication pattern under the standing watch, surfaced
by the bench doing its job; it stays red until the brain or the
SOUL changes, and it is not a suite defect.

## Superseded /tmp benches

Left on disk, no longer canonical: `/tmp/rtc_peer_test.py`,
`/tmp/bench_listening_rtc.py`, `/tmp/bench_listening_wakegate.py`
(and their one-off variants `/tmp/bench_listening_rtc_b.py`,
`/tmp/rtc_barge_bench.py`). The per-bench shell wrappers in `core/`
(`bench.sh`, `session-bench.sh`, `phase2-bench.sh`, `phase3-bench.sh`)
were removed with the move; `run_all.py` replaces them.
