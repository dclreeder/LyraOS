"""RTC mode: a full-duplex WebRTC session as Lyra's live channel.

Daniel's model (2026-11-16): in RTC mode the TV page -- the kiosk
browser, the only browser the Fedora ever has -- opens a WebRTC
peer connection to the core and the conversation runs as one
continuous session: no wake word, VAD-segmented turns, her speech
interruptible (barge-in). Modes are exclusive input sources: the
mode manager stops the room capture before RTC accepts offers, and
if no peer is connected the mode simply waits -- it never falls
back to the room mic.

RTC changes the transport, never the routing. Nothing here (and
nothing in the page) selects or pins an audio device: the page
captures the system default mic and plays back through the
browser's normal output, which PipeWire routes to Fedora's
default sink, exactly like every other stream tonight. This
module only moves PCM over the peer connection.

Stack: aiortc, direct. Pipecat's SmallWebRTC transport was the
discussion pick, but probing it (2026-11-16) showed pipecat-ai's
base install does not bundle the transport -- aiortc is an
optional extra it wraps (the import fails without it) -- and its
dependency set (aiohttp / numpy / pydantic) would churn the
production venv the core shares with ESPHome. aiortc 1.16
installed adding only new leaf packages, and every behavior that
matters is the core's own anyway:

- Segmentation reuses the production Endpointer's calibrated
  noise-floor tracking (fed only while no utterance is in
  progress, reproducing its armed-capture floor freeze), with
  onset/offset decisions on its thresholds.
- Turns run through the SAME FreeRouter as every other path
  (via="rtc": same SOUL, same 22 tools, same history, same inbox
  logging) -- an RTC session is the same conversation.
- Speech is Kokoro via PipelineDriver.synthesize_speech with the
  TV mirror suppressed: the peer track is her voice path in this
  mode, and the mirror would double-play every line on the page.
- STT is Parakeet TDT, the house's one engine (2026-11-18):
  the segmenter cuts each utterance and
  PipelineDriver.transcribe_audio transcribes it, the same
  ears WakeGate uses. (A Nemotron streaming sidecar served
  RTC and calls for one day; Daniel cut it the same evening
  -- one STT engine for the whole house, and the 50 ms mic
  path had already taken the latency it was masking.)

Echo is the system layer's job (2026-11-18): the mic feed
arrives already echo-cancelled -- PipeWire's WebRTC AEC makes
the echo-cancelled virtual source the default source, and the
page captures the default mic like everything else -- so
barge-in in this module is the plain conversational rule: a
speech onset while she is talking interrupts her. The
word-matching echo guard that used to judge onsets against her
playback text was removed once the AEC measured clean (30 dB
down, no echo transcripts); it lives in git history if the
room ever proves it necessary again.

The phone call feature (2026-11-18, Daniel: "the phone call is
supposed to be duplex") runs on this same machinery: a second
RtcServer instance, always accepting, whose sessions carry
via="call", a mirroring speaker (her voice also reaches the TV
mirror, as the call feature has always done), and no rtc_state
publishing. The call page attaches a data channel; the session
emits his transcript whole when the turn dispatches, her reply
sentences as they generate, and barge-in events over it. The
mode-gated server above is untouched by this: a call works in
any mode.
"""
from __future__ import annotations

import array
import asyncio
import collections
import io
import json
import logging
import math
import time
import wave
from fractions import Fraction

import av
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError, MediaStreamTrack

from .endpointer import Endpointer
from .mode import write_rtc_state
from .pipeline import PipelineDriver

_LOGGER = logging.getLogger("lyra_core.rtc")

PEER_RATE = 48000            # WebRTC / Opus clock
FRAME_SAMPLES = 960          # 20 ms at 48 kHz
UTTER_RATE = 16000           # pipeline contract format (16k s16 mono)
SEG_FRAME_MS = 30            # the endpointer's frame
SEG_FRAME_BYTES = UTTER_RATE * 2 * SEG_FRAME_MS // 1000
PREROLL_FRAMES = 15          # ~0.45 s of pre-speech audio kept
HANGOVER_MS = 800            # silence that ends an utterance
ONSET_TARGET = 4             # the endpointer arms' leaky-counter target
MIN_UTTER_MS = 250           # shorter than this is a click, not speech
MAX_UTTER_S = 30.0


def _pcm_frame(pcm: bytes, rate: int) -> av.AudioFrame:
    samples = len(pcm) // 2
    frame = av.AudioFrame(format="s16", layout="mono", samples=samples)
    frame.planes[0].update(pcm[: samples * 2])
    frame.sample_rate = rate
    return frame



def _frame_pcm(frame) -> bytes:
    """Packed s16 mono PCM out of a PyAV frame WITHOUT numpy:
    frame.to_ndarray() imports numpy, which this venv does not
    have -- production only worked because another core import
    drags the wyoming venv's numpy into the process. Plane bytes
    sliced to the frame's sample count are the same data."""
    return bytes(frame.planes[0])[: frame.samples * 2]

class _Segmenter:
    """VAD segmentation over the peer's 16 kHz stream.

    The production Endpointer tracks the noise floors (its windowed
    percentile logic is the calibrated part). It is fed ONLY while
    no utterance is in progress, which reproduces the floor freeze
    of its armed captures. Decisions ride its thresholds with the
    same hysteresis shape the session path uses: start threshold,
    0.8x end threshold, leaky onset counter, hangover.
    """

    def __init__(self) -> None:
        self._ep = Endpointer()
        self._buf = bytearray()
        self._preroll: collections.deque[bytes] = collections.deque(
            maxlen=PREROLL_FRAMES)
        self._utter: bytearray | None = None
        self._onset = 0
        self._silence_frames = 0
        self._hangover_frames = max(1, round(HANGOVER_MS / SEG_FRAME_MS))

    @property
    def in_utterance(self) -> bool:
        return self._utter is not None

    def end_threshold(self) -> float:
        return self._ep.thresholds()[1]

    @staticmethod
    def _rms(frame: bytes) -> float:
        samples = array.array("h")
        samples.frombytes(frame)
        if not samples:
            return 0.0
        return math.sqrt(sum(s * s for s in samples) / len(samples))

    def feed(self, pcm: bytes) -> list[tuple[str, bytes | None]]:
        events: list[tuple[str, bytes | None]] = []
        self._buf.extend(pcm)
        while len(self._buf) >= SEG_FRAME_BYTES:
            frame = bytes(self._buf[:SEG_FRAME_BYTES])
            del self._buf[:SEG_FRAME_BYTES]
            event = self._feed_frame(frame)
            if event is not None:
                events.append(event)
        return events

    def _feed_frame(self, frame: bytes) -> tuple[str, bytes | None] | None:
        start_thr, end_thr = self._ep.thresholds()
        level = self._rms(frame)
        if self._utter is None:
            self._ep.feed(frame)  # floors learn from non-speech only
            self._preroll.append(frame)
            if level >= start_thr:
                self._onset += 2
                if self._onset >= ONSET_TARGET:
                    self._utter = bytearray(b"".join(self._preroll))
                    self._silence_frames = 0
                    self._onset = 0
                    return ("speech_start", None)
            elif self._onset > 0:
                self._onset -= 1
            return None
        self._utter.extend(frame)
        if level >= start_thr:
            self._silence_frames = 0
        elif level < end_thr:
            self._silence_frames += 1
            if self._silence_frames >= self._hangover_frames:
                return self._finish()
        if len(self._utter) >= int(MAX_UTTER_S * UTTER_RATE * 2):
            return self._finish()
        return None

    def _finish(self) -> tuple[str, bytes | None] | None:
        pcm = bytes(self._utter or b"")
        self._utter = None
        self._silence_frames = 0
        if len(pcm) < MIN_UTTER_MS * UTTER_RATE * 2 // 1000:
            # A click, not speech: no turn -- but the capture did
            # end, and the session's status light needs the word
            # (a dangling "listening" is worse than the event).
            return ("speech_end", None)
        return ("utterance", pcm)


class _PeerAudioTrack(MediaStreamTrack):
    """Her voice to the peer: a 48 kHz mono track fed from the
    session's PCM buffer. recv() is clock-paced; an empty buffer is
    silence. Barge-in clears the buffer, so interruption lands
    within a frame or two of queued audio."""

    kind = "audio"

    def __init__(self, session: "_RtcSession") -> None:
        super().__init__()
        self._session = session
        self._timestamp = 0
        self._epoch: float | None = None

    async def recv(self) -> av.AudioFrame:
        if self.readyState != "live":
            raise MediaStreamError
        if self._epoch is None:
            self._epoch = time.monotonic()
        wait = self._epoch + self._timestamp / PEER_RATE - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        frame = av.AudioFrame(format="s16", layout="mono",
                              samples=FRAME_SAMPLES)
        frame.planes[0].update(self._session.take_out_frame())
        frame.sample_rate = PEER_RATE
        frame.pts = self._timestamp
        frame.time_base = Fraction(1, PEER_RATE)
        self._timestamp += FRAME_SAMPLES
        return frame


class _RtcSpeaker:
    """The call feature's _CallSpeaker shape for RTC: reply
    sentences are synthesized in order (mirror suppressed -- the
    peer track is her voice path in this mode) and appended to the
    session's out buffer at 48 kHz. Synthesis runs in a worker task
    so TTS never blocks the token stream. Sentences synthesized
    under a stale generation (a barge-in landed mid-turn) are
    dropped instead of played."""

    def __init__(self, driver: PipelineDriver, session: "_RtcSession",
                 generation: int, mirror: bool = False) -> None:
        self._driver = driver
        self._session = session
        self._generation = generation
        self._mirror = mirror
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._resampler = av.AudioResampler(
            format="s16", layout="mono", rate=PEER_RATE)
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
                for frame in self._resampler.resample(None):
                    self._session.append_out(
                        _frame_pcm(frame), self._generation)
                return
            try:
                wav_bytes, rate, _w, _c = await self._driver.synthesize_speech(
                    sentence, mirror=self._mirror)
            except Exception:
                _LOGGER.exception(
                    "rtc sentence synthesis failed: %r", sentence)
                continue
            with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                pcm = wf.readframes(wf.getnframes())
            for frame in self._resampler.resample(_pcm_frame(pcm, rate)):
                self._session.append_out(
                    _frame_pcm(frame), self._generation)

    async def drain(self) -> None:
        if self._worker is not None:
            await self._queue.put(None)
            await self._worker
            self._worker = None


class _RtcSession:
    """One peer connection's conversation: consume the peer's
    audio, segment it into utterances, run each through the shared
    router, play the reply on the out track. Barge-in: a speech
    onset while she is talking cancels the live turn and clears
    the out buffer."""

    def __init__(self, server: "RtcServer", pc: RTCPeerConnection) -> None:
        self._server = server
        self.pc = pc
        self._seg = _Segmenter()
        self._out = bytearray()
        self._generation = 0
        self._turn_task: asyncio.Task | None = None
        self._consume_task: asyncio.Task | None = None
        self._closed = False
        self._event_sink = None
        self._status_playing = False
        self._status_listening = False
        self.out_track = _PeerAudioTrack(self)

    # -- output side ------------------------------------------------

    def take_out_frame(self) -> bytes:
        n = FRAME_SAMPLES * 2
        if len(self._out) >= n:
            data = bytes(self._out[:n])
            del self._out[:n]
            self._note_playing()
            return data
        data = bytes(self._out) + b"\x00" * (n - len(self._out))
        self._out.clear()
        self._note_playing()
        return data

    def append_out(self, pcm48: bytes, generation: int) -> None:
        if generation == self._generation and not self._closed:
            self._out.extend(pcm48)
            self._note_playing()

    def _note_playing(self) -> None:
        """Status light (2026-11-18): report real playback state
        -- sentence audio queued or draining -- to the core's
        status hub, on transitions only. Playback is tracked by
        the playhead (take_out_frame drains in real time), so this
        is her actually sounding on the peer, not a timer."""
        playing = len(self._out) > 0
        if playing == self._status_playing:
            return
        self._status_playing = playing
        hub = self._server.status
        if hub is not None:
            hub.rtc_playing(id(self), playing)

    def _note_listening(self, active: bool) -> None:
        """Status light (2026-11-18): report utterance capture --
        the gate is open and she is taking in the peer's speech --
        on transitions only. Set at the segmenter's speech onset;
        cleared at every resolution (dispatch, discard, close).
        The hub ranks listening below speaking and thinking, so
        an onset during her playback (a barge-in onset) never
        flips the light off blue; it shows
        only once playback has actually stopped and he is still
        speaking."""
        if active == self._status_listening:
            return
        self._status_listening = active
        hub = self._server.status
        if hub is not None:
            hub.set_listening(id(self), active)

    @property
    def speaking(self) -> bool:
        turn_live = (self._turn_task is not None
                     and not self._turn_task.done())
        return turn_live or len(self._out) > FRAME_SAMPLES * 2 * 5

    def barge_in(self) -> None:
        if not self.speaking:
            return
        self._generation += 1
        self._out.clear()
        self._note_playing()
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        _LOGGER.info("%s barge-in: reply interrupted by peer speech",
                     self._server.label)
        self._emit({"type": "barge_in"})

    # -- page events (the call's data channel) ------------------------

    def attach_event_channel(self, channel) -> None:
        """Wire the session's page events to a WebRTC data channel
        (the phone call page). Events are JSON dicts: the
        dispatched transcript (whole, at turn dispatch), her
        reply sentences as they generate, reply completion, and
        barge-in. The TV page creates no data channel, so RTC
        mode sessions never emit."""
        def _send(msg: dict) -> None:
            try:
                if channel.readyState == "open":
                    channel.send(json.dumps(msg))
            except Exception:
                pass
        self._event_sink = _send

    def _emit(self, msg: dict) -> None:
        if self._event_sink is not None:
            self._event_sink(msg)

    # -- input side ---------------------------------------------------

    def attach_input(self, track: MediaStreamTrack) -> None:
        self._consume_task = asyncio.ensure_future(self._consume(track))

    async def _consume(self, track: MediaStreamTrack) -> None:
        resampler = av.AudioResampler(
            format="s16", layout="mono", rate=UTTER_RATE)
        try:
            while not self._closed:
                frame = await track.recv()
                for out in resampler.resample(frame):
                    await self._consume_pcm(_frame_pcm(out))
        except MediaStreamError:
            _LOGGER.info("rtc peer audio track ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("rtc consume loop failed")

    async def _consume_pcm(self, pcm: bytes) -> None:
        """One resampled 16 kHz chunk of peer audio: feed the
        segmenter; its events drive the turn. A speech onset
        while she is talking is the barge-in; a finished
        utterance is transcribed (Parakeet TDT) and dispatched."""
        for kind, payload in self._seg.feed(pcm):
            if kind == "speech_start":
                self._note_listening(True)
                if self.speaking:
                    # Barge-in: an onset while she is talking IS
                    # the interruption -- cancel the turn and let
                    # this utterance become his. No echo judging:
                    # the feed arrives echo-cancelled by PipeWire,
                    # so her own voice does not reach this point.
                    self.barge_in()
            elif kind == "speech_end":
                # The segmenter discarded a too-short blip: the
                # capture is over and nothing else resolves it.
                self._note_listening(False)
            elif kind == "utterance" and payload is not None:
                self._start_turn_pcm(payload)

    def _turn_busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    def _status_turn_begin(self) -> None:
        """Status light: a dispatched turn is thinking from here
        (STT finalization + router + tools) until the task ends;
        speaking is reported separately by the playback tracker.
        The done callback keeps begin/end paired on every exit
        path, barge-in cancels included."""
        hub = self._server.status
        if hub is None or self._turn_task is None:
            self._note_listening(False)
            return
        hub.turn_begin()
        self._turn_task.add_done_callback(lambda _t: hub.turn_end())
        # Dispatch ends the capture; clearing listening after
        # turn_begin hands the light from pink to amber with no
        # idle flicker between.
        self._note_listening(False)

    def _start_turn_pcm(self, pcm: bytes) -> None:
        if self._turn_busy():
            # Turns stay serial: a barge-in cancels the live turn at
            # speech onset, so a still-running task here is one in
            # its final drain; the new utterance queues behind it by
            # simply being dropped -- the peer can repeat it. (In
            # practice the cancel above makes this rare.)
            _LOGGER.info("%s utterance dropped: a turn is still running",
                         self._server.label)
            self._note_listening(False)
            return
        self._turn_task = asyncio.ensure_future(
            self._run_turn_pcm(pcm, self._generation))
        self._status_turn_begin()

    async def _run_turn_pcm(self, pcm: bytes, generation: int) -> None:
        try:
            transcript = await self._server.driver.transcribe_audio(
                pcm, rate=UTTER_RATE, width=2, channels=1)
            if not transcript:
                _LOGGER.info(
                    "%s utterance transcribed empty; staying silent",
                    self._server.label)
                return
            await self._run_transcript(
                transcript, generation, source="parakeet-tdt")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("%s turn failed", self._server.label)

    async def _run_transcript(self, transcript: str, generation: int,
                              source: str) -> None:
        try:
            _LOGGER.info("%s transcript [%s]: %r",
                         self._server.label, source, transcript)
            self._emit({"type": "transcript", "text": transcript,
                        "source": source})
            speaker = _RtcSpeaker(
                self._server.driver, self, generation,
                mirror=self._server.mirror)

            async def _feed(sentence: str) -> None:
                # Her words stream too: the page sees each sentence
                # as it is generated, before it is synthesized.
                self._emit({"type": "reply_sentence", "text": sentence})
                await speaker.feed(sentence)

            reply = await self._server.command(
                transcript, on_sentence=_feed, via=self._server.via)
            await speaker.drain()
            if not speaker.used and reply:
                # Nothing streamed (a router early-return line):
                # speak the whole reply as one sentence.
                await _feed(str(reply))
                await speaker.drain()
            self._emit({"type": "reply_done",
                        "text": str(reply) if reply else ""})
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("%s turn failed", self._server.label)

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        self._out.clear()
        self._note_playing()
        self._note_listening(False)
        for task in (self._consume_task, self._turn_task):
            if task is not None and not task.done():
                task.cancel()
        try:
            await self.pc.close()
        except Exception:
            pass


class RtcServer:
    """Owns the (single) RTC peer session and the offer endpoint's
    backend. Offers are honored only while `accepting` -- the mode
    manager flips that with the mode, so a stray offer in WakeGate
    or Sleep gets a clean refusal, never a session. One peer at a
    time: a fresh offer replaces the previous peer (the page
    rejoining after a hiccup is the normal case).

    The phone call feature runs a second instance of this class
    (via="call", mirror=True, publish_state=False) whose
    `accepting` is simply always on: a call is not a mode, so its
    offers are honored in WakeGate, RTC, and Sleep alike, and it
    never touches rtc_state.json -- that file is the TV mode's
    published fact, not the call's."""

    def __init__(self, driver: PipelineDriver, command, *,
                 via: str = "rtc", label: str = "rtc",
                 mirror: bool = False,
                 publish_state: bool = True) -> None:
        self.driver = driver
        self.command = command
        self.via = via
        self.label = label
        self.mirror = mirror
        self.publish_state = publish_state
        # Status hub (lyra_core.status), wired by production_main;
        # None in benches.
        self.status = None
        self.accepting = False
        self._session: _RtcSession | None = None

    @property
    def peer_connected(self) -> bool:
        return (self._session is not None
                and self._session.pc.connectionState == "connected")

    async def handle_offer(self, sdp: str, sdp_type: str) -> dict:
        if not self.accepting:
            return {"error": "rtc mode is not active", "status": 409}
        await self._close_session()
        pc = RTCPeerConnection()
        session = _RtcSession(self, pc)
        self._session = session

        @pc.on("datachannel")
        def _on_datachannel(channel) -> None:
            _LOGGER.info("%s peer data channel: %s",
                         self.label, channel.label)
            session.attach_event_channel(channel)

        @pc.on("track")
        def _on_track(track: MediaStreamTrack) -> None:
            _LOGGER.info("rtc peer track: %s", track.kind)
            if track.kind == "audio":
                session.attach_input(track)

        @pc.on("connectionstatechange")
        async def _on_state() -> None:
            _LOGGER.info("rtc peer connection state: %s",
                         pc.connectionState)
            if pc.connectionState == "connected":
                if self.publish_state:
                    write_rtc_state(True)
            elif pc.connectionState in ("disconnected", "failed", "closed"):
                if self.publish_state:
                    write_rtc_state(False)
                if pc.connectionState in ("failed", "closed"):
                    asyncio.ensure_future(self._close_session())

        try:
            await pc.setRemoteDescription(
                RTCSessionDescription(sdp=sdp, type=sdp_type))
            pc.addTrack(session.out_track)
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)
            for _ in range(100):
                if pc.iceGatheringState == "complete":
                    break
                await asyncio.sleep(0.05)
        except Exception:
            _LOGGER.exception("rtc offer handling failed")
            await self._close_session()
            return {"error": "offer failed", "status": 500}
        _LOGGER.info("rtc offer answered; peer %s", pc.connectionState)
        return {"sdp": pc.localDescription.sdp,
                "type": pc.localDescription.type}

    async def _close_session(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            await session.close()
            if self.publish_state:
                write_rtc_state(False)

    async def stop(self) -> None:
        """Mode exit: refuse new offers and tear the peer down."""
        self.accepting = False
        await self._close_session()
