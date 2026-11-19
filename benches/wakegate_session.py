"""Phase 1c bench: the composed session, proven against a MOCK box.

Part A — a fake box client (same callback/emitter surface as BoxClient)
feeds the recorded positive clip through VoiceSession at real-time
pace, followed by digital silence (a real box's stream never stops).
Proves the end-to-end event order: mic forwarded to the wake server ->
Detection -> RUN_START/STT_START -> STT_VAD_START -> STT_VAD_END ->
STT transcript -> INTENT_START/END -> TTS_START (spoken text) -> TTS_END (HTTP URL)
-> RUN_END, and fetch-back of the TTS URL from the core's endpoint.

Part B — the supervisor's reconnect loop against scripted fake
clients: two connect failures, a success, a mid-run drop, a reconnect,
then stop(). Proves backoff/retry, reconnect-after-drop, and that the
only exit path is the clean disconnect.

No real client is ever contacted; HA and production
services are untouched (the Wyoming services are only *used*, as in
the 1b bench — including the production collector filing the replayed
clip, which is observed and reported, not interfered with).
Run via benches/run_all.py (suite: wakegate_session). Part A asserts the
first turn's event order (see the suite note at the assertion):
the wake service may re-fire on the replayed pool clip.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sys
import time
import urllib.request
import wave
from pathlib import Path

# --- suite bootstrap: repo-relative import paths (core + this dir) ---
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.join(_HERE, "..", "core"), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end suite bootstrap ---
from lyra_core.audio_http import AudioHttpServer
from lyra_core.config import CoreConfig
from lyra_core.pipeline import PipelineDriver
from lyra_core.verify import finalize_pending_detection
from lyra_core.wake import REGISTRY
from lyra_core.session import CANNED_REPLY, VoiceSession
from lyra_core.supervisor import CoreSupervisor

CLIP = Path(
    "~/Documents/LyraOS/modules/wakeword/data/clips/positives/"
    "wake_positive_20261111_061439_0.987.wav"
).expanduser()
POS_DIR = Path("~/Documents/LyraOS/modules/wakeword/data/clips/positives").expanduser()
LEDGER = Path("~/Documents/LyraOS/modules/wakeword/data/clips/clips_ledger.jsonl").expanduser()

CHUNK = 3200  # 100 ms at 16 kHz / 16-bit / mono


# ---------------------------------------------------------------------------
# Fake box (Part A)
# ---------------------------------------------------------------------------

class FakeBox:
    """Stands in for BoxClient: constructed with the session's audio
    callback (the exact shape BoxClient uses), records every pipeline
    event the session emits instead of sending it to a device."""

    def __init__(self, on_audio, timeline: list) -> None:
        self._on_audio = on_audio
        self._timeline = timeline
        self.is_connected = True

    def _rec(self, name: str, detail: dict | None = None) -> None:
        self._timeline.append((time.monotonic(), "fakebox.sent", {"name": name, **(detail or {})}))

    def evt_run_start(self) -> None: self._rec("run_start")
    def evt_run_end(self) -> None: self._rec("run_end")
    def evt_stt_start(self) -> None: self._rec("stt_start")
    def evt_stt_vad_start(self) -> None: self._rec("stt_vad_start")
    def evt_stt_vad_end(self) -> None: self._rec("stt_vad_end")
    def evt_stt_end(self, text: str) -> None: self._rec("stt_end", {"text": text})
    def evt_intent_start(self) -> None: self._rec("intent_start")
    def evt_intent_end(self) -> None: self._rec("intent_end")
    def evt_tts_start(self, text: str) -> None: self._rec("tts_start", {"text": text})
    def evt_tts_end(self, url: str) -> None: self._rec("tts_end", {"url": url})
    def evt_error(self, code: str, message: str = "") -> None:
        self._rec("error", {"code": code, "message": message})

    async def feed(self, pcm: bytes, *, pace: bool = True) -> None:
        for off in range(0, len(pcm), CHUNK):
            await self._on_audio(pcm[off:off + CHUNK])
            if pace:
                await asyncio.sleep(0.09)


def _newest(d: Path) -> tuple[str, float] | None:
    files = [p for p in d.iterdir() if p.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest.name, newest.stat().st_mtime


def _last_line(path: Path) -> str:
    try:
        lines = path.read_text().strip().splitlines()
        return lines[-1] if lines else ""
    except FileNotFoundError:
        return ""


def _read_pcm(path: Path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as wf:
        return (wf.readframes(wf.getnframes()), wf.getframerate(),
                wf.getsampwidth(), wf.getnchannels())


def _fetch(url: str) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.status, resp.headers.get_content_type(), resp.read()


# ---------------------------------------------------------------------------
# Part A — mock-box end-to-end run
# ---------------------------------------------------------------------------

async def part_a(cfg: CoreConfig) -> dict:
    print("== Part A: mock-box end-to-end session ==")
    timeline: list = []
    t0 = time.monotonic()

    def hook(kind: str, detail: dict) -> None:
        timeline.append((time.monotonic(), kind, detail))

    server = AudioHttpServer(cfg.http.bind, cfg.http.port)
    server.start()
    driver = PipelineDriver(cfg)
    # The collector janitor hook, wired exactly as production
    # wires it (the STT bridge used to do this job).
    driver.on_transcribed = finalize_pending_detection
    session = VoiceSession(cfg, driver, server, on_event=hook)
    box = FakeBox(session.handle_audio, timeline)
    session.attach_box(box)  # type: ignore[arg-type]  (duck-typed BoxClient)
    await session.start()
    result: dict = {"clip": CLIP.name}
    try:
        # Give the wake link a moment to come up before audio flows.
        await asyncio.sleep(0.5)
        pcm, rate, width, channels = _read_pcm(CLIP)
        print(f"[clip] {CLIP.name}: {rate} Hz, {width * 8}-bit, {channels} ch, "
              f"{len(pcm) / (rate * width * channels):.2f}s")
        await box.feed(pcm)
        # A real box's stream never stops: follow with room silence so the
        # endpointer's hangover can expire naturally.
        await box.feed(b"\x00\x00" * 16000 * 2)  # 2 s of digital silence
        # Wait for the run to complete (back to idle after processing).
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            names = [d.get("name") for _t, k, d in timeline
                     if k == "box_event"]
            if "run_end" in names and session.state == "idle":
                break
            await asyncio.sleep(0.2)
        timeline.append((time.monotonic(), "bench", {"note": "feed complete"}))
        # Fetch the TTS URL back NOW, while the endpoint is still up (it
        # is stopped in the finally below — fetching after that was the
        # first bench run's only failure; the session itself was clean).
        if session.last_tts_url:
            f_status, f_ctype, f_body = await asyncio.to_thread(
                _fetch, session.last_tts_url
            )
            result["tts_fetch"] = {
                "status": f_status, "content_type": f_ctype, "bytes": len(f_body),
            }
            print(f"[http] GET {session.last_tts_url} -> {f_status}, "
                  f"{f_ctype}, {len(f_body)} B")
        # Also fetch the URL the box was actually handed on TTS_END,
        # while the endpoint is up (see the dialect note below: on
        # the tv route it is a different file from last_tts_url).
        box_end = next((d for _t, k, d in timeline
                        if k == "fakebox.sent" and d.get("name") == "tts_end"), None)
        if box_end and box_end.get("url"):
            b_status, b_ctype, b_body = await asyncio.to_thread(
                _fetch, box_end["url"]
            )
            result["box_tts_fetch"] = {
                "status": b_status, "content_type": b_ctype, "bytes": len(b_body),
            }
            print(f"[http] GET box tts_end {box_end['url']} -> {b_status}, "
                  f"{b_ctype}, {len(b_body)} B")
    finally:
        await session.stop()
        server.stop()

    # Print the merged timeline (session milestones + box-bound events).
    print("-- event timeline (t+ seconds from bench start) --")
    for ts, kind, detail in timeline:
        print(f"  t+{ts - t0:6.2f}s  {kind:14s} {detail}")
    # First-turn values come from the timeline, not session.last_*:
    # a repeat detection's run overwrites the session fields (its
    # own empty transcript) before these post-stop reads.
    first_stt_end = next(d for _t, k, d in timeline
                         if k == "box_event" and d.get("name") == "stt_end")
    result["transcript"] = first_stt_end.get("text") or ""
    result["tts_url"] = session.last_tts_url
    print(f"[stt] transcript={result['transcript']!r}")
    print(f"[tts] url={session.last_tts_url}")

    # Assertions on the required order.
    seq = [d.get("name") for _t, k, d in timeline if k == "box_event"]
    kinds = [k for _t, k, _d in timeline]
    expected = ["run_start", "stt_start", "stt_vad_start", "stt_vad_end",
                "stt_end", "intent_start", "intent_end", "tts_start",
                "tts_end", "run_end"]
    assert "wake_forward" in kinds, "mic audio was never forwarded to the wake server"
    assert "detection" in kinds, "gate never fired a Detection"
    assert kinds.index("wake_forward") < kinds.index("detection"), "forward must precede detection"
    assert kinds.index("detection") < kinds.index("box_event"), "detection must precede box events"
    # The contract is the FIRST turn's event order. The gate
    # can fire a second Detection on this replayed pool clip
    # (observed 2026-11-19: detections ~2.7 s apart; the collector
    # registry shows the same double-fire in production).
    # The session handles the repeat correctly -- a run that finds
    # no speech and errors back to idle -- so trailing events after
    # the first run_end are reported, not asserted away.
    first_turn = seq[:seq.index("run_end") + 1] if "run_end" in seq else seq
    assert first_turn == expected, f"box event order mismatch:\n  got      {first_turn}\n  expected {expected}"
    if len(seq) > len(first_turn):
        print(f"[bench] note: trailing events after the first turn "
              f"(repeat wake detection, handled): {seq[len(first_turn):]}")
    assert result["transcript"], "empty transcript"
    vad_i = kinds.index("vad_start"); _ = vad_i
    assert "vad_start" in kinds and "vad_end" in kinds, "endpointer VAD events missing"
    assert kinds.index("vad_start") < kinds.index("vad_end") < kinds.index("transcript"), \
        "VAD events must bracket the capture before the transcript"
    assert "tts_url" in kinds, "TTS URL was never produced"

    # Dialect check: TTS_START must carry the spoken TEXT and TTS_END
    # the audio URL, in that order (ESPHome voice_assistant.cpp reads
    # "text" on TTS_START and "url" on TTS_END; swapped/empty args make
    # the firmware bail silently). Asserted on what the fake box was
    # actually sent, not on session notify details.
    sent = [d for _t, k, d in timeline if k == "fakebox.sent"]
    sent_names = [d["name"] for d in sent]
    fb_start = next(d for d in sent if d["name"] == "tts_start")
    fb_end = next(d for d in sent if d["name"] == "tts_end")
    assert fb_start["text"] == CANNED_REPLY, fb_start
    # NOT == session.last_tts_url: since 2026-11-15 the TV is the
    # only audio out, so on the tv speech route the box (ears only)
    # is handed a silence WAV of the reply's length as its TTS
    # audio -- a different stored file from the real reply audio
    # behind last_tts_url. The dialect contract is that TTS_END
    # carries a URL that fetches back as WAV; both URLs are
    # fetch-checked (the box one just below, the reply at the end).
    assert fb_end["url"], fb_end
    assert sent_names.index("tts_start") < sent_names.index("tts_end"), sent_names

    # The TTS URL must have fetched back as WAV while the endpoint was up.
    fetched = result.get("tts_fetch")
    assert fetched and fetched["status"] == 200 and fetched["content_type"] == "audio/wav", fetched
    box_fetched = result.get("box_tts_fetch")
    assert box_fetched and box_fetched["status"] == 200 and box_fetched["content_type"] == "audio/wav", box_fetched
    print("Part A: PASS")
    return result


# ---------------------------------------------------------------------------
# Part B — supervisor reconnect behaviour against scripted fakes
# ---------------------------------------------------------------------------

class ScriptedFakeBox:
    """Client stand-in with a scripted connect outcome and a drop()
    switch, shaped to the CURRENT supervisor contract (supervisor.py
    is transport-agnostic since the localmic cutover): connect()
    just connects -- the BoxClient-era subscribe step and the
    unsubscribe-then-disconnect pair are gone; disconnect() is the
    supervisor's single clean-exit call, used for the intentional
    stop AND to close out a dropped client before discarding it."""

    def __init__(self, plan: str) -> None:
        self.plan = plan  # "fail" | "ok"
        self.calls: list[str] = []
        self._connected = False

    async def connect(self) -> None:
        self.calls.append("connect")
        if self.plan == "fail":
            raise OSError("scripted connect failure")
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected

    def drop(self) -> None:
        self._connected = False

    async def disconnect(self) -> None:
        self.calls.append("disconnect")
        self._connected = False


async def part_b(cfg: CoreConfig) -> dict:
    print("== Part B: supervisor reconnect ==")
    plans = iter(["fail", "fail", "ok", "ok"])
    made: list[ScriptedFakeBox] = []

    def factory() -> ScriptedFakeBox:
        box = ScriptedFakeBox(next(plans))
        made.append(box)
        return box

    class _NullDriver:
        pass

    session = VoiceSession(cfg, _NullDriver(), None)  # type: ignore[arg-type]
    supervisor = CoreSupervisor(
        cfg, session, client_factory=factory,  # type: ignore[arg-type]
        backoff_initial_s=0.05, backoff_max_s=0.2, poll_s=0.02,
    )
    run_task = asyncio.create_task(supervisor.run())
    # Wait for the third box (first success) to be supervising.
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (
        len(made) >= 3 and made[2].is_connected
    ):
        await asyncio.sleep(0.02)
    assert len(made) >= 3 and made[2].is_connected, "supervisor never connected"
    assert session.state == "idle"
    # Simulate the API connection dropping mid-supervision.
    made[2].drop()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not (
        len(made) >= 4 and made[3].is_connected
    ):
        await asyncio.sleep(0.02)
    assert len(made) >= 4 and made[3].is_connected, "supervisor never reconnected"
    # Clean stop: disconnect() is the ONLY exit path -- the stopped
    # client and the dropped one each get exactly one, and a client
    # whose connect failed is never disconnected.
    supervisor.stop()
    await asyncio.wait_for(run_task, timeout=10)
    for i, box in enumerate(made):
        print(f"  fake[{i}] plan={box.plan} calls={box.calls}")
        if box.plan == "fail":
            assert box.calls == ["connect"], \
                f"fake[{i}]: failed connect must not be disconnected: {box.calls}"
        else:
            assert box.calls == ["connect", "disconnect"], \
                f"fake[{i}]: expected connect then exactly one disconnect: {box.calls}"
    assert made[3].calls == ["connect", "disconnect"], \
        "final client must exit via the clean disconnect"
    assert supervisor.connect_attempts == 4, supervisor.connect_attempts
    assert supervisor.successful_connects == 2, supervisor.successful_connects
    print(f"  attempts={supervisor.connect_attempts} "
          f"successful={supervisor.successful_connects}")
    print("Part B: PASS")
    return {"attempts": supervisor.connect_attempts,
            "successful": supervisor.successful_connects,
            "fake_calls": [b.calls for b in made]}


# ---------------------------------------------------------------------------

async def run() -> int:
    cfg = CoreConfig.load()
    # Suite: bench HTTP server on 8199, never the production port.
    cfg = dataclasses.replace(cfg, http=dataclasses.replace(cfg.http, port=8199))
    # Silence (house rule): the driver's TV mirror is a
    # fire-and-forget POST to lyra-web; record it in-process so no
    # bench speech ever reaches the TV page.
    mirrored: list[int] = []
    async def _record_mirror(wav_bytes: bytes) -> None:
        mirrored.append(len(wav_bytes))
    import lyra_core.pipeline as _pipeline_mod
    _pipeline_mod._mirror_wav_to_tv = _record_mirror

    before_clip = _newest(POS_DIR)
    before_ledger = _last_line(LEDGER)
    before_pending = sorted(REGISTRY.pending_ids())
    report: dict = {"reply_text": CANNED_REPLY}
    report["part_a"] = await part_a(cfg)
    report["part_b"] = await part_b(cfg)
    # Production collector side effect (by design, as in 1b): observe only.
    await asyncio.sleep(8)
    after_clip = _newest(POS_DIR)
    after_ledger = _last_line(LEDGER)
    after_pending = sorted(REGISTRY.pending_ids())
    report["collector"] = {
        "new_positive_clip": (after_clip[0] if after_clip and after_clip != before_clip else None),
        "new_ledger_line": after_ledger if after_ledger != before_ledger else None,
        "pending_before": before_pending, "pending_after": after_pending,
    }
    print(f"[collector] new clip: {report['collector']['new_positive_clip']}")
    print(f"[collector] new ledger line: {report['collector']['new_ledger_line']}")
    print(f"[collector] pending before={before_pending} after={after_pending}")
    print("===SESSION_BENCH_JSON===")
    print(json.dumps(report, indent=2))
    print("===SESSION_BENCH_PASS===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
