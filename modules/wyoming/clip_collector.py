#!/usr/bin/env python3
"""Clip collector for the wake path (Lyra, 2026-11-10).

Implements the wakeword module's clip-save contract
(~/Documents/LyraOS/modules/wakeword/README.md), mirroring the
deleted bridge's filing logic
(localsat_service.py: classify_wake_transcription, _file_wake_clip,
_append_clip_ledger, miss-miner):

- verify pass  -> data/clips/positives/   wake_positive_<ts>_<score>.wav
- verify fail  -> data/clips/negatives/   wake_negative_<ts>_<score>.wav
- verify maybe -> data/clips/wake_maybe/  wake_maybe_<ts>_<score>.wav
- Parakeet heard the phrase but the gate never fired (sub-threshold
  near-miss with a 'pass' transcript) -> data/clips/missed triggers/
  missed_trigger_<ts>_<score>.wav, ledger verdict 'missed'.
- Every save appends one JSON line to data/clips/clips_ledger.jsonl
  (ts, verdict, gate_score, transcript, path relative to clips dir).

Flow (in-core since 2026-11-19): the core's wake detector
(lyra_core.wake) registers each detection (wake-phrase audio +
gate score) in the pending registry; when a command transcription
completes, the core claims the entry, concatenates wake + command
audio (positives keep attached command audio), verifies the
combined clip through the shared Parakeet service (:8096),
classifies the transcript, and files it. Detections never claimed
are finalized wake-only by the core's stale sweep. (Until
2026-11-19 the same flow ran through an on-disk spool between the
Wyoming wake and STT servers; the spool and its claim protocol
are gone.)
"""
import io
import json
import logging
import os
import re
import wave
from datetime import datetime

import httpx
import numpy as np

_LOGGER = logging.getLogger("lyra-collector")

CLIP_DIR = os.path.expanduser("~/Documents/LyraOS/modules/wakeword/data/clips")
POSITIVE_DIR = os.path.join(CLIP_DIR, "positives")
NEGATIVE_DIR = os.path.join(CLIP_DIR, "negatives")
MAYBE_DIR = os.path.join(CLIP_DIR, "wake_maybe")
MISSED_DIR = os.path.join(CLIP_DIR, "missed triggers")
LEDGER = os.path.join(CLIP_DIR, "clips_ledger.jsonl")
# STT backend (2026-11-17): Parakeet TDT 0.6B v2 served by
# parakeet-server (parakeet-server.service, :8096; OpenAI-compatible
# endpoint). Parakeet's home is ~/Documents/parakeet.cpp -- it lives
# outside this tree on purpose. Whisper (whisper.cpp, :8093) was
# removed from the project 2026-11-17 at Daniel's order; this is
# the only transcription backend now.
PARAKEET_URL = "http://127.0.0.1:8096/v1/audio/transcriptions"
SAMPLE_RATE = 16000

# --- Verification classifier (verbatim from localsat_service.py) ---

def _normalize_wake_text(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text

_WAKE_PASS_PATTERNS = [
    "hey lyra", "hey lara", "hey lira", "hey lyrah", "hey lyraa",
    "a lyra", "hey laira", "hey leera", "hey lyre", "hey lura",
    "hey lyraa", "ay lyra", "he lyra", "heylyra",
    "hilara", "heylara", "heylra", "hilra", "heylira",
    # Daniel 2026-10-21: Whisper hears his casual rendition as "Larry".
    "hey larry",
]

_LYRA_LIKE = ["lyra", "lara", "lira", "lyrah", "laira", "leera",
              "lyre", "lura", "lyraa", "lyrah", "larry", "elirah"]

def classify_wake_transcription(text: str) -> str:
    """Three-tier: 'pass', 'fail', or 'maybe'."""
    norm = _normalize_wake_text(text)
    if not norm:
        return "fail"
    for pattern in _WAKE_PASS_PATTERNS:
        if pattern in norm:
            return "pass"
    for word in _LYRA_LIKE:
        if word in norm.split():
            return "maybe"
    return "fail"

# --- Filing (mirrors _file_wake_clip / _append_clip_ledger) ---

def append_ledger(**entry) -> None:
    """Best-effort ledger append; must never break the voice path."""
    try:
        entry = {"ts": datetime.now().isoformat(timespec="seconds"), **entry}
        with open(LEDGER, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        _LOGGER.exception("ledger append failed")

def _write_wav(path: str, audio_i16: np.ndarray) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(np.ascontiguousarray(audio_i16, dtype="<i2").tobytes())

def file_clip(verdict: str, gate_score: float, transcript: str, ts: str,
              audio_i16: np.ndarray) -> str:
    """File a wake clip into the verdict's pool + ledger."""
    if verdict == "pass":
        dest = os.path.join(POSITIVE_DIR, f"wake_positive_{ts}_{gate_score:.3f}.wav")
    elif verdict == "fail":
        dest = os.path.join(NEGATIVE_DIR, f"wake_negative_{ts}_{gate_score:.3f}.wav")
    else:  # maybe
        dest = os.path.join(MAYBE_DIR, f"wake_maybe_{ts}_{gate_score:.3f}.wav")
    _write_wav(dest, audio_i16)
    append_ledger(verdict=verdict, gate_score=round(gate_score, 3),
                  transcript=transcript,
                  path=os.path.relpath(dest, CLIP_DIR))
    _LOGGER.info("Wake clip filed (%s, %.1fs): %s :: %r", verdict,
                 len(audio_i16) / SAMPLE_RATE, dest, transcript)
    return dest

def file_missed(gate_score: float, transcript: str, ts: str,
                audio_i16: np.ndarray) -> str:
    """File a sub-threshold near-miss whose transcript confirms the
    wake phrase (the gate missed a real wake)."""
    fname = f"missed_trigger_{ts}_{gate_score:.3f}.wav"
    dest = os.path.join(MISSED_DIR, fname)
    _write_wav(dest, audio_i16)
    append_ledger(verdict="missed", gate_score=round(gate_score, 3),
                  transcript=transcript,
                  path=os.path.join("missed triggers", fname))
    _LOGGER.info("Missed trigger filed (gate %.3f): %s :: %r",
                 gate_score, dest, transcript)
    return dest

# --- Parakeet verification (same call shape as stt_server.py) ---

def wav_bytes(audio_i16: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(np.ascontiguousarray(audio_i16, dtype="<i2").tobytes())
    return buf.getvalue()

async def parakeet_transcribe(audio_i16: np.ndarray) -> str:
    data = wav_bytes(audio_i16)
    async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
        resp = await client.post(
            PARAKEET_URL, files={"file": ("audio.wav", data, "audio/wav")})
        resp.raise_for_status()
        return (resp.json().get("text") or "").strip()

def read_wav(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        frames = wf.readframes(wf.getnframes())
    return np.frombuffer(frames, dtype="<i2").astype(np.int16)
