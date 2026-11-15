#!/usr/bin/env python3
"""Wyoming wake-word server for Daniel's 'hey Lyra' (Stable V1 ONNX).

Wraps the exact gate the Local Satellite uses (wake_gate.HeyLyraGate,
a verified streaming port of openWakeWord's front end) behind the
Wyoming protocol, so Home Assistant can use it as a wake_word service.
The gate is stateful, so each connection gets its own instance. A
Detection fires at most once per arming: after firing, the score must
fall below REARM before another Detection can be emitted.

Collector (2026-11-10, clip_collector.py): every detection is spooled
(wake-phrase audio + gate score) for the STT server to finalize into
the training pool with the command audio attached and a Whisper
verification transcript. Sub-threshold near-misses (peak in
[NEAR_MISS_FLOOR, THRESHOLD)) are collected as complete phrases --
the trailing snapshot plus a post-roll that runs until the speaker
finishes -- and Whisper-verified: a confirmed phrase files as a
missed trigger, a Lyra-like transcript files as a maybe. Unclaimed detections are finalized
wake-only by the janitor. The live gate stays at THRESHOLD (0.8);
near-miss handling never fires a Detection.
"""
import asyncio
import logging
import os
import sys
from collections import deque
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.expanduser("~/Documents/LyraOS/modules/wyoming"))
from wake_gate import HeyLyraGate  # noqa: E402

import clip_collector as collector  # noqa: E402

from wyoming.audio import AudioChunk, AudioStart, AudioStop  # noqa: E402
from wyoming.event import Event  # noqa: E402
from wyoming.info import Attribution, Describe, Info, WakeModel, WakeProgram  # noqa: E402
from wyoming.server import AsyncEventHandler, AsyncServer  # noqa: E402
from wyoming.wake import Detect, Detection  # noqa: E402

_LOGGER = logging.getLogger("lyra-wake")
MODEL_NAME = "hey_lyra_stable_v1"
# Lyra 2026-11-10: the live gate is 0.8 (matching HeyLyraGate's own
# default). This previously defaulted to 0.5, which is how sub-0.8
# scores fired Detections during the 2026-11-09 false-trigger incident.
THRESHOLD = float(os.environ.get("LYRA_WAKE_THRESHOLD", "0.8"))
REARM = 0.15
NEAR_MISS_FLOOR = 0.5
BUFFER_SECONDS = 8
DETECTION_CLIP_SECONDS = 2.5
NEAR_MISS_CLIP_SECONDS = 3.5
# Complete-phrase near-miss capture (Daniel, 2026-11-11): the 3.5 s
# slice above ENDS at the near-miss instant, so the command spoken
# after the wake phrase was never recorded -- a near-miss opens no
# core session. A filed miss must be a complete phrase like the
# positives are, so the handler snapshots that slice as the clip's
# head and keeps collecting a post-roll until the speaker finishes.
# Speech end is judged in the 300 Hz+ speech band: on this mic path
# the sub-300 Hz rumble bed sits at broadband RMS ~700-800 (speech
# only ~1000-2400), while in-band the bed is ~150 and speech frames
# run 500+ (measured on the 2026-11-11 pool) -- the same fix the
# core's endpointer needed, for the same reason.
NEAR_MISS_POSTROLL_MAX_SECONDS = 12.0
NEAR_MISS_START_TIMEOUT_SECONDS = 2.0
NEAR_MISS_END_HANGOVER_SECONDS = 0.7
NEAR_MISS_TAIL_PAD_SECONDS = 0.3
NEAR_MISS_BAND_FLOOR_MULT = 2.5
NEAR_MISS_BAND_FLOOR_LIFT = 75.0
_BAND_FRAME_SAMPLES = 480  # 30 ms at 16 kHz
JANITOR_STALE_SECONDS = 120

INFO = Info(
    wake=[
        WakeProgram(
            name="lyra-openwakeword",
            attribution=Attribution(name="Lyra (local)", url=""),
            installed=True,
            description="Hey Lyra wake word - Stable V1, served from the Fedora",
            version="1.0",
            models=[
                WakeModel(
                    name=MODEL_NAME,
                    attribution=Attribution(name="Lyra (local)", url=""),
                    installed=True,
                    description="hey Lyra (Stable V1)",
                    version="stable-v1",
                    languages=["en"],
                    phrase="hey Lyra",
                )
            ],
        )
    ]
)


async def _finalize_stale() -> None:
    """Janitor: detections STT never claimed (aborted pipeline, false
    trigger with no command) are still verified and filed wake-only."""
    for meta, wake_wav in collector.claim_stale_detections(JANITOR_STALE_SECONDS):
        try:
            audio = collector.read_wav(wake_wav)
            text = await collector.whisper_transcribe(audio)
            verdict = collector.classify_wake_transcription(text)
            collector.file_clip(verdict, meta["gate_score"], text,
                                meta["ts"], audio)
        except Exception:
            _LOGGER.exception("janitor finalize failed for %s", meta.get("ts"))
        finally:
            collector.discard_wake_wav(wake_wav)


async def _janitor_loop() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await _finalize_stale()
        except Exception:
            _LOGGER.exception("janitor loop error")


class _SpeechBand:
    """Two cascaded one-pole high-pass sections (~300 Hz) over the
    stream, returning the RMS of each complete 30 ms frame. Filter
    state is continuous across feeds, so a run started on the
    snapshot continues seamlessly into the post-roll chunks."""

    def __init__(self, rate: int) -> None:
        rc = 1.0 / (2.0 * np.pi * 300.0)
        self._alpha = rc / (rc + 1.0 / rate)
        self._px1 = self._py1 = self._px2 = self._py2 = 0.0
        self._pending = np.zeros(0, dtype=np.float64)

    def _section(self, x, px, py):
        y = np.empty_like(x)
        a = self._alpha
        for i, xi in enumerate(x):
            py = a * (py + xi - px)
            px = xi
            y[i] = py
        return y, px, py

    def feed(self, samples_i16: np.ndarray) -> list:
        x = samples_i16.astype(np.float64)
        y, self._px1, self._py1 = self._section(x, self._px1, self._py1)
        y, self._px2, self._py2 = self._section(y, self._px2, self._py2)
        buf = np.concatenate([self._pending, y])
        n = len(buf) // _BAND_FRAME_SAMPLES
        rms = [float(np.sqrt(np.mean(buf[k * _BAND_FRAME_SAMPLES:
                                        (k + 1) * _BAND_FRAME_SAMPLES] ** 2)))
               for k in range(n)]
        self._pending = buf[n * _BAND_FRAME_SAMPLES:]
        return rms


class _NearMissCollection:
    """A near-miss being collected into a complete phrase: the
    trailing snapshot taken at the near-miss instant (lead-in + the
    wake phrase near its end), plus every chunk that arrives after
    it until the speaker finishes. Fed chunk-by-chunk from the
    event handler, so the audio/detection loop never waits on it;
    only the Whisper verification runs as a task."""

    def __init__(self, peak: float, snapshot: np.ndarray, rate: int) -> None:
        self.peak = peak
        self.pre = snapshot
        self.rate = rate
        self.post: list = []
        self.post_samples = 0
        self.heard_speech = False
        self._quiet_samples = 0
        self._band = _SpeechBand(rate)
        # The speech floor comes from the snapshot itself (its
        # lead-in is room tone); running the snapshot through the
        # band filter also leaves the filter state continuous with
        # the live stream that follows it.
        floor_frames = self._band.feed(snapshot)
        self.floor_rms = (float(np.percentile(floor_frames, 10))
                          if floor_frames else 0.0)
        self.speech_rms = max(NEAR_MISS_BAND_FLOOR_MULT * self.floor_rms,
                              self.floor_rms + NEAR_MISS_BAND_FLOOR_LIFT)

    def feed(self, samples: np.ndarray) -> bool:
        """Add one post-roll chunk; True when the phrase is over."""
        self.post.append(samples)
        self.post_samples += len(samples)
        for rms in self._band.feed(samples):
            if rms >= self.speech_rms:
                self.heard_speech = True
                self._quiet_samples = 0
            else:
                self._quiet_samples += _BAND_FRAME_SAMPLES
        if self.post_samples >= int(NEAR_MISS_POSTROLL_MAX_SECONDS * self.rate):
            return True
        if self.heard_speech:
            return self._quiet_samples >= int(
                NEAR_MISS_END_HANGOVER_SECONDS * self.rate)
        return self.post_samples >= int(
            NEAR_MISS_START_TIMEOUT_SECONDS * self.rate)

    def audio(self) -> np.ndarray:
        pad = np.zeros(int(NEAR_MISS_TAIL_PAD_SECONDS * self.rate),
                       dtype=np.int16)
        return np.concatenate([self.pre, *self.post, pad])


class WakeHandler(AsyncEventHandler):
    def __init__(self, reader, writer):
        super().__init__(reader, writer)
        self.gate = HeyLyraGate()
        self.context = None
        self.armed = True
        self.rate = 16000
        self._chunks = deque()
        self._buffered = 0
        self._peak = 0.0
        self._nm = None  # _NearMissCollection while one is pending
        self._nm_tasks = set()

    def _push_audio(self, samples: np.ndarray) -> None:
        self._chunks.append(samples)
        self._buffered += len(samples)
        cap = BUFFER_SECONDS * self.rate
        while self._buffered > cap and self._chunks:
            self._buffered -= len(self._chunks.popleft())

    def _snapshot(self, seconds: float) -> np.ndarray:
        want = int(seconds * self.rate)
        parts = []
        for chunk in reversed(self._chunks):
            parts.append(chunk)
            want -= len(chunk)
            if want <= 0:
                break
        if not parts:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(list(reversed(parts)))
        return audio[-int(seconds * self.rate):]

    async def _verify_near_miss(self, peak: float, audio: np.ndarray) -> None:
        try:
            text = await collector.whisper_transcribe(audio)
            verdict = collector.classify_wake_transcription(text)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            if verdict == "pass":
                collector.file_missed(peak, text, ts, audio)
            elif verdict == "maybe":
                collector.file_clip("maybe", peak, text, ts, audio)
            else:
                _LOGGER.info("Near-miss peak=%.3f classified fail "
                             "(not filed): %r", peak, text)
        except Exception:
            _LOGGER.exception("near-miss verify failed")

    def _finish_near_miss(self) -> None:
        nm, self._nm = self._nm, None
        if nm is None:
            return
        audio = nm.audio()
        _LOGGER.info("Near-miss peak=%.3f; verifying complete phrase "
                     "(%.1fs)", nm.peak, len(audio) / self.rate)
        task = asyncio.create_task(self._verify_near_miss(nm.peak, audio))
        self._nm_tasks.add(task)
        task.add_done_callback(self._nm_tasks.discard)

    async def handle_event(self, event: Event) -> bool:
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            if chunk.rate:
                self.rate = chunk.rate
            samples = np.frombuffer(chunk.audio, dtype=np.int16)
            self._push_audio(samples)
            score = self.gate.predict(samples)
            created = False
            if self.armed and score >= THRESHOLD:
                if self._nm is not None:
                    # The real turn wins: the core files this
                    # utterance as a positive. Never double-file.
                    _LOGGER.info("Near-miss collection dropped "
                                 "(peak=%.3f): real detection", self._nm.peak)
                    self._nm = None
                self.armed = False
                self._peak = 0.0
                _LOGGER.info("Detection %s score=%.3f", MODEL_NAME, score)
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")
                try:
                    collector.spool_detection(
                        ts, float(score), self._snapshot(DETECTION_CLIP_SECONDS))
                except Exception:
                    _LOGGER.exception("spool_detection failed")
                await self.write_event(
                    Detection(name=MODEL_NAME, context=self.context).event()
                )
            elif (not self.armed) and score < REARM:
                self.armed = True
            elif self.armed:
                if score > self._peak:
                    self._peak = score
                if self._peak >= NEAR_MISS_FLOOR and score < REARM:
                    peak, self._peak = self._peak, 0.0
                    if self._nm is not None:
                        # Overlapping near-miss: fold it into the
                        # collection already running, keep the max.
                        self._nm.peak = max(self._nm.peak, peak)
                    else:
                        # Snapshot the trailing slice NOW (a copy --
                        # the rolling buffer keeps moving), then let
                        # the post-roll collect the rest of the phrase.
                        self._nm = _NearMissCollection(
                            peak,
                            self._snapshot(NEAR_MISS_CLIP_SECONDS).copy(),
                            self.rate)
                        created = True
                        _LOGGER.info("Near-miss peak=%.3f; collecting "
                                     "complete phrase", peak)
            if self._nm is not None and not created:
                if self._nm.feed(samples):
                    self._finish_near_miss()
            return True
        if Detect.is_type(event.type):
            detect = Detect.from_event(event)
            self.context = detect.context
            self.gate.reset()
            self.armed = True
            self._peak = 0.0
            if self._nm is not None:
                _LOGGER.info("Near-miss collection dropped "
                             "(peak=%.3f): new Detect", self._nm.peak)
                self._nm = None
            return True
        if Describe.is_type(event.type):
            await self.write_event(INFO.event())
            return True
        if AudioStop.is_type(event.type):
            # The stream ended mid-collection: file what we have.
            if self._nm is not None:
                self._finish_near_miss()
            return True
        # AudioStart / anything else: accept, no state change.
        return True


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = AsyncServer.from_uri("tcp://0.0.0.0:10400")
    _LOGGER.info("Lyra wake server listening on tcp://0.0.0.0:10400 "
                 "(threshold=%.2f, collector on)", THRESHOLD)
    asyncio.create_task(_janitor_loop())
    await server.run(lambda r, w: WakeHandler(r, w))


if __name__ == "__main__":
    asyncio.run(main())
