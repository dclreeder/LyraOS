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
- STT is the same Whisper path as the call feature
  (PipelineDriver.transcribe_audio).
"""
from __future__ import annotations

import array
import asyncio
import collections
import io
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
            return None  # a click, not speech: no turn, no event
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
                 generation: int) -> None:
        self._driver = driver
        self._session = session
        self._generation = generation
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
                    sentence, mirror=False)
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
        self.out_track = _PeerAudioTrack(self)

    # -- output side ------------------------------------------------

    def take_out_frame(self) -> bytes:
        n = FRAME_SAMPLES * 2
        if len(self._out) >= n:
            data = bytes(self._out[:n])
            del self._out[:n]
            return data
        data = bytes(self._out) + b"\x00" * (n - len(self._out))
        self._out.clear()
        return data

    def append_out(self, pcm48: bytes, generation: int) -> None:
        if generation == self._generation and not self._closed:
            self._out.extend(pcm48)

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
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        _LOGGER.info("rtc barge-in: reply interrupted by peer speech")

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
                    pcm = _frame_pcm(out)
                    for kind, payload in self._seg.feed(pcm):
                        if kind == "speech_start":
                            self.barge_in()
                        elif kind == "utterance" and payload is not None:
                            self._start_turn(payload)
        except MediaStreamError:
            _LOGGER.info("rtc peer audio track ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("rtc consume loop failed")

    def _start_turn(self, pcm: bytes) -> None:
        if self._turn_task is not None and not self._turn_task.done():
            # Turns stay serial: a barge-in cancels the live turn at
            # speech onset, so a still-running task here is one in
            # its final drain; the new utterance queues behind it by
            # simply being dropped -- the peer can repeat it. (In
            # practice the cancel above makes this rare.)
            _LOGGER.info("rtc utterance dropped: a turn is still running")
            return
        self._turn_task = asyncio.ensure_future(self._run_turn(pcm))

    async def _run_turn(self, pcm: bytes) -> None:
        generation = self._generation
        try:
            transcript = await self._server.driver.transcribe_audio(
                pcm, rate=UTTER_RATE, width=2, channels=1)
            if not transcript:
                _LOGGER.info(
                    "rtc utterance transcribed empty; staying silent")
                return
            _LOGGER.info("rtc transcript: %r", transcript)
            speaker = _RtcSpeaker(
                self._server.driver, self, generation)
            reply = await self._server.command(
                transcript, on_sentence=speaker.feed, via="rtc")
            await speaker.drain()
            if not speaker.used and reply:
                # Nothing streamed (a router early-return line):
                # speak the whole reply as one sentence.
                await speaker.feed(str(reply))
                await speaker.drain()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("rtc turn failed")

    async def close(self) -> None:
        self._closed = True
        self._generation += 1
        self._out.clear()
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
    rejoining after a hiccup is the normal case)."""

    def __init__(self, driver: PipelineDriver, command) -> None:
        self.driver = driver
        self.command = command
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
                write_rtc_state(True)
            elif pc.connectionState in ("disconnected", "failed", "closed"):
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
            write_rtc_state(False)

    async def stop(self) -> None:
        """Mode exit: refuse new offers and tear the peer down."""
        self.accepting = False
        await self._close_session()
