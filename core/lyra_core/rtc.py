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
- STT is streaming Nemotron 3.5 (2026-11-18): the peer's audio
  feeds the Nemotron streaming sidecar (lyra_core.stream_stt)
  and a turn ends on text finalization -- finalized text plus a
  short quiet confirmation window -- not a silence-timed batch
  cut. If the sidecar is down at session start or dies
  mid-session, the session falls back to the Endpointer +
  PipelineDriver.transcribe_audio (Parakeet TDT) path,
  unchanged. WakeGate never uses the streamer.
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
from .stream_stt import StreamSttClient

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
STREAM_CONFIRM_S = 0.35      # quiet + no-new-text window ending a streamed turn
STREAM_TAIL_S = 0.10         # audio that must be fed past the last finalized word
STREAM_IDLE_RESET_S = 8.0    # silent streamed audio before a hygiene reset
STREAM_SEG_GRACE_S = 0.35    # grace for late finals after the segmenter cuts


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
        self._stream: StreamSttClient | None = None
        self._stream_setup_task: asyncio.Task | None = None
        self._utter_streaming = False
        self._stream_dispatched = False
        self._pending_pcm: bytes | None = None
        self._seg_done_at = 0.0
        self._utter_stream_start = 0.0
        self._last_loud_at = 0.0
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
        self._stream_setup_task = asyncio.ensure_future(self._setup_stream())

    async def _setup_stream(self) -> None:
        """Session-start sidecar health check: connect to the
        Nemotron streaming sidecar. Success arms the streaming STT
        path for this session; failure leaves the session on the
        legacy Endpointer + TDT path, exactly as before."""
        client = await StreamSttClient.connect()
        if client is None:
            _LOGGER.warning(
                "rtc: stream sidecar unreachable; session falls back "
                "to Endpointer + Parakeet TDT")
            return
        if self._closed:
            await client.close()
            return
        client.on_failure = self._on_stream_failure
        self._stream = client
        _LOGGER.info("rtc: Nemotron streaming STT armed for this session")

    def _on_stream_failure(self) -> None:
        """The sidecar died mid-session: fall back to the legacy
        Endpointer + TDT path. An utterance the segmenter already
        cut dispatches from its held PCM now; one still in flight
        dispatches when the segmenter's hangover completes; one
        the stream already dispatched needs nothing."""
        if self._closed:
            return
        _LOGGER.warning(
            "rtc: stream sidecar lost mid-session; falling back to "
            "Endpointer + Parakeet TDT")
        if self._stream_dispatched:
            return
        if self._pending_pcm is not None:
            pending, self._pending_pcm = self._pending_pcm, None
            self._utter_streaming = False
            self._start_turn_pcm(pending)
        else:
            self._utter_streaming = False

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
        """One resampled 16 kHz chunk of peer audio: the segmenter
        always sees it (barge-in onset + the legacy path), and the
        stream sidecar sees it whenever this session is armed."""
        now = time.monotonic()
        if _Segmenter._rms(pcm) >= self._seg.end_threshold():
            self._last_loud_at = now
        for kind, payload in self._seg.feed(pcm):
            if kind == "speech_start":
                self.barge_in()
                stream = self._stream
                self._utter_streaming = (
                    stream is not None and not stream.failed)
                self._stream_dispatched = False
                self._pending_pcm = None
                if self._utter_streaming and (
                        stream.text or stream.audio_fed > 0.05):
                    # The stream heard audio before this utterance
                    # was armed (it connected late, or a previous
                    # turn's tail lingered): start clean.
                    await stream.reset()
                self._utter_stream_start = (
                    stream.audio_fed if stream is not None else 0.0)
            elif kind == "utterance" and payload is not None:
                self._on_segmenter_utterance(payload, now)
        stream = self._stream
        if stream is None or stream.failed:
            return
        await stream.feed(pcm)
        if (not self._utter_streaming and not stream.text
                and stream.audio_fed >= STREAM_IDLE_RESET_S
                and now - self._last_loud_at >= 1.0):
            # Minutes of room tone would otherwise pile up in the
            # model's cache between turns; restart the stream on a
            # quiet boundary instead. Never mid-utterance.
            await stream.reset()
            return
        await self._maybe_dispatch_stream(now)

    def _on_segmenter_utterance(self, pcm: bytes, now: float) -> None:
        if not self._utter_streaming:
            # Legacy path: no stream armed for this utterance (the
            # sidecar was down, or it died and handed the utterance
            # back). Transcribe the buffered PCM exactly as before.
            self._start_turn_pcm(pcm)
            return
        if self._stream_dispatched:
            # The stream already turned this speech into a turn;
            # the segmenter's cut is just its hangover catching up.
            self._utter_streaming = False
            self._pending_pcm = None
            return
        # The segmenter cut before the stream's finals settled
        # (finalization can lag the acoustic end by ~0.65 s). Hold
        # the PCM: the stream dispatch proceeds when its text
        # lands, and the PCM is the fallback if the stream dies
        # before that.
        self._pending_pcm = pcm
        self._seg_done_at = now

    async def _maybe_dispatch_stream(self, now: float) -> None:
        """Turn end for the streaming path (2026-11-18): the model
        has finalized text, no new text has landed for
        STREAM_CONFIRM_S, the input has been energy-quiet for the
        same window, and audio has been fed past the last
        finalized word. The segmenter's cut short-circuits the
        wait when it fires first with text already in hand. A
        streamed utterance that runs to MAX_UTTER_S is flushed
        regardless (the legacy cap)."""
        stream = self._stream
        if (stream is None or stream.failed or not self._utter_streaming
                or self._stream_dispatched):
            return
        text = stream.text.strip()
        seg_done = self._pending_pcm is not None
        forced = (stream.audio_fed - self._utter_stream_start
                  >= MAX_UTTER_S)
        if not forced and not (seg_done and text):
            if not text:
                if seg_done and now - self._seg_done_at >= STREAM_SEG_GRACE_S:
                    # The segmenter heard a full utterance and the
                    # model finalized nothing: unintelligible or
                    # noise. Drop it (the legacy path would have
                    # transcribed empty and stayed silent too) and
                    # resync the stream.
                    self._utter_streaming = False
                    self._pending_pcm = None
                    await stream.reset()
                return
            if now - stream.last_text_at < STREAM_CONFIRM_S:
                return
            if now - self._last_loud_at < STREAM_CONFIRM_S:
                return
            last_word_end = stream.last_word_end
            if (last_word_end is not None
                    and stream.audio_fed - last_word_end < STREAM_TAIL_S):
                return
        self._stream_dispatched = True
        final = await stream.flush()
        self._utter_stream_start = stream.audio_fed
        if final is None:
            _LOGGER.warning(
                "rtc stream flush failed; session on the legacy path now")
            pending, self._pending_pcm = self._pending_pcm, None
            self._utter_streaming = False
            if pending is not None:
                self._start_turn_pcm(pending)
            return
        self._pending_pcm = None
        if not final.strip():
            return  # silence/noise: no turn, staying silent
        self._start_turn_text(final.strip())

    def _turn_busy(self) -> bool:
        return self._turn_task is not None and not self._turn_task.done()

    def _start_turn_pcm(self, pcm: bytes) -> None:
        if self._turn_busy():
            # Turns stay serial: a barge-in cancels the live turn at
            # speech onset, so a still-running task here is one in
            # its final drain; the new utterance queues behind it by
            # simply being dropped -- the peer can repeat it. (In
            # practice the cancel above makes this rare.)
            _LOGGER.info("rtc utterance dropped: a turn is still running")
            return
        self._turn_task = asyncio.ensure_future(
            self._run_turn_pcm(pcm, self._generation))

    def _start_turn_text(self, transcript: str) -> None:
        if self._turn_busy():
            _LOGGER.info("rtc utterance dropped: a turn is still running")
            return
        self._turn_task = asyncio.ensure_future(
            self._run_transcript(transcript, self._generation,
                                 source="nemotron-stream"))

    async def _run_turn_pcm(self, pcm: bytes, generation: int) -> None:
        try:
            transcript = await self._server.driver.transcribe_audio(
                pcm, rate=UTTER_RATE, width=2, channels=1)
            if not transcript:
                _LOGGER.info(
                    "rtc utterance transcribed empty; staying silent")
                return
            await self._run_transcript(
                transcript, generation, source="parakeet-tdt")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("rtc turn failed")

    async def _run_transcript(self, transcript: str, generation: int,
                              source: str) -> None:
        try:
            _LOGGER.info("rtc transcript [%s]: %r", source, transcript)
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
        for task in (self._consume_task, self._turn_task,
                     self._stream_setup_task):
            if task is not None and not task.done():
                task.cancel()
        if self._stream is not None:
            await self._stream.close()
            self._stream = None
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
