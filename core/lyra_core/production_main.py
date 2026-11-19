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
- Modes (2026-11-16, Daniel's model): three exclusive modes --
  WakeGate (the room path above), RTC (a full-duplex WebRTC
  session with the TV page as the peer; lyra_core.rtc), and Sleep
  (no inputs at all: capture suspended, she is in bed). One source
  of truth, web/mode_state.json (lyra_core.mode): the dashboard
  writes it, a _ModeManager here applies it -- WakeGate runs the
  capture supervisor, RTC stops the capture and accepts offers,
  Sleep stops everything input-shaped. The mode persists across
  restarts EXCEPT rtc: a boot never has a peer, so a core that
  finds rtc persisted comes up in WakeGate and rewrites the file.
- SIGTERM/SIGINT -> shutdown: the mode manager stops the RTC
  server and the seat's supervisor; the only exit is the clean
  disconnect of the attached client.
- Transport is the core config's ONE switch, `transport`, and its
  only value is now "localmic" (2026-11-16): the session's client
  is a PipeWire capture of Fedora's default audio source, and her
  voice leaves via the TV mirror to Fedora's default sink. The
  speaker-box transports (ESPHome API, satlink) were removed with
  the box.
"""
from __future__ import annotations

import array
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
from . import mode as mode_store
from .rtc import RtcServer
from .status import StatusHub, StatusPublisher, health_monitor
from .stream_stt import StreamSttClient
from .session import VoiceSession
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


class _SupervisorSeat:
    """Owns the (single) capture supervisor so the mode manager can
    stop and restart the room capture as modes change. A fresh
    CoreSupervisor per WakeGate entry: CoreSupervisor.stop() is a
    one-way latch by design, and the clean client disconnect stays
    the only release path."""

    def __init__(self, cfg: CoreConfig, session: VoiceSession,
                 factory) -> None:
        self._cfg = cfg
        self._session = session
        self._factory = factory
        self.supervisor: CoreSupervisor | None = None
        self.task: asyncio.Task | None = None

    async def start(self) -> None:
        if self.task is not None:
            return
        self.supervisor = CoreSupervisor(
            self._cfg, self._session, client_factory=self._factory)
        self.task = asyncio.create_task(
            self.supervisor.run(), name="lyra-supervisor")
        self.task.add_done_callback(self._log_task_end)

    @staticmethod
    def _log_task_end(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("supervisor task ended with an exception",
                          exc_info=task.exception())

    async def stop(self) -> None:
        if self.task is None:
            return
        assert self.supervisor is not None
        self.supervisor.stop()
        await self.task
        self.task = None
        self.supervisor = None

    def stop_nowait(self) -> None:
        if self.supervisor is not None:
            self.supervisor.stop()


class _ModeManager:
    """Applies web/mode_state.json to the live composition.

    wakegate -> capture supervisor running, RTC offers refused.
    rtc      -> capture stopped; the RTC server accepts offers.
    sleep    -> capture stopped, RTC closed; no inputs at all.

    The dashboard writes the file; this manager polls it (the same
    file-driven pattern lyra-web uses for the room state) and runs
    the transitions serially."""

    def __init__(self, seat: _SupervisorSeat, rtc_server: RtcServer) -> None:
        self._seat = seat
        self._rtc = rtc_server
        self.current: str | None = None
        # Status hub (lyra_core.status), wired in amain; the
        # status light's sleep state is the APPLIED mode.
        self.status: StatusHub | None = None

    async def apply(self, mode: str) -> None:
        if mode == self.current:
            return
        previous, self.current = self.current, mode
        _LOGGER.info("mode: %s -> %s", previous, mode)
        if self.status is not None:
            self.status.set_mode(mode)
        if mode == mode_store.WAKEGATE:
            await self._rtc.stop()
            await self._seat.start()
        elif mode == mode_store.RTC:
            await self._seat.stop()
            self._rtc.accepting = True
        else:  # sleep
            await self._rtc.stop()
            await self._seat.stop()

    async def watch(self) -> None:
        while True:
            try:
                mode = mode_store.read_mode()
                if mode != self.current:
                    await self.apply(mode)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("mode watch failed")
            await asyncio.sleep(0.5)

    async def shutdown(self) -> None:
        await self._rtc.stop()
        await self._seat.stop()


async def _status_heartbeat(hub: StatusHub) -> None:
    """Repost the status snapshot on a slow cadence: lyra-web
    treats a core that has not pushed in ~35 s as offline (red
    "Core offline"), so a live core must keep pushing even when
    nothing changes."""
    while True:
        await asyncio.sleep(15.0)
        hub.republish()


async def _status_loop(session: VoiceSession, seat: _SupervisorSeat) -> None:
    while True:
        await asyncio.sleep(STATUS_PERIOD_S)
        client = session._box
        floor = session._ep.noise_floor
        sup = seat.supervisor
        _LOGGER.info(
            "status: mode=%s client_connected=%s session_state=%s "
            "wake_link_up=%s ep_noise_floor=%s connects=%d attempts=%d",
            mode_store.read_mode(),
            bool(client is not None and client.is_connected),
            session.state,
            session._wake_link_up,
            ("%.0f" % floor) if floor is not None else None,
            sup.successful_connects if sup is not None else 0,
            sup.connect_attempts if sup is not None else 0,
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
    # Daily conversation logging (Phase 0 inbox) REMOVED
    # 2026-11-17 at Daniel's order: no InboxLog is constructed
    # or wired anywhere -- the router and the alarm both
    # default to no inbox, so no daily log is written.
    # -- kiosk status light (2026-11-18, lyra_core.status) --
    # The hub aggregates activity (turns + the speech clock +
    # RTC playback), the applied mode and dependency health into
    # the snapshot lyra-web pushes to the TV page. WakeGate
    # thinking is the session's processing state, fed through
    # the session event hook; every TV-mirrored synthesis feeds
    # the speech clock through the driver's hook.
    hub = StatusHub()
    hub.subscribe(StatusPublisher())
    driver.on_speech = hub.note_speech
    _session_processing = [False]

    def _status_event_hook(kind: str, detail: dict) -> None:
        _event_hook(kind, detail)
        if kind == "state":
            st = detail.get("state")
            if st == "processing" and not _session_processing[0]:
                _session_processing[0] = True
                hub.turn_begin()
            elif st == "idle" and _session_processing[0]:
                _session_processing[0] = False
                hub.turn_end()

    session = VoiceSession(cfg, driver, http, verify_gate=gate, on_event=_status_event_hook)

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

    # The capture supervisor lives in a seat the mode manager
    # drives: WakeGate starts it, RTC and Sleep stop it (capture
    # suspended -- in Sleep she is not listening at all).
    seat = _SupervisorSeat(cfg, session, _localmic_factory)
    _LOGGER.info(
        "transport: localmic (Fedora default source; no satellite box)"
    )
    alarm = Alarm(cfg, box_getter=lambda: session._box, http=http)
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
    )
    session._on_command = router.handle
    _LOGGER.info("FREE MODE live: %s", router.describe())

    # -- modes + RTC (2026-11-16) --
    # The RTC server runs the same router (via="rtc"); the mode
    # manager owns when it may accept offers and when the room
    # capture runs. See lyra_core.mode for the state contract.
    rtc_server = RtcServer(driver, router.handle)
    # The phone call's own RTC server (2026-11-18): the same
    # session machinery as RTC mode, but not a mode -- always
    # accepting, via="call", its speaker mirroring to the TV (the
    # call feature's long-standing behavior), and no rtc_state
    # publishing. Daniel: the phone call is supposed to be duplex.
    call_rtc_server = RtcServer(
        driver, router.handle, via="call", label="call",
        mirror=True, publish_state=False)
    call_rtc_server.accepting = True
    rtc_server.status = hub
    call_rtc_server.status = hub
    mode_mgr = _ModeManager(seat, rtc_server)
    mode_mgr.status = hub

    async def _rtc_offer(sdp: str, sdp_type: str) -> dict:
        """RTC signaling gate: offers live and die with the mode."""
        if mode_mgr.current != mode_store.RTC:
            return {"error": "rtc mode is not active", "status": 409}
        return await rtc_server.handle_offer(sdp, sdp_type)

    async def _call_offer(sdp: str, sdp_type: str) -> dict:
        """Call signaling: NOT mode-gated. A phone call is not a
        mode -- it connects while the house is in WakeGate (the
        normal case) and never disturbs the TV page's RTC
        plumbing or the room capture."""
        return await call_rtc_server.handle_offer(sdp, sdp_type)

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
    # Since 2026-11-18 call turns transcribe through the Nemotron
    # streaming sidecar first (see _call_transcribe below), with
    # Parakeet TDT as the fallback -- Daniel, 2026-11-18: the
    # phone call is supposed to be duplex.
    # Later the same day the call became a true streaming session:
    # the page is a WebRTC peer of the call RTC server (see
    # _call_offer above) -- his words stream to the sidecar live
    # and render on the page as they finalize, her replies stream
    # sentence by sentence, and speech over her reply barges in.
    # The /call/start + /call/poll shape below remains as the
    # page's fallback when the streaming transport cannot be
    # established, and _call_transcribe remains its ears.
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
        finally:
            hub.turn_end()

    # -- call transcription: Nemotron streaming first (2026-11-18) --
    # RTC got the streaming sidecar this morning; call turns use
    # the same ears. The call API is per-utterance -- the phone
    # client VADs in the browser and uploads one complete WAV per
    # turn -- so each turn takes its OWN sidecar connection: the
    # recorded PCM is fed through the stream and the transcript
    # is the stream's flush final, the same finalization contract
    # RTC dispatches on. (RTC's live STREAM_CONFIRM_S window has
    # no analogue here: the utterance is already complete when it
    # arrives -- the client's silence cut is the turn end and the
    # flush is the final.) A healthy stream's final is
    # authoritative, empty included, mirroring RTC. Any failure
    # -- sidecar down, busy (the sidecar serves one connection at
    # a time and an RTC session holds its own), or dying
    # mid-turn -- falls back to driver.transcribe_audio (Parakeet
    # TDT) on the original PCM: the pre-streaming behavior, so a
    # dead or busy streamer can never break a call.
    _CALL_STREAM_CONNECT_S = 1.5

    def _call_stream_pcm(pcm: bytes, rate: int, width: int,
                         channels: int) -> bytes | None:
        """Normalize an utterance to the sidecar's contract format
        (s16le 16 kHz mono), stdlib only -- the core venv has no
        numpy. The phone client already records in this format,
        so in practice this is a pass-through; the conversions
        cover the plausible variants. None for a shape we cannot
        convert: the caller then goes straight to TDT, which
        takes the original format natively."""
        if width == 2:
            samples = array.array("h")
            samples.frombytes(pcm[: len(pcm) // 2 * 2])
        elif width == 1:
            samples = array.array("h", ((b - 128) << 8 for b in pcm))
        elif width == 4:
            wide = array.array("i")
            wide.frombytes(pcm[: len(pcm) // 4 * 4])
            samples = array.array("h", (v >> 16 for v in wide))
        else:
            return None
        if channels == 2:
            samples = array.array("h", (
                (samples[i] + samples[i + 1]) // 2
                for i in range(0, len(samples) - 1, 2)))
        elif channels != 1:
            return None
        if rate != 16000 and len(samples) > 1:
            ratio = rate / 16000.0
            out = array.array("h")
            for i in range(int(len(samples) / ratio)):
                pos = i * ratio
                j = int(pos)
                a = samples[j]
                b = samples[min(j + 1, len(samples) - 1)]
                out.append(int(a + (b - a) * (pos - j)))
            samples = out
        return samples.tobytes()

    async def _call_transcribe_stream(pcm16: bytes) -> str | None:
        """One utterance through the Nemotron streaming sidecar
        on its own connection: the flush final (possibly ''), or
        None if the sidecar could not serve the turn."""
        client = await StreamSttClient.connect(
            timeout=_CALL_STREAM_CONNECT_S)
        if client is None:
            return None
        try:
            chunk = 6400  # 200 ms of 16 kHz s16 mono
            for offset in range(0, len(pcm16), chunk):
                await client.feed(pcm16[offset:offset + chunk])
                if client.failed:
                    return None
            return await client.flush()
        finally:
            await client.close()

    async def _call_transcribe(pcm: bytes, rate: int, width: int,
                               channels: int) -> str:
        pcm16 = _call_stream_pcm(pcm, rate, width, channels)
        if pcm16:
            text = await _call_transcribe_stream(pcm16)
            if text is not None:
                text = text.strip()
                _LOGGER.info(
                    "call transcript [nemotron-stream]: %r", text)
                return text
            _LOGGER.warning(
                "call: stream sidecar could not serve the turn; "
                "falling back to Parakeet TDT")
        transcript = await driver.transcribe_audio(
            pcm, rate=rate, width=width, channels=channels)
        _LOGGER.info("call transcript [parakeet-tdt]: %r", transcript)
        return transcript

    async def _call_start(wav_bytes: bytes) -> dict:
        now = time.monotonic()
        with _call_lock:
            stale = [k for k, e in _call_entries.items()
                     if now - e["created"] > 600]
            for k in stale:
                del _call_entries[k]
            if any(not e["done"] for e in _call_entries.values()):
                return {"busy": True}
        # Status light: the call turn is thinking from here --
        # transcription (in this function) plus the router turn
        # (_run_call_turn ends it in its finally).
        hub.turn_begin()
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                rate = wf.getframerate()
                width = wf.getsampwidth()
                channels = wf.getnchannels()
                pcm = wf.readframes(wf.getnframes())
            transcript = await _call_transcribe(
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
        except Exception:
            hub.turn_end()
            raise
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
        rtc_offer=_rtc_offer,
        call_offer=_call_offer,
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
    # Boot mode: the persisted mode is honored EXCEPT rtc -- a boot
    # never has a peer, so a core that finds rtc persisted comes up
    # in WakeGate and rewrites the file (never strand her deaf).
    persisted = mode_store.read_mode()
    if persisted == mode_store.RTC:
        _LOGGER.warning(
            "mode persisted as rtc at boot; coercing to wakegate "
            "(no peer can exist yet)")
        mode_store.write_mode(mode_store.WAKEGATE)
        persisted = mode_store.WAKEGATE
    await mode_mgr.apply(persisted)
    hub.republish()
    _shutdown = asyncio.Event()

    def _request_shutdown() -> None:
        seat.stop_nowait()
        _shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _request_shutdown)
        except NotImplementedError:
            pass
    _LOGGER.info("lyra core production starting (verify gate attached, LIVE filing)")
    watch_task = asyncio.create_task(mode_mgr.watch(), name="lyra-mode-watch")
    status_task = asyncio.create_task(
        _status_loop(session, seat), name="lyra-status"
    )
    health_task = asyncio.create_task(
        health_monitor(hub), name="lyra-health")
    heartbeat_task = asyncio.create_task(
        _status_heartbeat(hub), name="lyra-status-heartbeat")
    try:
        await _shutdown.wait()
    finally:
        watch_task.cancel()
        health_task.cancel()
        heartbeat_task.cancel()
        await call_rtc_server.stop()
        await mode_mgr.shutdown()
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
