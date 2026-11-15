"""Core-side speech endpointer: derives STT VAD start/end from the mic
stream's energy.

Why this exists: the Lyra Flex firmware releases its XVF3800 beam lock
on the server-sent STT_VAD_END event and tracks LED phases off
STT_VAD_START/END (Phase 1a design SS1/SS3), so the core — like HA's
pipeline driver before it — must endpoint the utterance itself. Only
two decisions are needed: speech has begun (VAD_START) and speech has
been absent long enough that the utterance is over (VAD_END, after a
~700 ms hangover comparable to HA's behaviour today).

Method: 30 ms frames with TWO energy arms over the same audio — a
broadband RMS arm and a speech-band arm (frame RMS after two cascaded
one-pole high-pass sections at ~300 Hz). Each arm keeps the same
decision logic: windowed minimum-statistics noise floor (10th
percentile of the last ~4 s of non-speech frame levels) with
hysteresis — start threshold = clamp(floor * 1.5, min, max), end
threshold = 0.8 * start. Speech onset uses a leaky counter (+2 per
loud frame, -1 per quiet frame) so brief inter-word dips don't
fragment the onset; the end counter only resets on frames above the
START threshold, so the hysteresis band holds the hangover. VAD_START
fires when either arm declares speech; VAD_END fires when either arm
completes its hangover.

Calibration evidence (2026-11-11): the production pool clip used by
the benches (wake_positive_20261111_061439_0.987.wav, real box audio)
has a noise bed around RMS 720-870 and speech peaks 1700-3150 — a
fixed low threshold would sit permanently "in speech" on real room
audio, which is why the floor is adaptive rather than a constant.

Live failure (2026-11-11 16:47): the original raw-minimum floor
dropped instantly to any single frame's RMS, and sparse near-silent
outlier frames in the box's processed audio collapsed it to ~323
against a true bed of ~875. Frozen there at VAD_START, its end
threshold (~388) sat below the bed, so silence never registered and
captures ran to the 30 s cap, merging separate asks into one
unroutable transcript. A windowed percentile cannot be poisoned by
sparse outliers.

Live failure (2026-11-11 19:40): the percentile-floor fix was not
enough for the sat path. The broadband bed during a capture is
dominated by sub-300 Hz rumble (in the incident clip the bed's
350-3200 Hz band carries only ~15% of its RMS) whose level wanders
~2x on ten-second scales: frozen floor 594 gave an end threshold of
713 while the in-capture bed sat at median ~1000 — most non-speech
frames RESET the silence counter and the rest held it (longest
sub-threshold run during the asks: 5 of the 23 frames needed), so
two asks 14 s apart merged into one 30 s capture. In the speech
band the same bed is tight and stationary (gap p50 165, max 214,
vs speech peaks 800-1700), so the band arm endpoints where the
broadband arm is blind; the broadband arm still endpoints the
deep-dip pauses of quieter paths, where it always worked.

The endpointer is armed per capture (begin_capture at wake Detection)
and disarmed at run end; while disarmed it only tracks the floors.
"""
from __future__ import annotations

import array
import math
from collections import deque
from dataclasses import dataclass

VAD_START = "vad_start"
VAD_END = "vad_end"


@dataclass
class EndpointerConfig:
    rate: int = 16000
    width: int = 2  # bytes per sample (int16)
    frame_ms: int = 30
    start_ratio: float = 1.5
    min_start_rms: float = 250.0
    max_start_rms: float = 8000.0
    end_ratio: float = 0.8  # end threshold = start threshold * end_ratio
    onset_target: int = 4  # leaky-counter value that declares VAD_START
    silence_end_ms: int = 700  # hangover after speech before VAD_END
    floor_window_s: float = 4.0  # sliding window for floor statistics
    floor_percentile: float = 0.10  # floor = this percentile of the window
    # Speech-band arm: same logic over high-passed frame energy, with
    # clamps on the band's (much smaller) level scale.
    hp_cutoff_hz: float = 300.0
    band_min_start_rms: float = 90.0
    band_max_start_rms: float = 3000.0


class _Arm:
    """One energy arm: percentile floor + hysteresis onset/end
    counters over a per-frame level series."""

    def __init__(self, cfg: EndpointerConfig, min_start: float, max_start: float) -> None:
        self._cfg = cfg
        self._min_start = min_start
        self._max_start = max_start
        self._silence_frames_needed = max(
            1, round(cfg.silence_end_ms / cfg.frame_ms)
        )
        self._floor_window: deque[float] = deque(
            maxlen=max(1, round(cfg.floor_window_s * 1000 / cfg.frame_ms))
        )
        self._floor: float | None = None
        self.in_speech = False
        self._onset = 0
        self._silence = 0

    @property
    def noise_floor(self) -> float | None:
        return self._floor

    def thresholds(self) -> tuple[float, float]:
        floor = self._floor or 0.0
        start = min(
            self._max_start,
            max(self._min_start, floor * self._cfg.start_ratio),
        )
        return start, start * self._cfg.end_ratio

    def reset_capture(self) -> None:
        self.in_speech = False
        self._onset = 0
        self._silence = 0

    def track_floor(self, level: float) -> None:
        # Windowed minimum statistics: the floor is a low percentile of
        # the recent non-speech frames in the sliding window, NOT the
        # raw minimum. Sparse near-silent outlier frames in the box's
        # processed audio must not be able to collapse the estimate
        # (live failure 2026-11-11: floor 323 vs a true bed of ~875,
        # thresholds below the bed, VAD_END unreachable). A genuinely
        # rising bed still raises the floor: quiet frames age out of
        # the window within floor_window_s.
        self._floor_window.append(level)
        ordered = sorted(self._floor_window)
        rank = max(1, math.ceil(self._cfg.floor_percentile * len(ordered)))
        self._floor = ordered[rank - 1]

    def feed_frame(self, level: float) -> str | None:
        """Advance the arm by one frame while armed; return VAD_START,
        VAD_END, or None (arm-local events; the parent unions them)."""
        start_thr, end_thr = self.thresholds()
        if not self.in_speech:
            if level >= start_thr:
                self._onset += 2
                if self._onset >= self._cfg.onset_target:
                    self.in_speech = True
                    self._silence = 0
                    return VAD_START
            elif self._onset > 0:
                self._onset -= 1
        else:
            if level >= start_thr:
                self._silence = 0
            elif level < end_thr:
                self._silence += 1
                if self._silence >= self._silence_frames_needed:
                    self.in_speech = False
                    self._onset = 0
                    return VAD_END
            # Frames inside the hysteresis band hold the hangover.
        return None


class Endpointer:
    def __init__(self, config: EndpointerConfig | None = None) -> None:
        self._cfg = config or EndpointerConfig()
        self._frame_bytes = (
            self._cfg.rate * self._cfg.frame_ms // 1000
        ) * self._cfg.width
        self._silence_frames_needed = max(
            1, round(self._cfg.silence_end_ms / self._cfg.frame_ms)
        )
        self._buf = bytearray()
        self._broad = _Arm(self._cfg, self._cfg.min_start_rms, self._cfg.max_start_rms)
        self._band = _Arm(self._cfg, self._cfg.band_min_start_rms, self._cfg.band_max_start_rms)
        # Speech-band filter state: two cascaded one-pole high-pass
        # sections, stateful over the whole stream (frames in order).
        rc = 1.0 / (2.0 * math.pi * self._cfg.hp_cutoff_hz)
        dt = 1.0 / self._cfg.rate
        self._hp_alpha = rc / (rc + dt)
        self._hp_x1a = 0.0
        self._hp_y1 = 0.0
        self._hp_x1b = 0.0
        self._hp_y2 = 0.0
        self._armed = False
        self._in_speech = False

    # -- introspection (bench + logs) --------------------------------------

    @property
    def in_speech(self) -> bool:
        return self._in_speech

    @property
    def noise_floor(self) -> float | None:
        return self._broad.noise_floor

    @property
    def band_noise_floor(self) -> float | None:
        return self._band.noise_floor

    def thresholds(self) -> tuple[float, float]:
        return self._broad.thresholds()

    def band_thresholds(self) -> tuple[float, float]:
        return self._band.thresholds()

    # -- capture lifecycle ---------------------------------------------------

    def begin_capture(self) -> None:
        self._armed = True
        self._in_speech = False
        self._broad.reset_capture()
        self._band.reset_capture()
        self._buf.clear()

    def end_capture(self) -> None:
        self._armed = False
        self._in_speech = False
        self._broad.reset_capture()
        self._band.reset_capture()

    # -- streaming -------------------------------------------------------------

    def _frame_levels(self, frame: bytes) -> tuple[float, float]:
        samples = array.array("h")
        samples.frombytes(frame)  # PCM is little-endian; so is the host
        if not samples:
            return 0.0, 0.0
        a = self._hp_alpha
        x1a, y1, x1b, y2 = self._hp_x1a, self._hp_y1, self._hp_x1b, self._hp_y2
        acc_broad = 0.0
        acc_band = 0.0
        for x in samples:
            acc_broad += x * x
            v = a * (y1 + x - x1a)
            x1a, y1 = float(x), v
            w = a * (y2 + v - x1b)
            x1b, y2 = v, w
            acc_band += w * w
        self._hp_x1a, self._hp_y1, self._hp_x1b, self._hp_y2 = x1a, y1, x1b, y2
        n = len(samples)
        return math.sqrt(acc_broad / n), math.sqrt(acc_band / n)

    def feed(self, pcm: bytes) -> list[str]:
        """Consume stream audio; return VAD events fired by these bytes
        (subset of {VAD_START, VAD_END}, in order)."""
        events: list[str] = []
        self._buf.extend(pcm)
        while len(self._buf) >= self._frame_bytes:
            frame = bytes(self._buf[: self._frame_bytes])
            del self._buf[: self._frame_bytes]
            broad_rms, band_rms = self._frame_levels(frame)
            if not self._armed:
                # The floors learn only from the idle (disarmed) stream.
                # Arming a capture freezes the estimates for the whole
                # capture: an utterance's own inter-word dips otherwise
                # enter the window during the armed pre-speech phase
                # and drag the percentile down mid-capture, pushing the
                # end threshold back under the bed.
                self._broad.track_floor(broad_rms)
                self._band.track_floor(band_rms)
                continue
            ev_broad = self._broad.feed_frame(broad_rms)
            ev_band = self._band.feed_frame(band_rms)
            if not self._in_speech:
                if ev_broad == VAD_START or ev_band == VAD_START:
                    self._in_speech = True
                    events.append(VAD_START)
            else:
                if ev_broad == VAD_END or ev_band == VAD_END:
                    self._in_speech = False
                    events.append(VAD_END)
        return events


def _self_check() -> None:
    """Synthetic proof: quiet bed -> tone burst -> quiet bed must yield
    exactly [VAD_START, VAD_END]. Never touches any service or device."""
    import math as _math

    ep = Endpointer()
    rate = ep._cfg.rate
    frame_bytes = ep._frame_bytes

    def tone(seconds: float, amp: float, freq: float = 440.0) -> bytes:
        n = int(rate * seconds)
        out = array.array(
            "h",
            (int(amp * _math.sin(2 * _math.pi * freq * i / rate)) for i in range(n)),
        )
        return out.tobytes()

    def quiet(seconds: float) -> bytes:
        return tone(seconds, 0.0)

    events: list[str] = []
    # 1 s of silence while disarmed: floor seeds, no events possible.
    events += ep.feed(quiet(1.0))
    assert not events, events
    ep.begin_capture()
    events += ep.feed(quiet(0.5))
    events += ep.feed(tone(1.0, 6000.0))  # burst, RMS ~4240
    still_open = ep.in_speech
    events += ep.feed(quiet(1.5))
    ep.end_capture()
    assert still_open, "burst never declared speech"
    assert events == [VAD_START, VAD_END], events
    start_thr, end_thr = ep.thresholds()
    print(
        "endpointer self-check OK: "
        f"events={events}, floor={ep.noise_floor:.0f}, "
        f"thresholds start={start_thr:.0f} end={end_thr:.0f}; "
        f"frame={frame_bytes} B, silence_frames={ep._silence_frames_needed}"
    )


if __name__ == "__main__":
    _self_check()
