"""Phase 2 verify-then-fire gate: the training pipeline, in-path.

The core transcribes the COMBINED wake+command clip directly through
the shared whisper.cpp service (:8093) — the collector's own
``whisper_transcribe``, not the Wyoming STT spool/claim path — runs the
production collector's classifier on that transcript in-process, and
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
which the core's interpreter (the ESPHome venv) does not carry; the
Wyoming services venv (same CPython 3.14) does, so its site-packages
are appended as a fallback. Nothing about the logic is re-implemented.

DRY-RUN (the default, and the only mode the Phase 2 benches use):
classification and transcription run for real, but filing is computed
by the REAL collector.file_clip with its two side-effect primitives —
_write_wav and append_ledger — temporarily swapped for recorders
inside this process only. The destination path and the ledger entry
are therefore exactly what production would produce, and nothing is
written. (The production wake/STT processes are separate processes;
the swap cannot leak into them.)

SPOOL CLAIM+DISCARD (live mode only): the wake server spools every
Detection for the STT-server claim path, which the core never uses;
left alone, its janitor would file this turn a second time at 120 s
(wake-only snapshot, possibly with a wrong verdict -- a genuine
command clip in negatives/ would poison the pool). So in live mode,
once a turn has been verified and filed here, the gate claims its
own spool entry BY NAME (see SPOOL ASSOCIATION below: the
collector's own atomic _claim on that entry, with
collector.claim_latest_detection as the no-name fallback) and
discards the wake wav
(collector.discard_wake_wav -- the STT server's finally-path). The
claim's atomic rename removes the meta the janitor looks for, so a
core-handled turn is filed exactly once: here. If verification or
filing raises, no claim runs and the janitor's wake-only filing
remains the fallback, as for any unclaimed detection. Dry-run
never claims: benches leave the spool exactly as they found it.

Gate score: the Wyoming Detection event carries no score, so the
score is read back from the wake server's spool meta (a plain read
— never the rename-based claim) for the detection the spool
recorded moments earlier.

SPOOL ASSOCIATION (dial-in 2026-11-11, item N): selecting “the
newest spool entry” is racy — a detection spooled while this turn
is being verified could lend this turn its score, and the claim
could steal that newer entry and discard its wav. The fix uses
data the gate already has: the wake server names each spool entry
with the local second of its detection, and the session stamps
each accepted Detection with the local second it arrived (same
host clock). verify() receives that stamp as `spool_ts` and both
the score read and the claim select the entry BY NAME (this
second, or one second earlier for a boundary straddle) — the claim
via the collector's own atomic _claim primitive on that specific
entry. The newest-entry behaviour survives only as the fallback
when no named entry exists. No collector or spool-format changes.
"""
from __future__ import annotations

import glob
import json
import logging
import re
import string
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
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

_LOGGER = logging.getLogger("lyra_core.verify")

# Clip filing timestamps follow the project's zone explicitly, like
# the router's (dial-in 2026-11-11, item M) -- the old code used a
# naive local now(), which silently depends on the process TZ.
_FILING_TZ = ZoneInfo("America/New_York")


def _prev_second(ts: str) -> str | None:
    """The spool-name second before `ts` (or None if unparseable)."""
    try:
        dt = datetime.strptime(ts, "%Y%m%d_%H%M%S") - timedelta(seconds=1)
    except ValueError:
        return None
    return dt.strftime("%Y%m%d_%H%M%S")

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

    def _spool_gate_score(self, spool_ts: str | None = None) -> float | None:
        """Read-only peek at the spool meta (never a claim): the
        wake server spooled this turn's Detection just before the core
        saw it. With `spool_ts` (item N), read THIS turn's named
        entry (this second, or one earlier on a boundary straddle);
        if it is gone, return None rather than borrowing a neighbor
        detection's score. Without it, the legacy newest-fresh scan.
        Returns None when nothing fresh is there."""
        if spool_ts is not None:
            for name in (spool_ts, _prev_second(spool_ts)):
                if name is None:
                    continue
                try:
                    meta = json.loads(
                        (Path(collector.SPOOL_DIR) / f"{name}.json").read_text()
                    )
                    return float(meta["gate_score"])
                except Exception:
                    continue
            return None
        try:
            metas = sorted(
                Path(collector.SPOOL_DIR).glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return None
        for meta_path in metas:
            try:
                if time.time() - meta_path.stat().st_mtime > 60:
                    break
                meta = json.loads(meta_path.read_text())
                return float(meta["gate_score"])
            except Exception:
                continue
        return None

    def claim_and_discard_spool(self, spool_ts: str | None = None) -> None:
        """Claim this turn's spool entry and discard its wake wav.

        Mirrors the STT server's claim path (claim_latest_detection
        with max_age_s=120 -- the janitor's own staleness threshold,
        so a verified turn always wins the race) and its discard
        (discard_wake_wav, the STT server's finally-path). Never
        raises: a failed claim leaves the entry for the janitor,
        which is the pre-fix behaviour, not a new failure.
        """
        if spool_ts is not None:
            # Item N: claim THIS turn's entry by name, via the
            # collector's own atomic claim primitive, so a newer
            # detection spooled during this turn's verify is never
            # stolen. If the named entry is already gone, someone
            # else handled it -- do not fall through to claiming a
            # different (newer) entry in that case.
            for name in (spool_ts, _prev_second(spool_ts)):
                if name is None:
                    continue
                meta_path = Path(collector.SPOOL_DIR) / f"{name}.json"
                if not meta_path.exists():
                    continue
                try:
                    got = collector._claim(str(meta_path))
                except Exception:
                    _LOGGER.exception("spool claim failed")
                    return
                if got is None:
                    _LOGGER.warning(
                        "spool entry %s claimed by someone else first", name
                    )
                    return
                meta, wake_wav = got
                collector.discard_wake_wav(wake_wav)
                _LOGGER.info(
                    "spool entry claimed + discarded (ts=%s)", meta.get("ts")
                )
                return
            _LOGGER.warning(
                "no spool entry named for ts=%s; using latest-claim fallback",
                spool_ts,
            )
        try:
            got = collector.claim_latest_detection(max_age_s=120)
        except Exception:
            _LOGGER.exception("spool claim failed")
            return
        if got is None:
            _LOGGER.warning("no spool entry to claim after verify")
            return
        meta, wake_wav = got
        collector.discard_wake_wav(wake_wav)
        _LOGGER.info("spool entry claimed + discarded (ts=%s)", meta.get("ts"))

    async def verify(
        self,
        combined_pcm: bytes,
        *,
        gate_score: float | None = None,
        spool_ts: str | None = None,
    ) -> VerifyResult:
        audio = np.frombuffer(combined_pcm, dtype="<i2").astype(np.int16)
        if len(audio) == 0:
            transcript = ""
        else:
            # The production direct call to whisper.cpp :8093 — the same
            # function the STT server uses to verify spooled clips.
            transcript = await collector.whisper_transcribe(audio)
        verdict = collector.classify_wake_transcription(transcript)
        score = gate_score
        if score is None:
            score = self._spool_gate_score(spool_ts)
        if score is None:
            score = 0.0
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
        if not self._dry_run:
            # Live: this turn is filed; keep the wake server's janitor
            # from filing it a second time (see module docstring).
            self.claim_and_discard_spool(spool_ts)
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
