"""Voice session: composes the box client and the Wyoming pipeline
driver into one turn-owning module (Phase 1c).

Flow (HA's pipeline order and timing):

  box mic stream --(this session)--> Wyoming wake server (continuous)
  Detection -> RUN_START, STT_START -> capture the utterance from the
  stream, endpointed by the core-side endpointer (STT_VAD_START /
  STT_VAD_END as speech starts / the ~700 ms hangover expires)
  -> STT_END (transcript) -> INTENT_START/END -> TTS_START (HTTP URL of
  the reply, served by audio_http) -> TTS_END -> RUN_END.

Phase scope notes:
- The reply is a FIXED line (CANNED_REPLY) unless an on_command hook
  returns one. Phase 3: the hook is the phrase router -- it returns
  the tool's spoken confirmation, the D5 "didn't catch that" line,
  or the no-match "I don't understand." line.
  The canned line survives only as the no-hook / hook-declines
  fallback, and on the gate-free Phase 1 path.
- Phase 2 verify-then-fire: when a verify_gate is attached, the
  combined wake+command clip is transcribed directly via whisper.cpp
  (the production collector's own call) and its classifier gates the
  turn — FAIL/MAYBE end the run silently (no TTS, no reply URL), PASS
  hands the wake-stripped command to on_command. Without a gate, the
  Phase 1 path (Wyoming STT transcript -> canned reply) is unchanged,
  exactly as HA's seat-swap proof requires (only the controller
  changes).
- No wake ack (removed 2026-11-16, Daniel's order): a Detection
  used to trigger a box earcon, plus a guard that kept the
  earcon's echo from endpointing the capture. Both are gone;
  capture endpoints on the speaker's VAD alone.
- An empty transcript ends the run as ERROR "stt-no-text-recognized"
  (the firmware ignores that code: no error LED phase), mirroring HA.
- While a run is active the mic stream is NOT forwarded to the wake
  server (HA diverts the stream to the STT stage the same way), and
  further Detections are ignored — the core-side equivalent of the
  firmware's duplicate_wake_up_detected guard.
- The utterance sent to STT includes a short pre-roll (audio from just
  before the Detection) so command onsets are not clipped.

Nothing here connects anywhere by itself: the supervisor (or the bench)
attaches a box client and calls start(). Stream format is the design's
16 kHz / 16-bit / mono API audio (Phase 1a design SS3).
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from datetime import datetime
from collections import deque
from collections.abc import Awaitable, Callable

from .audio_http import AudioHttpServer
from .localmic import LocalMicClient
from .config import CoreConfig
from .endpointer import VAD_END, VAD_START, Endpointer
import io
import wave
from .pipeline import PipelineDriver, PipelineError, silent_wav_like, speech_route
from .wyoming_client import WyomingConnection, audio_chunk, audio_start, detect

_LOGGER = logging.getLogger("lyra_core.session")

RATE, WIDTH, CHANNELS = 16000, 2, 1
CANNED_REPLY = "Lyra core bench check complete."

# VA error code reported through evt_error when a captured turn
# produced no speech (the firmware-era code name, kept so event
# consumers see the same string they always have).
ERROR_STT_NO_TEXT = "stt-no-text-recognized"


# Observer hook: (kind, detail) — kinds: wake_forward (first chunk),
# detection, vad_start, vad_end, verify, silent_drop,
# transcript, tts_url, box_event, state.
EventHook = Callable[[str, dict], None]

STATE_IDLE = "idle"
STATE_CAPTURING = "capturing"
STATE_PROCESSING = "processing"


class _SentenceSpeaker:
    """Streaming speech for one turn (2026-11-15): the router feeds
    reply sentences while the rest of the reply is still generating;
    each is synthesized in order -- synthesize_speech fires the TV
    mirror per sentence -- so the first sentence is heard after
    roughly one sentence of generation instead of the whole reply
    plus the whole synthesis. Synthesis runs in a worker task so
    TTS never blocks the token stream. The box still receives one
    silent WAV of the reply's exact total length, as before."""

    def __init__(self, driver) -> None:
        self._driver = driver
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self.used = False
        self.total_frames = 0
        self.params = None

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
                wav_bytes, _r, _w, _c = await self._driver.synthesize_speech(sentence)
            except Exception:
                _LOGGER.exception(
                    "streamed sentence synthesis failed: %r", sentence)
                continue
            try:
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    self.total_frames += wf.getnframes()
                    self.params = wf.getparams()
            except Exception:
                pass

    async def drain(self) -> None:
        if self._worker is not None:
            await self._queue.put(None)
            await self._worker
            self._worker = None

    def silence_wav(self) -> bytes | None:
        """One silent WAV of the total spoken length (+ a small tail
        pad), in the format of the synthesized sentences. None when
        nothing was actually synthesized (caller falls back to the
        whole-reply path)."""
        if self.params is None or self.total_frames <= 0:
            return None
        frames = self.total_frames + int(self.params.framerate * 0.4)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setparams(self.params)
            wf.writeframes(
                b"\x00" * frames * self.params.sampwidth * self.params.nchannels)
        return buf.getvalue()


class VoiceSession:
    def __init__(
        self,
        config: CoreConfig,
        driver: PipelineDriver,
        http_server: AudioHttpServer,
        *,
        reply_text: str = CANNED_REPLY,
        on_event: EventHook | None = None,
        endpointer: Endpointer | None = None,
        preroll_s: float = 0.5,
        no_speech_timeout_s: float = 6.0,
        max_capture_s: float = 30.0,
        verify_gate=None,
        on_command: Callable[[str], object] | None = None,
        clip_ring_s: float = 2.5,
    ) -> None:
        self._cfg = config
        self._driver = driver
        self._http = http_server
        self._reply_text = reply_text
        self._on_event = on_event
        self._ep = endpointer or Endpointer()
        self._no_speech_timeout_s = no_speech_timeout_s
        self._max_capture_s = max_capture_s
        self._preroll_max = int(RATE * WIDTH * CHANNELS * preroll_s)
        self._preroll: deque[bytes] = deque()
        self._preroll_bytes = 0
        # Phase 2 verify-then-fire (duck-typed verify.VerifyGate; None
        # keeps the Phase 1 behavior). The clip ring holds the trailing
        # clip_ring_s of idle audio; at a Detection it is snapshotted as
        # the combined clip's wake prefix — the collector's clip shape.
        self._verify_gate = verify_gate
        # Spool-name second of the accepted Detection (item N);
        # stamped in _on_detection, consumed by _verify_stage.
        self._detection_spool_ts: str | None = None
        self._on_command = on_command
        self._clip_ring_max = int(RATE * WIDTH * CHANNELS * clip_ring_s)
        self._clip_ring: deque[bytes] = deque()
        self._clip_ring_bytes = 0
        self._capture_started_at: float | None = None
        self._clip_prefix = b""
        self._utterance_preroll_len = 0
        self.last_verify = None
        self._box: LocalMicClient | None = None
        self._state = STATE_IDLE
        self._utterance = bytearray()
        self._vad_started = False
        self._capture_deadline: asyncio.Task | None = None
        self._process_task: asyncio.Task | None = None
        self._wake_conn: WyomingConnection | None = None
        self._wake_task: asyncio.Task | None = None
        self._wake_link_up = False
        self._forward_notified = False
        self.last_transcript: str | None = None
        self.last_tts_url: str | None = None

    # -- wiring --------------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    def attach_box(self, box: LocalMicClient) -> None:
        """Attach the (freshly connected) client."""
        self._box = box
        self._set_state(STATE_IDLE)
        _LOGGER.info("client attached; session idle")

    def detach_box(self) -> None:
        """Connection lost or supervisor stopping: drop any live run."""
        self._cancel_capture_tasks()
        self._ep.end_capture()
        self._box = None
        self._set_state(STATE_IDLE)
        _LOGGER.info("client detached; session reset to idle")

    def make_client(self) -> LocalMicClient:
        """Composition factory: a LocalMicClient whose callbacks land here."""
        return LocalMicClient(
            self._cfg,
            on_audio=self.handle_audio,
            on_start=self.handle_start,
            on_stop=self.handle_stop,
        )

    # -- lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Open the wake-server link (Detect + AudioStart) and read it."""
        if self._wake_task is not None:
            return
        self._wake_task = asyncio.create_task(
            self._wake_link_loop(), name="lyra-wake-link"
        )

    async def stop(self) -> None:
        """Stop the wake link. Box disconnect is the supervisor's job
        (unsubscribe-then-disconnect is the only seat-release path)."""
        if self._wake_task is not None:
            self._wake_task.cancel()
            try:
                await self._wake_task
            except asyncio.CancelledError:
                pass
            self._wake_task = None
        self._cancel_capture_tasks()

    async def _wake_link_loop(self) -> None:
        """Maintain the wake connection: connect, read Detections, and
        reconnect with backoff if the service link drops."""
        cfg = self._cfg.wyoming
        backoff = 1.0
        while True:
            try:
                async with await WyomingConnection.connect(cfg.wake_uri) as conn:
                    self._wake_conn = conn
                    self._wake_link_up = True
                    backoff = 1.0
                    await conn.send(detect([cfg.wake_name]))
                    await conn.send(audio_start(RATE, WIDTH, CHANNELS))
                    _LOGGER.info("wake link up (%s)", cfg.wake_uri)
                    while True:
                        event = await conn.receive()
                        if event is None:
                            break
                        if event.type == "detection":
                            self._on_detection(event.data)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # link failure: log, back off, retry
                _LOGGER.warning("wake link error: %s", exc)
            finally:
                self._wake_conn = None
                self._wake_link_up = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    # -- box callbacks -----------------------------------------------------------

    async def handle_start(
        self, conversation_id: str, wake_word_phrase: str | None
    ) -> None:
        # Device-initiated VA start (e.g. a physical control). In streamed
        # mode runs are Detection-driven; nothing to do beyond logging.
        _LOGGER.info(
            "box VA start conversation=%s wake=%r",
            conversation_id, wake_word_phrase,
        )

    async def handle_stop(self, abort: bool) -> None:
        _LOGGER.info("box VA stop abort=%s", abort)
        if self._state != STATE_IDLE:
            self._capture_started_at = None
            self._cancel_capture_tasks()
            self._ep.end_capture()
            self._emit_box("run_end", self._box.evt_run_end if self._box else None)
            self._set_state(STATE_IDLE)

    async def handle_audio(self, data: bytes) -> None:
        """One chunk of box mic audio. Must stay cheap: it runs on the
        API client's callback path."""
        if not data:
            return
        if self._state == STATE_IDLE:
            # Clip ring (Phase 2): trailing idle audio; a Detection
            # snapshots it as the combined clip's wake prefix.
            self._clip_ring.append(data)
            self._clip_ring_bytes += len(data)
            while self._clip_ring_bytes > self._clip_ring_max and len(self._clip_ring) > 1:
                self._clip_ring_bytes -= len(self._clip_ring.popleft())
            # Pre-roll ring (command onsets survive the Detection cut).
            self._preroll.append(data)
            self._preroll_bytes += len(data)
            while self._preroll_bytes > self._preroll_max and len(self._preroll) > 1:
                self._preroll_bytes -= len(self._preroll.popleft())
            # Floor tracking only while idle; events are capture-scoped.
            self._ep.feed(data)
            await self._forward_to_wake(data)
        elif self._state == STATE_CAPTURING:
            self._utterance.extend(data)
            for vad_event in self._ep.feed(data):
                if vad_event == VAD_START:
                    self._vad_started = True
                    self._notify("vad_start", {})
                    self._emit_box(
                        "stt_vad_start", self._box.evt_stt_vad_start if self._box else None
                    )
                elif vad_event == VAD_END:
                    self._notify("vad_end", {})
                    self._emit_box(
                        "stt_vad_end", self._box.evt_stt_vad_end if self._box else None
                    )
                    self._finish_capture()
        # PROCESSING: audio is dropped (HA diverts it to the STT stage;
        # here the utterance is already captured).

    async def _forward_to_wake(self, data: bytes) -> None:
        conn = self._wake_conn
        if conn is None or not self._wake_link_up:
            return
        try:
            await conn.send(audio_chunk(RATE, WIDTH, CHANNELS, data))
        except Exception as exc:  # the link loop will notice and reconnect
            _LOGGER.debug("wake forward failed: %s", exc)
            return
        if not self._forward_notified:
            self._forward_notified = True
            self._notify("wake_forward", {"bytes": len(data)})

    # -- run choreography ---------------------------------------------------------

    def _on_detection(self, data: dict) -> None:
        if self._state != STATE_IDLE or self._box is None:
            _LOGGER.debug("detection ignored in state %s", self._state)
            return
        self._notify("detection", dict(data))
        # Item N (dial-in 2026-11-11): stamp the spool second for
        # this turn. The wake server names its spool entry with the
        # same local second (same host clock); the verify gate uses
        # the name to read + claim THIS turn's entry, not the
        # newest one that happens to be there.
        self._detection_spool_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        self._capture_started_at = time.monotonic()
        self._set_state(STATE_CAPTURING)
        self._clip_prefix = b"".join(self._clip_ring)
        self._clip_ring.clear()
        self._clip_ring_bytes = 0
        self._utterance = bytearray(b"".join(self._preroll))
        self._utterance_preroll_len = len(self._utterance)
        self._preroll.clear()
        self._preroll_bytes = 0
        self._vad_started = False
        self._ep.begin_capture()
        self._emit_box("run_start", self._box.evt_run_start)
        self._emit_box("stt_start", self._box.evt_stt_start)
        loop = asyncio.get_running_loop()
        self._capture_deadline = loop.create_task(
            self._capture_watchdog(), name="lyra-capture-watchdog"
        )

    async def _capture_watchdog(self) -> None:
        """Bounds a capture: no speech within the no-speech window ends
        the run as no-text; speech that never ends is force-ended at the
        capture cap."""
        try:
            await asyncio.sleep(self._no_speech_timeout_s)
            if self._state == STATE_CAPTURING and not self._vad_started:
                _LOGGER.info("capture ended: no speech detected")
                self._finish_capture(no_speech=True)
                return
            remaining = self._max_capture_s - self._no_speech_timeout_s
            if remaining > 0:
                await asyncio.sleep(remaining)
            if self._state == STATE_CAPTURING:
                _LOGGER.info("capture ended: max duration reached")
                if self._vad_started:
                    self._notify("vad_end", {"forced": True})
                    self._emit_box(
                        "stt_vad_end", self._box.evt_stt_vad_end if self._box else None
                    )
                self._finish_capture(no_speech=not self._vad_started)
        except asyncio.CancelledError:
            pass

    def _finish_capture(self, no_speech: bool = False) -> None:
        if self._state != STATE_CAPTURING:
            return
        self._capture_started_at = None
        self._set_state(STATE_PROCESSING)
        self._ep.end_capture()
        if self._capture_deadline is not None:
            self._capture_deadline.cancel()
            self._capture_deadline = None
        pcm = bytes(self._utterance)
        self._utterance = bytearray()
        self._process_task = asyncio.create_task(
            self._process_utterance(pcm, no_speech=no_speech),
            name="lyra-process-utterance",
        )

    async def _process_utterance(self, pcm: bytes, *, no_speech: bool) -> None:
        box = self._box
        try:
            transcript = ""
            reply_text = self._reply_text
            speaker = _SentenceSpeaker(self._driver)
            self._turn_speaker = speaker
            if self._verify_gate is not None:
                outcome = await self._verify_stage(pcm, no_speech=no_speech)
                if outcome is None:
                    return  # silent drop; the run is already ended
                transcript, reply_text = outcome
            elif not no_speech:
                transcript = await self._driver.transcribe_audio(
                    pcm, RATE, WIDTH, CHANNELS
                )
            self.last_transcript = transcript
            if not transcript:
                # HA parity: a no-text turn ends as ERROR with the code
                # the firmware ignores (no error LED phase).
                if box is not None:
                    self._emit_box(
                        "error",
                        lambda: box.evt_error(ERROR_STT_NO_TEXT, "no speech recognized"),
                        {"code": ERROR_STT_NO_TEXT},
                    )
                    self._emit_box("run_end", box.evt_run_end)
                return
            self._notify("transcript", {"text": transcript})
            if box is not None:
                self._emit_box(
                    "stt_end", lambda: box.evt_stt_end(transcript), {"text": transcript}
                )
                self._emit_box("intent_start", box.evt_intent_start)
                self._emit_box("intent_end", box.evt_intent_end)
            wav_bytes = None
            if speaker.used:
                await speaker.drain()
                # Streamed route: the sentences already spoke on the
                # TV via their mirrors; the box gets one silent WAV
                # of the exact total length, exactly as the TV route
                # below would build from a whole-reply synthesis.
                wav_bytes = speaker.silence_wav()
            if wav_bytes is None:
                wav_bytes, _rate, _width, _ch = await self._driver.synthesize_speech(
                    reply_text
                )
            audio_id = self._http.store.put(wav_bytes)
            url = self._http.url_for(audio_id, self._cfg.http.public_host)
            self.last_tts_url = url
            self._notify("tts_url", {"url": url, "bytes": len(wav_bytes)})
            if box is not None:
                # TV is the only audio out (2026-11-15): the page speaks
                # the reply via the mirror, and the box -- ears only now --
                # is handed silence of the reply's length as its TTS
                # audio. Its run completes a normal play phase (no
                # empty-run beeps) and the playback echo-guard covers
                # the TV speaking, so her own voice is not re-heard as
                # a command. On a box route (not currently reachable)
                # the box gets the real audio as before.
                tts_url = url
                if speech_route() == "tv":
                    silence_id = self._http.store.put(silent_wav_like(wav_bytes))
                    tts_url = self._http.url_for(
                        silence_id, self._cfg.http.public_host)
                self._emit_box(
                    "tts_start",
                    lambda: box.evt_tts_start(reply_text),
                    {"text": reply_text},
                )
                self._emit_box(
                    "tts_end", lambda: box.evt_tts_end(tts_url), {"url": tts_url}
                )
                self._emit_box("run_end", box.evt_run_end)
        except PipelineError as exc:
            _LOGGER.error("pipeline error during run: %s", exc)
            if box is not None:
                self._emit_box(
                    "error",
                    lambda: box.evt_error("pipeline-error", str(exc)),
                    {"code": "pipeline-error"},
                )
                self._emit_box("run_end", box.evt_run_end)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never strand the box mid-run
            _LOGGER.exception("unexpected error during run")
            if box is not None:
                self._emit_box(
                    "error",
                    lambda: box.evt_error("pipeline-error", str(exc)),
                    {"code": "pipeline-error"},
                )
                self._emit_box("run_end", box.evt_run_end)
        finally:
            _sp = getattr(self, "_turn_speaker", None)
            if _sp is not None and _sp._worker is not None:
                _sp._worker.cancel()
                _sp._worker = None
            if self._box is not None:
                self._set_state(STATE_IDLE)

    # -- helpers -------------------------------------------------------------------

    async def _verify_stage(
        self, pcm: bytes, *, no_speech: bool
    ) -> tuple[str, str] | None:
        """Phase 2 verify-then-fire gate. Builds the combined clip
        (Detection-time ring snapshot + the utterance past its pre-roll;
        a no-speech capture verifies the snapshot alone — the in-path
        equivalent of the collector janitor's wake-only pass), verifies
        it through the gate, and:
        - PASS -> returns (combined transcript, reply text): the
          on_command hook (if any) sees the wake-stripped command and
          may return the reply text; the canned line is the fallback.
        - FAIL/MAYBE -> ends the run silently (ERROR with the
          firmware-ignored stt-no-text-recognized code + RUN_END; no
          STT_END, no TTS, no reply URL) and returns None."""
        box = self._box
        if no_speech:
            combined = self._clip_prefix
        else:
            combined = self._clip_prefix + pcm[self._utterance_preroll_len:]
        result = await self._verify_gate.verify(
            bytes(combined), spool_ts=self._detection_spool_ts
        )
        self._detection_spool_ts = None
        self.last_verify = result
        self._notify(
            "verify",
            {
                "verdict": result.verdict,
                "transcript": result.transcript,
                "command": result.command,
                "gate_score": result.gate_score,
                "dry_run": result.dry_run,
                "filed_path": result.filed_path,
                "audio_seconds": round(result.audio_seconds, 2),
            },
        )
        if result.verdict != "pass":
            self._notify(
                "silent_drop",
                {"verdict": result.verdict, "transcript": result.transcript},
            )
            _LOGGER.info(
                "verify %s: turn dropped silently (%r)",
                result.verdict,
                result.transcript,
            )
            if box is not None:
                self._emit_box(
                    "error",
                    lambda: box.evt_error(ERROR_STT_NO_TEXT, "no speech recognized"),
                    {"code": ERROR_STT_NO_TEXT},
                )
                self._emit_box("run_end", box.evt_run_end)
            return None
        reply = self._reply_text
        if self._on_command is not None:
            # Phase 3: the hook is the router. It is called on every
            # PASS -- including an empty command (D5 garbled handling
            # lives in the router) -- and receives the VerifyResult
            # as a second argument when its signature accepts one;
            # one-argument hooks (the Phase 1/2 benches) are called
            # exactly as before. A hook returning a string replaces
            # the reply; a hook returning nothing leaves the canned
            # fallback in place.
            hook = self._on_command
            try:
                arity = len(inspect.signature(hook).parameters)
            except (TypeError, ValueError):
                arity = 1
            speaker = getattr(self, "_turn_speaker", None)
            if arity >= 3 and speaker is not None:
                offered = hook(result.command or "", result, speaker.feed)
            elif arity >= 2:
                offered = hook(result.command or "", result)
            else:
                offered = hook(result.command or "")
            if inspect.isawaitable(offered):
                offered = await offered
            if isinstance(offered, str) and offered:
                reply = offered
        return result.transcript, reply


    def _cancel_capture_tasks(self) -> None:
        for task in (self._capture_deadline, self._process_task):
            if task is not None:
                task.cancel()
        self._capture_deadline = None
        self._process_task = None

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self._notify("state", {"state": state})

    def _notify(self, kind: str, detail: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, detail)
            except Exception:
                _LOGGER.exception("event hook failed on %s", kind)

    def _emit_box(
        self,
        name: str,
        fn: Callable[[], None] | None,
        detail: dict | None = None,
    ) -> None:
        self._notify("box_event", {"name": name, **(detail or {})})
        if fn is not None:
            fn()
