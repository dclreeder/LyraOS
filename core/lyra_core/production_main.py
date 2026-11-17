"""Production entry point for the Lyra core (bold cutover, 2026-11-11).

Composes the existing pieces exactly as the benches do — AudioHttpServer,
PipelineDriver, VoiceSession, CoreSupervisor — and runs them as the
long-lived controller. Composition only: no bench/session/pipeline
code is modified.

- The Phase 2 VerifyGate is attached in LIVE filing mode (flipped
  2026-11-11 with the spool double-file fix): verify-then-fire gating
  is live (fail/maybe turns end silently) and verified clips are
  filed to the training pool by the production collector. After each
  verified turn the gate claims + discards its own wake-server spool
  entry, so the spool janitor never files a core-handled turn twice.
- The Fedora-native alarm (Phase 3) runs in this composition:
  persisted state + per-minute scheduler with the catch-up rule,
  Shelly light, 15-minute hard stop, snooze, and the authenticated
  HTTP kill endpoint for Daniel's iOS Action Button. Since
  2026-11-15 the ring sounds through the TV (TvRinger: display on,
  TV woken + volume-managed over ADB, the web page loops the tone
  and acks); the attached-client ring (localmic: the tone via
  Fedora's default sink) remains as the fallback path.
- The session's on_command hook is the free router
  (lyra_core.free_router): Muse Spark 1.3 via the Meta Model
  API interprets each verified command uncaged and speaks its
  own replies, with the readback-verified tools (light / alarm /
  clock / weather / tv) as the only way anything actually
  happens. Garbled commands get the one D5 "didn't catch that"
  line; a failed turn speaks the fallback line.
- The Phase 4 core API (lyra_core/api.py) is Lyra's hands on this
  same HTTP endpoint: POST /tool executes the router's own tool
  objects and returns their verified ToolResults as JSON, and
  POST /announce speaks a statement through the TV mirror. Both
  share the alarm kill endpoint's token.
- Morning note (2026-11-12): the alarm can hold a one-shot,
  date-scoped spoken note; the composition wires _announce
  as the alarm's note speaker, so a morning kill speaks the note
  right after the ring ends. Notes are set through the API
  (alarm note_set), not the voice router.
- SIGTERM/SIGINT -> supervisor.stop(): the only exit is the clean
  disconnect of the attached client.
- Transport is the core config's ONE switch, `transport`, and its
  only value is now "localmic" (2026-11-16): the session's client
  is a PipeWire capture of Fedora's default audio source, and her
  voice leaves via the TV mirror to Fedora's default sink. The
  speaker-box transports (ESPHome API, satlink) were removed with
  the box.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import signal
import sys
import threading
import time
import uuid
import wave

from .alarm import Alarm, ShellyLight
from .api import CoreApi
from .audio_http import AudioHttpServer
from .config import CoreConfig
from .pipeline import PipelineDriver
from .free_router import FreeRouter
from .localmic import LocalMicClient
from .session import VoiceSession
from .inbox import InboxLog
from .tv_ring import TvRinger
from .tools import (
    AlarmTool,
    ClockTool,
    LightTool,
    TvTool,
    TvVolumeTool,
    WeatherTool,
)
from .supervisor import CoreSupervisor
from .verify import VerifyGate

_LOGGER = logging.getLogger("lyra_core.production")

STATUS_PERIOD_S = 15.0


def _event_hook(kind: str, detail: dict) -> None:
    _LOGGER.info("session event: %s %s", kind, detail)


async def _status_loop(session: VoiceSession, supervisor: CoreSupervisor) -> None:
    while True:
        await asyncio.sleep(STATUS_PERIOD_S)
        client = session._box
        floor = session._ep.noise_floor
        _LOGGER.info(
            "status: client_connected=%s session_state=%s wake_link_up=%s "
            "ep_noise_floor=%s connects=%d attempts=%d",
            bool(client is not None and client.is_connected),
            session.state,
            session._wake_link_up,
            ("%.0f" % floor) if floor is not None else None,
            supervisor.successful_connects,
            supervisor.connect_attempts,
        )



async def amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg = CoreConfig.load()
    http = AudioHttpServer(cfg.http.bind, cfg.http.port)
    http.start()
    driver = PipelineDriver(cfg)
    gate = VerifyGate(dry_run=False)
    inbox = InboxLog()
    session = VoiceSession(cfg, driver, http, verify_gate=gate, on_event=_event_hook)

    if cfg.transport != "localmic":
        raise ValueError(
            f"unknown transport {cfg.transport!r}: the speaker-box "
            "transports were removed 2026-11-16; only 'localmic' remains"
        )
    # Box-free transport (2026-11-16, Daniel: "act like the box is
    # gone"): the session's ears are a PipeWire capture of Fedora's
    # default audio source, and her voice leaves via the TV mirror
    # -> Fedora's default sink. No satlink listener is opened.
    def _localmic_factory() -> LocalMicClient:
        return LocalMicClient(
            cfg,
            on_audio=session.handle_audio,
            on_start=session.handle_start,
            on_stop=session.handle_stop,
            audio_store=http.store,
        )

    supervisor = CoreSupervisor(cfg, session, client_factory=_localmic_factory)
    _LOGGER.info(
        "transport: localmic (Fedora default source; no satellite box)"
    )
    alarm = Alarm(cfg, box_getter=lambda: session._box, http=http)
    alarm._inbox = inbox
    # TV sounder (2026-11-15): the alarm rings through the TV; the
    # attached-client path (localmic: default sink) is the fallback.
    alarm.set_tv_ringer(TvRinger())
    # Item F (dial-in 2026-11-11): validate the alarm's required
    # assets BEFORE any background service starts. A missing tone
    # degrades the alarm only -- it must never crash-loop the voice
    # service -- so the outcome is logged loudly, never raised.
    if not alarm.validate_assets():
        _LOGGER.error(
            "alarm tone asset missing/unreadable: the alarm is "
            "DEGRADED (light-only rings); the voice service "
            "continues normally"
        )
    light_tool = LightTool(ShellyLight(cfg.alarm.light_host, cfg.alarm.light_id))
    tv_tool = TvTool()
    tv_volume_tool = TvVolumeTool()
    alarm_tool = AlarmTool(alarm)
    clock_tool = ClockTool(cfg.router.timezone)
    weather_tool = WeatherTool(
        cfg.router.weather_latitude,
        cfg.router.weather_longitude,
        cfg.router.timezone,
    )
    router = FreeRouter(
        cfg.router,
        light=light_tool,
        alarm_tool=alarm_tool,
        clock=clock_tool,
        weather=weather_tool,
        tv=tv_tool,
        tv_volume=tv_volume_tool,
        inbox=inbox,
    )
    session._on_command = router.handle
    _LOGGER.info("FREE MODE live: %s", router.describe())

    async def _announce(text: str) -> None:
        """Phase 4 /announce: Kokoro -> TV mirror. synthesize_speech
        fires the mirror itself (speech_route is unconditionally the
        TV since 2026-11-15), so announcing is just synthesizing
        the line."""
        await driver.synthesize_speech(text)


    # Morning note: the alarm speaks its stored note through this
    # same announce path when a morning kill ends the ring.
    alarm.set_note_speaker(_announce)

    # -- dashboard call, streamed sentence by sentence (2026-11-15) --
    # /call/start transcribes and returns at once; the turn runs in
    # the background, the router feeds reply sentences to a
    # _CallSpeaker as they generate, and each is synthesized in order
    # (firing the TV mirror per sentence, like the room path) and
    # appended to the call's entry. The call page polls _call_poll and
    # plays the sentences as they land, so she starts talking after
    # roughly one sentence instead of the whole reply plus the whole
    # synthesis (the old /call/turn shape). Entries are pruned at
    # 10 minutes; one call turn at a time.
    _call_lock = threading.Lock()
    _call_entries: dict[str, dict] = {}

    def _call_add_sentence(entry: dict, wav_bytes: bytes) -> None:
        with _call_lock:
            entry["sentences"].append(wav_bytes)

    class _CallSpeaker:
        """The room's _SentenceSpeaker shape, minus the box: the
        worker synthesizes fed sentences in order and appends the
        WAVs to the call entry the phone polls. Synthesis runs in a
        worker task so TTS never blocks the token stream."""

        def __init__(self, entry: dict) -> None:
            self._entry = entry
            self._queue: asyncio.Queue = asyncio.Queue()
            self._worker: asyncio.Task | None = None
            self.used = False

        async def feed(self, sentence: str) -> None:
            self.used = True
            if self._worker is None:
                self._worker = asyncio.ensure_future(self._run())
            await self._queue.put(sentence)

        async def _run(self) -> None:
            while True:
                sentence = await self._queue.get()
                if sentence is None:
                    return
                try:
                    wav_bytes, _r, _w, _c = await driver.synthesize_speech(sentence)
                except Exception:
                    _LOGGER.exception(
                        "call sentence synthesis failed: %r", sentence)
                    continue
                _call_add_sentence(self._entry, wav_bytes)

        async def drain(self) -> None:
            if self._worker is not None:
                await self._queue.put(None)
                await self._worker
                self._worker = None

    async def _run_call_turn(entry: dict, transcript: str) -> None:
        try:
            speaker = _CallSpeaker(entry)
            if transcript:
                reply = str(await router.handle(
                    transcript, on_sentence=speaker.feed, via="call"))
            else:
                reply = "didn't catch that."
            await speaker.drain()
            if not speaker.used:
                # Nothing streamed (a router early-return line): speak
                # the whole reply as one sentence, as before.
                wav, _r, _w, _c = await driver.synthesize_speech(reply)
                _call_add_sentence(entry, wav)
            with _call_lock:
                entry["reply"] = reply
                entry["done"] = True
        except Exception as exc:
            _LOGGER.exception("call turn failed")
            with _call_lock:
                entry["error"] = f"call turn failed: {exc}"
                entry["done"] = True

    async def _call_start(wav_bytes: bytes) -> dict:
        now = time.monotonic()
        with _call_lock:
            stale = [k for k, e in _call_entries.items()
                     if now - e["created"] > 600]
            for k in stale:
                del _call_entries[k]
            if any(not e["done"] for e in _call_entries.values()):
                return {"busy": True}
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = wf.getframerate()
            width = wf.getsampwidth()
            channels = wf.getnchannels()
            pcm = wf.readframes(wf.getnframes())
        transcript = await driver.transcribe_audio(
            pcm, rate=rate, width=width, channels=channels
        )
        entry = {
            "call_id": uuid.uuid4().hex[:16],
            "created": now,
            "transcript": transcript,
            "sentences": [],
            "done": False,
            "reply": None,
            "error": None,
        }
        with _call_lock:
            _call_entries[entry["call_id"]] = entry
        asyncio.ensure_future(_run_call_turn(entry, transcript))
        return {"call_id": entry["call_id"], "transcript": transcript}

    def _call_poll(call_id: str, after: int) -> dict | None:
        with _call_lock:
            entry = _call_entries.get(call_id)
            if entry is None:
                return None
            return {
                "sentences": [
                    base64.b64encode(w).decode("ascii")
                    for w in entry["sentences"][after:]
                ],
                "next": len(entry["sentences"]),
                "done": entry["done"],
                "reply": entry["reply"],
                "error": entry["error"],
                "transcript": entry["transcript"],
            }

    api = CoreApi(
        http=http,
        token=alarm.http_auth_token(),
        light=light_tool,
        alarm_tool=alarm_tool,
        clock=clock_tool,
        weather=weather_tool,
        announce=_announce,
        call_start=_call_start,
        call_poll=_call_poll,
        tv=tv_tool,
        tv_volume=tv_volume_tool,
    )
    await session.start()
    try:
        await alarm.start()
    except Exception:
        _LOGGER.exception(
            "alarm failed to start; continuing WITHOUT the alarm "
            "(the voice service stays up)"
        )
    await api.start()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, supervisor.stop)
        except NotImplementedError:
            pass
    _LOGGER.info("lyra core production starting (verify gate attached, LIVE filing)")
    sup_task = asyncio.create_task(supervisor.run(), name="lyra-supervisor")
    status_task = asyncio.create_task(
        _status_loop(session, supervisor), name="lyra-status"
    )
    try:
        await sup_task
    finally:
        await alarm.stop()
        status_task.cancel()
        await session.stop()
        http.stop()
    _LOGGER.info("lyra core production stopped")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
