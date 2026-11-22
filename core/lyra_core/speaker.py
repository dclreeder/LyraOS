"""Speaker marking (2026-11-22): whose voice is this turn?

Daniel's ask: turns marked per speaker, and Lyra recognizes his
voice from a friend's. The wake VerifyGate cannot do this -- it
verifies the wake PHRASE in the transcript, not the voice. This
module is the identity layer: a speaker-embedding model (WeSpeaker
ECAPA-TDNN 512, VoxCeleb, official ONNX export) embeds each routed
turn's audio and compares it against Daniel's enrolled voiceprint
(an average embedding over his filed wake clips).

Labels are attribution, not access control: a PASS turn is answered
regardless. The label rides the VerifyResult into the router, which
stamps her context and the memory record, so a guest's words are
never curated as Daniel's. Everything fails silent to "unknown":
a missing model, a missing voiceprint, or too-short audio can
never break or block a turn.
"""
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

_LOGGER = logging.getLogger(__name__)

_DATA = Path(__file__).resolve().parent.parent.parent / "data" / "speaker"
MODEL_PATH = _DATA / "voxceleb_ECAPA512_LM.onnx"
VOICEPRINT_PATH = _DATA / "daniel_voiceprint.npy"
THRESHOLD_PATH = _DATA / "threshold.txt"

SAMPLE_RATE = 16000
_FRAME_LEN = int(0.025 * SAMPLE_RATE)   # 25 ms
_FRAME_SHIFT = int(0.010 * SAMPLE_RATE)  # 10 ms
_NUM_MELS = 80
_MIN_SECONDS = 1.0


def fbank(pcm_i16: np.ndarray) -> np.ndarray:
    """Kaldi-style 80-dim log-mel features, (T, 80) float32.

    One implementation shared by enrollment and runtime so the two
    can never drift: preemphasis 0.97, 25/10 ms frames, Hamming,
    512-pt FFT, mel filterbank 20 Hz..Nyquist, log, then per-column
    mean normalization over the utterance (the CMN the WeSpeaker
    recipe applies). Deterministic -- no dither."""
    x = pcm_i16.astype(np.float32) / 32768.0
    if len(x) < _FRAME_LEN:
        return np.zeros((0, _NUM_MELS), dtype=np.float32)
    x = np.append(x[0], x[1:] - 0.97 * x[:-1])
    n_frames = 1 + (len(x) - _FRAME_LEN) // _FRAME_SHIFT
    idx = (np.arange(_FRAME_LEN)[None, :]
           + _FRAME_SHIFT * np.arange(n_frames)[:, None])
    frames = x[idx] * np.hamming(_FRAME_LEN).astype(np.float32)
    power = np.abs(np.fft.rfft(frames, n=512)) ** 2 / 512.0
    # mel filterbank
    low, high = 20.0, SAMPLE_RATE / 2.0
    mel = lambda f: 2595.0 * np.log10(1.0 + f / 700.0)
    inv = lambda m: 700.0 * (10.0 ** (m / 2595.0) - 1.0)
    pts = inv(np.linspace(mel(low), mel(high), _NUM_MELS + 2))
    bins = np.floor((512 + 1) * pts / SAMPLE_RATE).astype(int)
    fb = np.zeros((_NUM_MELS, 257), dtype=np.float32)
    for m in range(1, _NUM_MELS + 1):
        left, center, right = bins[m - 1], bins[m], bins[m + 1]
        for k in range(left, center):
            fb[m - 1, k] = (k - left) / max(1, center - left)
        for k in range(center, right):
            fb[m - 1, k] = (right - k) / max(1, right - center)
    feats = np.log(np.maximum(power @ fb.T, 1e-10)).astype(np.float32)
    feats -= feats.mean(axis=0, keepdims=True)
    return feats


class SpeakerGate:
    """Embed a turn's audio and label it daniel / guest / unknown.

    Lazy: the ONNX session and voiceprint load on first use, so
    benches and tests that never wire the gate pay nothing."""

    def __init__(self, model_path: Path = MODEL_PATH,
                 voiceprint_path: Path = VOICEPRINT_PATH,
                 threshold_path: Path = THRESHOLD_PATH) -> None:
        self._model_path = Path(model_path)
        self._voiceprint_path = Path(voiceprint_path)
        self._threshold_path = Path(threshold_path)
        self._session = None
        self._voiceprint: np.ndarray | None = None
        self._threshold: float | None = None
        self._broken = False

    def _load_model(self) -> bool:
        if self._session is not None:
            return True
        if self._broken:
            return False
        try:
            import onnxruntime as ort
            self._session = ort.InferenceSession(
                str(self._model_path), providers=["CPUExecutionProvider"])
            return True
        except Exception:
            _LOGGER.exception("speaker model unavailable")
            self._broken = True
            return False

    def _load(self) -> bool:
        if self._voiceprint is not None:
            return True
        if not self._load_model():
            return False
        try:
            vp = np.load(self._voiceprint_path).astype(np.float32)
            self._voiceprint = vp / np.linalg.norm(vp)
            self._threshold = float(
                self._threshold_path.read_text().strip())
            _LOGGER.info("speaker gate loaded (threshold %.3f)",
                         self._threshold)
            return True
        except Exception:
            _LOGGER.exception("speaker gate unavailable; labels unknown")
            self._broken = True
            return False

    def embed(self, pcm: bytes) -> np.ndarray | None:
        """192-dim L2-normalized embedding for int16 mono 16k PCM,
        or None when the audio is unusable. Never raises."""
        try:
            if not self._load_model():
                return None
            audio = np.frombuffer(pcm, dtype="<i2").astype(np.int16)
            if len(audio) < int(_MIN_SECONDS * SAMPLE_RATE):
                return None
            feats = fbank(audio)
            if len(feats) < 10:
                return None
            out = self._session.run(
                None, {"feats": feats[None, :, :]})[0][0]
            return (out / np.linalg.norm(out)).astype(np.float32)
        except Exception:
            _LOGGER.exception("speaker embed failed")
            return None

    def label(self, pcm: bytes) -> tuple[str, float]:
        """(label, cosine score). 'daniel' at or above threshold,
        'guest' below it, 'unknown' when no judgment was possible."""
        if not self._load():
            return "unknown", 0.0
        emb = self.embed(pcm)
        if emb is None or self._voiceprint is None or self._threshold is None:
            return "unknown", 0.0
        score = float(emb @ self._voiceprint)
        return ("daniel" if score >= self._threshold else "guest"), score
