"""In-process wake-word detection (2026-11-19): the gate moved home.

Until today the last Wyoming service standing was the wake server:
the core streamed mic audio over TCP to modules/wyoming's
wake_server.py, which ran openWakeWord (hey_lyra_stable_v1.onnx)
behind the Wyoming protocol and spooled every Detection to disk so
the clip collector could claim it later. Daniel's simplification
pass deleted the service: this module runs the SAME gate in the
core process -- the recovered HeyLyraGate from modules/wyoming/
wake_gate.py (same model files from modules/wakeword, same 0.8
threshold, verified score-for-score identical in this interpreter
before the move) -- fed directly from the mic stream the core
already receives via localmic.

The handler state machine below (arming/rearm, the sub-threshold
near-miss phrase collection with its speech-band endpointing) is
the wake server's WakeHandler with the wire removed: audio arrives
as bytes through feed(), a Detection comes back as a
PendingDetection, and the collector handoff is a registry entry,
not a spool file. The spool directory and its claim protocol are
gone; the two filing behaviors survive with identical outputs
(clip names, pool directories, ledger format):

- command-finalize: verify.finalize_pending_detection claims the
  newest pending detection when a command transcription completes
  and files wake+command audio through the collector;
- stale sweep: run_detection_sweep() (driven by production_main)
  files detections never claimed within the 120 s window
  wake-only -- the job the wake server's janitor did.

Reuse, not fork: the gate and the collector stay in
modules/wyoming/ (the TTS bridge's home) and are imported here,
exactly as lyra_core.verify already imports the collector.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

_WYOMING_SERVICES = Path.home() / "Documents" / "LyraOS" / "modules" / "wyoming"
if str(_WYOMING_SERVICES) not in sys.path:
    sys.path.insert(0, str(_WYOMING_SERVICES))
import clip_collector as collector  # noqa: E402
from wake_gate import HeyLyraGate  # noqa: E402

_LOGGER = logging.getLogger("lyra_core.wake")

# Gate discipline, verbatim from the deleted wake server: the live
# gate is 0.8; after firing, the score must fall below REARM before
# another Detection can be emitted. Near-miss handling never fires
# a Detection.
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
# A detection never claimed by a command transcription is filed
# wake-only once it is older than this (the old janitor's window).
STALE_SECONDS = 120.0


def wake_info() -> dict:
    """The wake service's Info, as PipelineDriver.describe_all used
    to fetch it over Wyoming: same shape, produced in-process."""
    return {
        "wake": [
            {
                "name": "lyra-openwakeword",
                "attribution": {"name": "Lyra (local)", "url": ""},
                "installed": True,
                "description": "Hey Lyra wake word - Stable V1, in the core process",
                "version": "1.0",
                "models": [
                    {
                        "name": "hey_lyra_stable_v1",
                        "attribution": {"name": "Lyra (local)", "url": ""},
                        "installed": True,
                        "description": "hey Lyra (Stable V1)",
                        "version": "stable-v1",
                        "languages": ["en"],
                        "phrase": "hey Lyra",
                    }
                ],
            }
        ]
    }


# ---------------------------------------------------------------------
# Pending detections: the collector handoff, in-process.
# ---------------------------------------------------------------------


@dataclass
class PendingDetection:
    id: int
    ts: str  # local second of the detection ("%Y%m%d_%H%M%S")
    gate_score: float
    wake_audio: np.ndarray  # int16, the 2.5 s detection snapshot
    registered_at: float  # time.time() at registration


class DetectionRegistry:
    """Detections waiting for the collector. Replaces the on-disk
    spool + rename-claim protocol: producers register, the command
    path claims the newest fresh entry, the sweep claims the stale
    ones, and a verify-filed turn discards its own entry. All
    callers share one event loop, so plain dict ops are the whole
    concurrency story."""

    def __init__(self) -> None:
        self._entries: dict[int, PendingDetection] = {}
        self._next_id = 1

    def register(
        self, ts: str, gate_score: float, wake_audio: np.ndarray
    ) -> PendingDetection:
        entry = PendingDetection(
            id=self._next_id,
            ts=ts,
            gate_score=gate_score,
            wake_audio=wake_audio,
            registered_at=time.time(),
        )
        self._entries[entry.id] = entry
        self._next_id += 1
        _LOGGER.info("Detection registered ts=%s score=%.3f", ts, gate_score)
        return entry

    def claim_latest(self, max_age_s: float) -> PendingDetection | None:
        """Remove and return the newest entry younger than max_age_s."""
        now = time.time()
        for entry in sorted(
            self._entries.values(), key=lambda e: e.registered_at, reverse=True
        ):
            if now - entry.registered_at <= max_age_s:
                del self._entries[entry.id]
                return entry
        return None

    def claim_stale(self, older_than_s: float) -> list[PendingDetection]:
        """Remove and return every entry older than older_than_s."""
        now = time.time()
        stale = [
            e for e in self._entries.values()
            if now - e.registered_at > older_than_s
        ]
        for entry in stale:
            del self._entries[entry.id]
        return sorted(stale, key=lambda e: e.registered_at)

    def discard(self, entry_id: int) -> bool:
        return self._entries.pop(entry_id, None) is not None

    def pending_ids(self) -> set[int]:
        return set(self._entries)

    def __len__(self) -> int:
        return len(self._entries)


REGISTRY = DetectionRegistry()


# ---------------------------------------------------------------------
# Near-miss phrase collection (ported from the wake server verbatim).
# ---------------------------------------------------------------------


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
    it until the speaker finishes. Fed chunk-by-chunk from feed(),
    so the audio/detection path never waits on it; only the
    Whisper verification runs as a task."""

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


async def _verify_near_miss(peak: float, audio: np.ndarray) -> None:
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


# ---------------------------------------------------------------------
# The detector.
# ---------------------------------------------------------------------


class WakeDetector:
    """One gate over one mic stream. feed() takes the session's raw
    chunks (16 kHz / 16-bit / mono) and returns a PendingDetection
    the moment the gate fires, else None. The gate is stateful, so
    one detector owns one stream -- in production that is the
    session's; benches build their own."""

    def __init__(self, *, wake_name: str = "hey_lyra_stable_v1") -> None:
        self.wake_name = wake_name
        self.gate = HeyLyraGate()
        self.armed = True
        self.rate = 16000
        self._chunks: deque = deque()
        self._buffered = 0
        self._peak = 0.0
        self._nm: _NearMissCollection | None = None
        self._nm_tasks: set = set()

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

    def _finish_near_miss(self) -> None:
        nm, self._nm = self._nm, None
        if nm is None:
            return
        audio = nm.audio()
        _LOGGER.info("Near-miss peak=%.3f; verifying complete phrase "
                     "(%.1fs)", nm.peak, len(audio) / self.rate)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # fed outside a loop: verify inline later is
            return  # not a case any caller hits; drop rather than crash
        task = loop.create_task(_verify_near_miss(nm.peak, audio))
        self._nm_tasks.add(task)
        task.add_done_callback(self._nm_tasks.discard)

    def feed(self, data: bytes) -> PendingDetection | None:
        if not data:
            return None
        samples = np.frombuffer(data, dtype=np.int16)
        self._push_audio(samples)
        score = self.gate.predict(samples)
        created = False
        entry = None
        if self.armed and score >= THRESHOLD:
            if self._nm is not None:
                # The real turn wins: the core files this
                # utterance as a positive. Never double-file.
                _LOGGER.info("Near-miss collection dropped "
                             "(peak=%.3f): real detection", self._nm.peak)
                self._nm = None
            self.armed = False
            self._peak = 0.0
            _LOGGER.info("Detection %s score=%.3f", self.wake_name, score)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            entry = REGISTRY.register(
                ts, float(score), self._snapshot(DETECTION_CLIP_SECONDS))
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
        return entry

    def event_data(self) -> dict:
        """The Detection event's data dict, as the Wyoming client
        used to deliver it to the session."""
        return {"name": self.wake_name,
                "timestamp": int(time.time() * 1_000_000_000)}


# ---------------------------------------------------------------------
# The stale sweep (the wake server's janitor, in-core).
# ---------------------------------------------------------------------


async def finalize_stale_detections(
    older_than_s: float = STALE_SECONDS,
) -> list[str]:
    """One sweep pass: detections never claimed (aborted pipeline,
    false trigger with no command) are still verified and filed
    wake-only. Returns the filed paths."""
    filed: list[str] = []
    for entry in REGISTRY.claim_stale(older_than_s):
        try:
            text = await collector.whisper_transcribe(entry.wake_audio)
            verdict = collector.classify_wake_transcription(text)
            filed.append(collector.file_clip(
                verdict, entry.gate_score, text, entry.ts, entry.wake_audio))
        except Exception:
            _LOGGER.exception("sweep finalize failed for ts=%s", entry.ts)
    return filed


async def run_detection_sweep() -> None:
    """The sweep loop production_main runs for the process's life."""
    while True:
        await asyncio.sleep(60)
        try:
            await finalize_stale_detections()
        except Exception:
            _LOGGER.exception("wake sweep error")
