"""Production entry point for the Lyra core (bold cutover, 2026-11-11).

Composes the existing pieces exactly as the benches do — AudioHttpServer,
PipelineDriver, VoiceSession, CoreSupervisor — and runs them as the
long-lived controller. Composition only: no bench/session/pipeline
code is modified.

- The Phase 2 VerifyGate is attached in LIVE filing mode (flipped
  2026-11-11 with the spool double-file fix): verify-then-fire gating
  is live (fail/maybe turns end silently) and verified clips are
  filed to the training pool by the production collector. After each
  verified turn the gate discards its own pending-detection registry
  entry, so the stale sweep never files a core-handled turn twice.
- The Fedora-native alarm (Phase 3) runs in this composition:
  persisted state + per-minute scheduler with the catch-up rule,
  Shelly light, 15-minute hard stop, snooze, and the authenticated
  HTTP kill endpoint for Daniel's iOS Action Button. Since
  2026-11-19 the core plays the tone itself through Fedora's
  default sink (SinkRinger: paplay at the sink's own
  volume) -- no ADB, no TV volume save/restore, no browser
  ring.
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
  (no inputs at all: capture suspended, she is in bed). The core
  owns the mode state (2026-11-19): persisted in
  core/mode_state.json (lyra_core.mode), changed only through the
  core API's POST /mode, applied by the _ModeManager here --
  WakeGate runs the capture supervisor, RTC stops the capture and
  accepts offers, Sleep stops everything input-shaped. Everyone
  else consumes the mode from the status hub's pushed snapshot.
  The mode persists across
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

import asyncio
import logging
import signal
import sys
from pathlib import Path

from .alarm import Alarm, ShellyLight
from .api import CoreApi
from .audio_http import AudioHttpServer
from .config import CoreConfig
from .pipeline import PipelineDriver
from .free_router import FreeRouter
from .inbox import InboxLog
from .localmic import LocalMicClient
from . import mode as mode_store
from .rtc import RtcServer
from .status import StatusHub, StatusPublisher, health_monitor
from .session import VoiceSession
from .sink_ring import SinkRinger
from .tools import (
    AlarmTool,
    ClockTool,
    JellyfinTool,
    LightTool,
    TvTool,
    TvVolumeTool,
    WeatherTool,
)
from .supervisor import CoreSupervisor
from .verify import VerifyGate, finalize_pending_detection
from .wake import run_detection_sweep

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
    """Owns the mode state and applies it to the live composition.

    wakegate -> capture supervisor running, RTC offers refused.
    rtc      -> capture stopped; the RTC server accepts offers.
    sleep    -> capture stopped, RTC closed; no inputs at all.

    The core is the state file's only writer (2026-11-19): mode
    changes arrive over the core API and land in set_mode below,
    which persists the choice and runs the transition serially.
    Before this, the dashboard wrote web/mode_state.json and this
    manager polled it every 0.5 s."""

    def __init__(self, seat: _SupervisorSeat, rtc_server: RtcServer) -> None:
        self._seat = seat
        self._rtc = rtc_server
        self.current: str | None = None
        self._lock = asyncio.Lock()
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

    async def set_mode(self, mode: str) -> str:
        """Persist + apply a mode change (the core API's entry
        point). write_mode validates the mode name first -- an
        unknown mode raises ValueError before anything is applied.
        Transitions are serialized under the lock: they stop and
        start whole subsystems and must never interleave."""
        async with self._lock:
            mode_store.write_mode(mode)
            await self.apply(mode)
            return self.current

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


async def _status_loop(session: VoiceSession, seat: _SupervisorSeat,
                       mode_mgr: _ModeManager) -> None:
    while True:
        await asyncio.sleep(STATUS_PERIOD_S)
        client = session._box
        floor = session._ep.noise_floor
        sup = seat.supervisor
        _LOGGER.info(
            "status: mode=%s client_connected=%s session_state=%s "
            "wake_ready=%s ep_noise_floor=%s connects=%d attempts=%d",
            mode_mgr.current,
            bool(client is not None and client.is_connected),
            session.state,
            session._wake_ready,
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
    # Daily conversation logging (Phase 0 of the memory build,
    # LyraOS/memory/PLAN.md): the code-owned record. The inbox
    # logs every completed turn plus alarm events; the notebook
    # is the parallel per-day stream for the running brain's
    # notes (proposals + corrections -- the memory_note tool
    # that writes them is Phase 3). Both are append-only,
    # fail-silent, and read by nothing yet: the sleep curator
    # that consumes them is Phase 2. Removed 2026-11-17 at
    # Daniel's order; rebuilt 2026-11-21 at his order as the
    # first phase of the approved memory plan.
    inbox = InboxLog()
    notebook = InboxLog("~/Documents/LyraOS/memory/notes")
    Path("~/Documents/LyraOS/memory/notes").expanduser().mkdir(
        parents=True, exist_ok=True)
    # -- kiosk status light (2026-11-18, lyra_core.status) --
    # The hub aggregates activity (turns + the speech clock +
    # RTC playback), the applied mode and dependency health into
    # the snapshot lyra-web pushes to the TV page. WakeGate
    # thinking is the session's processing state and listening
    # its capturing state, both fed through the session event
    # hook; every TV-mirrored synthesis feeds the speech clock
    # through the driver's hook.
    hub = StatusHub()
    hub.subscribe(StatusPublisher())
    driver.on_speech = hub.note_speech
    # Collector finalize (2026-11-19): the STT bridge finalized
    # spooled wake detections after each transcription; the core
    # is the transcriber now, so the driver's hook runs the same
    # finalize in-process (lyra_core.verify) over the pending
    # registry (lyra_core.wake).
    driver.on_transcribed = finalize_pending_detection
    _session_processing = [False]
    _capture_speech = [False]

    def _status_event_hook(kind: str, detail: dict) -> None:
        _event_hook(kind, detail)
        if kind == "vad_start":
            _capture_speech[0] = True
        elif kind == "verify":
            # A speechless capture (the wake fired, he said
            # nothing) becomes a real turn only here: the gate
            # passed a wake-only clip, the router takes the empty
            # command, and she answers. Begin the turn at the
            # verdict; a fail drops silently in the session and
            # never shows thinking.
            if detail.get("verdict") == "pass" and not _session_processing[0]:
                _session_processing[0] = True
                hub.turn_begin()
                hub.set_listening("wakegate", False)
        elif kind == "silent_drop":
            hub.set_listening("wakegate", False)
        elif kind == "state":
            st = detail.get("state")
            if st == "capturing":
                # Gate open: she is taking in his command -- the
                # status light's listening state, from here until
                # the capture endpoints (processing) or the run
                # dies back to idle.
                _capture_speech[0] = False
                hub.set_listening("wakegate", True)
            elif st == "processing":
                # Thinking is a captured command being worked on.
                # With captured speech the turn starts now (the
                # listening clear rides after turn_begin, so the
                # light hands pink to amber with no idle flicker).
                # Without it, the gate is still deciding whether
                # the wake was real: listening holds until the
                # verdict -- a pass begins the turn above, a fail
                # ends the run at silent_drop.
                if not _session_processing[0] and _capture_speech[0]:
                    _session_processing[0] = True
                    hub.turn_begin()
                    hub.set_listening("wakegate", False)
            elif st == "idle":
                hub.set_listening("wakegate", False)
                if _session_processing[0]:
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
        "transport: localmic (echo-cancelled source, pinned by node name; no satellite box)"
    )
    alarm = Alarm(cfg, http=http)
    # Default-sink sounder (2026-11-19): the core plays the tone
    # itself through Fedora's default sink, at the sink's own
    # volume; no device volume is touched.
    alarm.set_ringer(SinkRinger(cfg.alarm.tone_path))
    alarm._inbox = inbox
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
    jellyfin_tool = JellyfinTool()
    router = FreeRouter(
        cfg.router,
        light=light_tool,
        alarm_tool=alarm_tool,
        clock=clock_tool,
        weather=weather_tool,
        tv=tv_tool,
        tv_volume=tv_volume_tool,
        jellyfin=jellyfin_tool,
        inbox=inbox,
        notebook=notebook,
    )
    session._on_command = router.handle
    _LOGGER.info("FREE MODE live: %s", router.describe())

    # -- modes + RTC (2026-11-16) --
    # The RTC server runs the same router (via="rtc"); the mode
    # manager owns when it may accept offers and when the room
    # capture runs. See lyra_core.mode for the state contract.
    rtc_server = RtcServer(driver, router.handle)
    rtc_server.status = hub
    mode_mgr = _ModeManager(seat, rtc_server)
    mode_mgr.status = hub

    async def _rtc_offer(sdp: str, sdp_type: str) -> dict:
        """RTC signaling gate: offers live and die with the mode."""
        if mode_mgr.current != mode_store.RTC:
            return {"error": "rtc mode is not active", "status": 409}
        return await rtc_server.handle_offer(sdp, sdp_type)

    async def _announce(text: str) -> None:
        """Phase 4 /announce: Kokoro -> TV mirror. synthesize_speech
        fires the mirror itself (speech_route is unconditionally the
        TV since 2026-11-15), so announcing is just synthesizing
        the line."""
        await driver.synthesize_speech(text)


    # Morning note: the alarm speaks its stored note through this
    # same announce path when a morning kill ends the ring.
    alarm.set_note_speaker(_announce)

    async def _mode_set(mode: str) -> str:
        """POST /mode: the manager persists + applies the mode."""
        return await mode_mgr.set_mode(mode)

    def _mode_get() -> str:
        """GET /mode: the currently applied mode."""
        return mode_mgr.current or mode_store.DEFAULT_MODE


    api = CoreApi(
        http=http,
        token=alarm.http_auth_token(),
        light=light_tool,
        alarm_tool=alarm_tool,
        clock=clock_tool,
        weather=weather_tool,
        announce=_announce,
        rtc_offer=_rtc_offer,
        mode_set=_mode_set,
        mode_get=_mode_get,
        tv=tv_tool,
        tv_volume=tv_volume_tool,
        jellyfin=jellyfin_tool,
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
    status_task = asyncio.create_task(
        _status_loop(session, seat, mode_mgr), name="lyra-status"
    )
    health_task = asyncio.create_task(
        health_monitor(hub), name="lyra-health")
    heartbeat_task = asyncio.create_task(
        _status_heartbeat(hub), name="lyra-status-heartbeat")
    # Collector stale sweep: files detections no transcription ever
    # claimed, wake-only, past the 120 s window (the deleted wake
    # server's janitor, in-core since the gate moved in).
    sweep_task = asyncio.create_task(
        run_detection_sweep(), name="lyra-wake-sweep")
    try:
        await _shutdown.wait()
    finally:
        health_task.cancel()
        heartbeat_task.cancel()
        sweep_task.cancel()
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
