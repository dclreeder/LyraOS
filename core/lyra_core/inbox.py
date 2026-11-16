"""The inbox: Phase 0 of the memory build (LyraOS/memory/PLAN.md).

Append-only daily JSONL log of conversation turns and notable
events, written by code from what actually happened -- never by
the model. One file per day under ~/Documents/LyraOS/memory/inbox/,
named by its America/New_York date (YYYY-MM-DD.jsonl); a new file
starts each day, rolled by date check on every write, so no
scheduler is involved.

This is the record the future sleep-cycle curator reads. Nothing
in here is a summary: live-context compaction summaries are never
written to the inbox (the seal is by construction -- the log holds
turns and events only).

Logging must never break a turn: every write is guarded, and a
failure is journaled and swallowed.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_LOGGER = logging.getLogger(__name__)
_TZ = ZoneInfo("America/New_York")
_DEFAULT_DIR = Path("~/Documents/LyraOS/memory/inbox").expanduser()


class InboxLog:
    """Append entries to the current day's inbox file."""

    def __init__(self, directory: Path | str = _DEFAULT_DIR) -> None:
        self._dir = Path(directory).expanduser()
        self._fh = None
        self._open_date = None

    def log(self, entry: dict) -> None:
        try:
            now = datetime.now(_TZ)
            if self._fh is None or now.date() != self._open_date:
                self._close()
                self._dir.mkdir(parents=True, exist_ok=True)
                path = self._dir / f"{now:%Y-%m-%d}.jsonl"
                self._fh = open(path, "a", encoding="utf-8")
                self._open_date = now.date()
            record = {"ts": now.isoformat(timespec="seconds"), **entry}
            self._fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            self._fh.flush()
        except Exception as exc:  # the log never breaks a turn
            _LOGGER.warning("inbox write failed: %s", exc)

    def _close(self) -> None:
        try:
            if self._fh is not None:
                self._fh.close()
        except Exception:
            pass
        self._fh = None
