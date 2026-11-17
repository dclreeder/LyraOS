"""Production entry point for the Lyra core (bold cutover, 2026-11-11).

Composes the existing pieces exactly as the benches do — AudioHttpServer,
PipelineDriver, VoiceSession, CoreSupervisor — and runs them as the
box's long-lived controller. Composition only: no bench/session/pipeline
code is modified.

- The Phase 2 VerifyGate is attached in LIVE filing mode (flipped
  2026-11-11 with the spool double-file fix): verify-then-fire gating
  is live (fail/maybe turns end silently) and verified clips are
  filed to the training pool by the production collector. After each
  verified turn the gate claims + discards its own wake-server spool
  entry, so the spool janitor never files a core-handled turn twice.
- One-time cutover announcement: after the first successful box connect
  plus 30 s of streaming, the line below is synthesized through Kokoro
  and played via BoxClient.announce (the core's own announce path),
  exactly once. A sentinel file suppresses any repeat on later restarts,
  and the attempt is made once even if it fails (no second audible try).
- The Fedora-native alarm (Phase 3) runs in this composition:
  persisted state + per-minute scheduler with the catch-up rule,
  Shelly light, 15-minute hard stop, snooze, and the authenticated
  HTTP kill endpoint for Daniel's iOS Action Button. Since
  2026-11-15 the ring sounds through the TV (TvRinger: display on,
  TV woken + volume-managed over ADB, the web page loops the tone
  and acks); the box ring remains as the fallback path.
- The LLM router is the session's on_command hook: the shared
  local LLM (lyra-llm, Qwen3-4B-Instruct-2507) classifies each
  verified command into a closed intent set under a JSON-schema
  grammar; the router re-validates the slots, and the
  readback-verified tools (light / monitor / alarm / clock /
  weather) speak short confirmations from the actual outcome
  (D2) -- model text is never spoken. Garbled commands get the
  one D5 "didn't catch that" line; no-match commands (including
  any turn where the LLM is down) get the spoken
  "I don't understand." line.
- The Phase 4 core API (lyra_core/api.py) is Lyra's hands on this
  same HTTP endpoint: POST /tool executes the router's own tool
  objects and returns their verified ToolResults as JSON, and
  POST /announce speaks a statement through the box. Both share
  the alarm kill endpoint's token.
- Morning note (2026-11-12): the alarm can hold a one-shot,
  date-scoped spoken note; the composition wires _announce_via_box
  as the alarm's note speaker, so a morning kill speaks the note
  right after the ring ends. Notes are set through the API
  (alarm note_set), not the voice router.
- Firmware updates ride the same API (tool "firmware"): on the
  satlink transport the core pushes an image to the box over the
  sat link (background task + status action in the API; the push
  itself is SatBoxClient.ota_push). The satlink factory wires the
  client's busy gate to this composition's alarm + session, so a
  push is refused while the alarm rings or a voice turn runs.
- SIGTERM/SIGINT -> supervisor.stop(): the only exit is the clean
  unsubscribe-then-disconnect seat release.
- Box transport is chosen by the core config's ONE switch,
  `transport`: "esphome" (default; BoxClient over the ESPHome API)
  or "satlink" (Stage 1, 2026-11-11: SatBoxClient listens for the
  custom sat7 firmware and streams PCM both ways). Only the box
  factory differs; everything downstream is shared.
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
from pathlib import Path

from .alarm import Alarm, ShellyLight
from .api import AnnounceUnavailable, CoreApi
from .audio_http import AudioHttpServer
from .config import CoreConfig
from .pipeline import PipelineDriver, speech_route
from .free_router import FreeRouter
from .satlink import SatBoxClient
from .localmic import LocalMicClient
from .session import VoiceSession
from .inbox import InboxLog
from .tv_ring import TvRinger
from .tools import (
    AlarmTool,
    ClockTool,
    FirmwareTool,
    LightTool,
    MediaVolumeTool,
    MonitorTool,
    TvTool,
    TvVolumeTool,
    WeatherTool,
)
from .supervisor import CoreSupervisor
from .verify import VerifyGate

_LOGGER = logging.getLogger("lyra_core.production")

ANNOUNCE_TEXT = "Lyra core is live. Home Assistant is off."
SENTINEL = Path(__file__).resolve().parent.parent / ".cutover-announce-done"
ANNOUNCE_DELAY_S = 30.0
STATUS_PERIOD_S = 15.0


def _event_hook(kind: str, detail: dict) -> None:
    _LOGGER.info("session event: %s %s", kind, detail)


async def _status_loop(session: VoiceSession, supervisor: CoreSupervisor) -> None:
    while True:
        await asyncio.sleep(STATUS_PERIOD_S)
        box = session._box
        floor = session._ep.noise_floor
        _LOGGER.info(
            "status: box_connected=%s session_state=%s wake_link_up=%s "
            "ep_noise_floor=%s connects=%d attempts=%d",
            bool(box is not None and box.is_connected),
            session.state,
            session._wake_link_up,
            ("%.0f" % floor) if floor is not None else None,
            supervisor.successful_connects,
            supervisor.connect_attempts,
        )


async def _announce_once(
    session: VoiceSession,
    driver: PipelineDriver,
    http: AudioHttpServer,
    cfg: CoreConfig,
) -> None:
    if SENTINEL.exists():
        _LOGGER.info("cutover announcement already done (sentinel present); skipping")
        return
    while True:
        box = session._box
        if box is not None and box.is_connected:
            break
        await asyncio.sleep(1.0)
    _LOGGER.info("box connected; cutover announcement in %.0fs", ANNOUNCE_DELAY_S)
    await asyncio.sleep(ANNOUNCE_DELAY_S)
    box = session._box
    if box is None or not box.is_connected:
        _LOGGER.error("cutover announcement aborted: box not connected")
        SENTINEL.write_text("aborted: box not connected\n")
        return
    try:
        wav_bytes, _rate, _width, _ch = await driver.synthesize_speech(ANNOUNCE_TEXT)
        audio_id = http.store.put(wav_bytes)
        url = http.url_for(audio_id, cfg.http.public_host)
        _LOGGER.info(
            "cutover announcement: playing %s (%d bytes)", url, len(wav_bytes)
        )
        result = await box.announce(url, text=ANNOUNCE_TEXT, timeout=60.0)
        _LOGGER.info("cutover announcement finished: %r", result)
        SENTINEL.write_text("played\n")
    except Exception as exc:  # one attempt only — never a second audible try
        _LOGGER.error("cutover announcement failed: %s", exc)
        SENTINEL.write_text(f"failed: {exc}\n")


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
    # OTA busy gate for the satlink client: the factory below only
    # runs under the supervisor, after the whole composition
    # exists, so the alarm (created further down) is filled into
    # this holder before the first client is ever built.
    ota_gate: dict = {"alarm": None}

    def _ota_busy_reason() -> str | None:
        alarm_obj = ota_gate.get("alarm")
        if alarm_obj is not None and alarm_obj.summary().get("ringing"):
            return "the alarm is ringing"
        if session.state != "idle":
            return "a voice turn is in progress"
        return None

    if cfg.transport == "satlink":
        # Custom-firmware link: the core listens, the box connects.
        # SatBoxClient is duck-typed to BoxClient, so the session,
        # alarm and announce path are transport-agnostic.
        def _satlink_factory() -> SatBoxClient:
            return SatBoxClient(
                cfg,
                on_audio=session.handle_audio,
                on_start=session.handle_start,
                on_stop=session.handle_stop,
                audio_store=http.store,
                busy_check=_ota_busy_reason,
            )

        supervisor = CoreSupervisor(cfg, session, box_factory=_satlink_factory)
        _LOGGER.info(
            "box transport: satlink (listening on %s:%d)",
            cfg.satlink.bind_host, cfg.satlink.port,
        )
    elif cfg.transport == "localmic":
        # Box-free transport (2026-11-16, Daniel: "act like the box
        # is gone"): the session's ears are a PipeWire capture of
        # Fedora's default audio source, and her voice already
        # leaves via the TV mirror -> Fedora's default sink. No
        # satlink listener is opened; the satellite code stays in
        # the tree, unused, one config value away.
        def _localmic_factory() -> LocalMicClient:
            return LocalMicClient(
                cfg,
                on_audio=session.handle_audio,
                on_start=session.handle_start,
                on_stop=session.handle_stop,
                audio_store=http.store,
            )

        supervisor = CoreSupervisor(cfg, session, box_factory=_localmic_factory)
        _LOGGER.info(
            "box transport: localmic (Fedora default source; no satellite box)"
        )
    elif cfg.transport == "esphome":
        supervisor = CoreSupervisor(cfg, session)
        _LOGGER.info("box transport: esphome")
    else:
        raise ValueError(f"unknown box transport {cfg.transport!r}")
    alarm = Alarm(cfg, box_getter=lambda: session._box, http=http)
    alarm._inbox = inbox
    # TV sounder (2026-11-15): the alarm rings through the TV; the
    # box path remains as the alarm's own fallback.
    alarm.set_tv_ringer(TvRinger())
    ota_gate["alarm"] = alarm
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
    monitor_tool = MonitorTool()
    tv_tool = TvTool()
    tv_volume_tool = TvVolumeTool()
    alarm_tool = AlarmTool(alarm)
    # The everyday box volume rides the same box_getter as the
    # alarm (the supervisor's current connection, either transport).
    media_volume_tool = MediaVolumeTool(box_getter=lambda: session._box)
    clock_tool = ClockTool(cfg.router.timezone)
    weather_tool = WeatherTool(
        cfg.router.weather_latitude,
        cfg.router.weather_longitude,
        cfg.router.timezone,
    )
    # Firmware updates (satlink OTA): gated on this composition's
    # alarm + session, pushing through the supervisor's current box.
    firmware_tool = FirmwareTool(
        box_getter=lambda: session._box,
        alarm=alarm_tool,
        session=session,
    )
    router = FreeRouter(
        cfg.router,
        light=light_tool,
        monitor=monitor_tool,
        alarm_tool=alarm_tool,
        media_volume=media_volume_tool,
        clock=clock_tool,
        weather=weather_tool,
        tv=tv_tool,
        tv_volume=tv_volume_tool,
        inbox=inbox,
    )
    session._on_command = router.handle
    _LOGGER.info("FREE MODE live: %s", router.describe())

    async def _announce_via_box(text: str) -> None:
        """Phase 4 /announce: Kokoro -> audio URL -> box announce.
        Output gating (2026-11-15): on the TV route the line reaches
        the TV through synthesize_speech's mirror and the box is never
        asked to play it (its connection is not even required); on the
        box route, announce as before."""
        if speech_route() == "tv":
            await driver.synthesize_speech(text)
            return
        box = session._box
        if box is None or not box.is_connected:
            raise AnnounceUnavailable("speaker box is not connected")
        wav_bytes, _rate, _width, _ch = await driver.synthesize_speech(text)
        audio_id = http.store.put(wav_bytes)
        url = http.url_for(audio_id, cfg.http.public_host)
        result = await box.announce(url, text=text, timeout=60.0)
        if getattr(result, "success", None) is False:
            raise RuntimeError("box reported announce failure")

    # Morning note: the alarm speaks its stored note through this
    # same announce path when a morning kill ends the ring.
    alarm.set_note_speaker(_announce_via_box)

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
        monitor=monitor_tool,
        alarm_tool=alarm_tool,
        media_volume=media_volume_tool,
        clock=clock_tool,
        weather=weather_tool,
        announce=_announce_via_box,
        call_start=_call_start,
        call_poll=_call_poll,
        firmware=firmware_tool,
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
    announce_task = asyncio.create_task(
        _announce_once(session, driver, http, cfg), name="lyra-announce"
    )
    try:
        await sup_task
    finally:
        await alarm.stop()
        status_task.cancel()
        announce_task.cancel()
        await session.stop()
        http.stop()
    _LOGGER.info("lyra core production stopped (clean seat release)")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
