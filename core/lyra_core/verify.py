"""Phase 2 verify-then-fire gate: the training pipeline, in-path.

The core transcribes the COMBINED wake+command clip directly through
the shared Whisper service (:8093) — the collector's own
``whisper_transcribe`` — runs the production collector's classifier
on that transcript in-process, and
the verdict gates the turn (see session.py's verify stage):

- fail  -> clip files to negatives/ + ledger; the turn ends silently
- maybe -> clip files to wake_maybe/ + ledger; the turn ends silently
- pass  -> clip files to positives/ + ledger (clip shape = the ring
           snapshot ending at the Detection with the command audio
           attached, assembled by the session); the transcript with the
           wake phrase stripped becomes the command.

Reuse, not fork: classification AND filing are the production
functions in ~/Documents/LyraOS/modules/wyoming/clip_collector.py, imported here. The
only adapter is sys.path plumbing — clip_collector needs httpx/numpy,
which the core's interpreter (the ESPHome venv) carries since
2026-11-19 (installed for the in-process gate); the Wyoming services
venv's site-packages (same CPython 3.14) remain appended as a
fallback. Nothing about the logic is re-implemented.

DRY-RUN (the default, and the only mode the Phase 2 benches use):
classification and transcription run for real, but filing is computed
by the REAL collector.file_clip with its two side-effect primitives —
_write_wav and append_ledger — temporarily swapped for recorders
inside this process only. The destination path and the ledger entry
are therefore exactly what production would produce, and nothing is
written. (The swap is process-local and the swapped region contains
no awaits, so no other task in this process can observe it.)

REGISTRY DISCARD (live mode only): every Detection is registered
in the pending registry (lyra_core.wake) at the instant the
in-process gate fires, and an entry left unclaimed is filed
wake-only by the stale sweep at 120 s (possibly with a wrong
verdict -- a genuine command clip in negatives/ would poison the
pool). So in live mode, once a turn has been verified and filed
here, verify() discards its own registry entry (by id), and a
core-handled turn is filed exactly once: here. If verification or
filing raises, no discard runs and the sweep's wake-only filing
remains the fallback, as for any unclaimed detection. Dry-run
never discards: benches leave the registry exactly as they found
it.

Gate score: the session was there when the gate fired, so it
hands verify() the score (and the registry id) directly -- no
read-back, no name association, no race with a newer detection.
"""
from __future__ import annotations

import glob
import logging
import re
import string
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_WYOMING_SERVICES = Path.home() / "Documents" / "LyraOS" / "modules" / "wyoming"
if str(_WYOMING_SERVICES) not in sys.path:
    sys.path.insert(0, str(_WYOMING_SERVICES))
try:
    import clip_collector as collector
except ImportError:  # core interpreter lacks httpx/numpy: borrow the venv's
    for _sp in glob.glob(
        str(_WYOMING_SERVICES / "venv" / "lib" / "python3.*" / "site-packages")
    ):
        if _sp not in sys.path:
            sys.path.append(_sp)
    import clip_collector as collector

import numpy as np

from .wake import REGISTRY

_LOGGER = logging.getLogger("lyra_core.verify")

# Clip filing timestamps follow the project's zone explicitly, like
# the router's (dial-in 2026-11-11, item M) -- the old code used a
# naive local now(), which silently depends on the process TZ.
_FILING_TZ = ZoneInfo("America/New_York")


_KEEP = set(string.ascii_lowercase + string.digits)


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Mirror collector._normalize_wake_text exactly, while tracking for
    each normalized character the index of the original character it
    came from (a collapsed separator run maps to its first character).
    The Phase 2 bench asserts string equality with the production
    normalizer over a battery of inputs."""
    norm_chars: list[str] = []
    index_map: list[int] = []
    pending_space_from: int | None = None
    for i, ch in enumerate(text):
        low = ch.lower()
        if len(low) == 1 and low in _KEEP:
            if pending_space_from is not None and norm_chars:
                norm_chars.append(" ")
                index_map.append(pending_space_from)
            pending_space_from = None
            norm_chars.append(low)
            index_map.append(i)
        elif pending_space_from is None:
            pending_space_from = i
    return "".join(norm_chars), index_map


def strip_wake_phrase(transcript: str) -> str:
    """Remove the matched wake phrase (one of the collector's own pass
    patterns) from the transcript; the remainder is the command, in the
    transcript's original characters (casing and final punctuation
    survive). When the wake phrase occurs more than once -- the user
    repeated the ask inside one capture -- the command is the text
    after the LAST occurrence: the final ask wins. Returns '' when no
    pass pattern is present or nothing follows the phrase."""
    norm, index_map = _normalize_with_map(transcript)
    end_norm: int | None = None  # end of the LAST wake-phrase match
    for pattern in collector._WAKE_PASS_PATTERNS:
        if not pattern:
            continue
        start = 0
        while True:
            idx = norm.find(pattern, start)
            if idx < 0:
                break
            end = idx + len(pattern)
            if end_norm is None or end > end_norm:
                end_norm = end
            start = idx + 1
    if end_norm is None:
        return ""
    if end_norm >= len(index_map):
        return ""
    rest = transcript[index_map[end_norm]:]
    return re.sub(r"^[^A-Za-z0-9]+", "", rest).strip()


@dataclass
class VerifyResult:
    verdict: str                # 'pass' | 'fail' | 'maybe' (collector's words)
    transcript: str             # combined-clip transcript (the routing truth)
    command: str | None         # wake phrase stripped; only on pass
    gate_score: float           # score used for the filing name/ledger
    dry_run: bool
    filed_path: str             # dest file_clip returned (would-be in dry-run)
    would_ledger: dict | None   # ledger kwargs captured in dry-run
    audio_seconds: float


class VerifyGate:
    """Transcribe -> classify -> file (or would-file), in-process."""

    def __init__(self, *, dry_run: bool = True) -> None:
        self._dry_run = dry_run

    @property
    def dry_run(self) -> bool:
        return self._dry_run

    async def verify(
        self,
        combined_pcm: bytes,
        *,
        gate_score: float | None = None,
        detection_id: int | None = None,
    ) -> VerifyResult:
        audio = np.frombuffer(combined_pcm, dtype="<i2").astype(np.int16)
        if len(audio) == 0:
            transcript = ""
        else:
            # The production direct call to Whisper :8093 — the
            # same function the collector's other paths verify with.
            transcript = await collector.whisper_transcribe(audio)
            _LOGGER.info(
                "verify transcript [whisper-turbo]: %r", transcript)
        verdict = collector.classify_wake_transcription(transcript)
        score = gate_score if gate_score is not None else 0.0
        ts = datetime.now(_FILING_TZ).strftime("%Y%m%d_%H%M%S")
        would_ledger: dict | None = None
        if self._dry_run:
            written: list[tuple[str, int]] = []
            ledger_kwargs: list[dict] = []
            orig_write = collector._write_wav
            orig_append = collector.append_ledger
            collector._write_wav = (
                lambda path, audio_i16: written.append((path, len(audio_i16)))
            )
            collector.append_ledger = lambda **kw: ledger_kwargs.append(kw)
            try:
                dest = collector.file_clip(verdict, score, transcript, ts, audio)
            finally:
                collector._write_wav = orig_write
                collector.append_ledger = orig_append
            would_ledger = ledger_kwargs[0] if ledger_kwargs else None
            _LOGGER.info(
                "DRY-RUN would file (%s): %s :: %r", verdict, dest, transcript
            )
        else:
            dest = collector.file_clip(verdict, score, transcript, ts, audio)
        if not self._dry_run and detection_id is not None:
            # Live: this turn is filed; drop its registry entry so
            # the stale sweep does not file it again (module docstring).
            REGISTRY.discard(detection_id)
        command = strip_wake_phrase(transcript) if verdict == "pass" else None
        return VerifyResult(
            verdict=verdict,
            transcript=transcript,
            command=command,
            gate_score=score,
            dry_run=self._dry_run,
            filed_path=dest,
            would_ledger=would_ledger,
            audio_seconds=len(audio) / collector.SAMPLE_RATE,
        )


async def finalize_pending_detection(command_pcm: bytes) -> None:
    """File the newest pending wake detection with this command
    audio attached -- the collector's fresh-claim path. Until
    2026-11-19 this lived in the Wyoming STT bridge, which ran it
    after every transcription it served; the bridge is gone and
    the core is the transcriber now, so PipelineDriver's
    on_transcribed hook (wired in production_main and the session
    benches) calls this instead. In production the VerifyGate
    files its own turns and discards their registry entries first,
    so this finds nothing on gate-handled turns -- it is the
    backstop for gate-less sessions, exactly as it was under the
    bridge. Never raises."""
    try:
        entry = REGISTRY.claim_latest(max_age_s=120)
        if entry is None:
            return
        command_audio = np.frombuffer(
            command_pcm, dtype="<i2").astype(np.int16)
        combined = np.concatenate([entry.wake_audio, command_audio])
        text = await collector.whisper_transcribe(combined)
        verdict = collector.classify_wake_transcription(text)
        collector.file_clip(verdict, entry.gate_score, text,
                            entry.ts, combined)
    except Exception:
        _LOGGER.exception("collector finalize failed")
