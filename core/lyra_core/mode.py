"""Lyra's mode system: one source of truth for how she listens.

Daniel's model (2026-11-16): three exclusive modes, one active at
a time, WakeGate the resting mode.

- wakegate: the room path as it has been -- the localmic transport
  captures Fedora's default source and the wake word gates turns.
- rtc: a full-duplex WebRTC session is her live channel. The peer
  is the TV page in the kiosk browser (the only browser the Fedora
  ever has); inside the session there is no wake word, and the
  room capture does not drive turns. RTC changes the TRANSPORT,
  never the routing: the page captures the system default mic and
  plays back through the browser's normal output, which PipeWire
  routes to Fedora's default sink -- no device is selected or
  pinned anywhere in the RTC path.
- sleep: no inputs at all -- capture suspended, she is in bed. The
  TV page shows the sleep plate off the same state.

The state lives in core/mode_state.json, owned by the core
(2026-11-19): the dashboard asks the core to change mode over the
core API (POST /mode), the core persists the choice here -- the
file's only writer -- and applies it, and everyone else (lyra-web,
the pages, the dashboard's read-back) consumes the mode from the
status hub's pushed snapshot. Writes are atomic (tmp + rename).
Before this, the file lived in web/ in the web state-file pattern:
the dashboard wrote it, the core polled it every 0.5 s, lyra-web
read it -- three processes around one file, replaced by one owner.
The old sleep_state.json is retired by this file: sleep is a mode
now, and this is the only switch.

The RTC peer fact ({"connected": bool}) rides the status hub's
pushed snapshot instead of a file of its own: rtc.py reports
transitions to the hub, lyra-web merges the fact into its
in-memory page state, and the dashboard reads it from there.
(web/rtc_state.json carried it until the 2026-11-19 web merge
deleted the file.) Mode is intent (dashboard -> core); the
snapshot is fact (core -> everyone).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
MODE_STATE_PATH = CORE_DIR / "mode_state.json"

WAKEGATE = "wakegate"
RTC = "rtc"
SLEEP = "sleep"
MODES = (WAKEGATE, RTC, SLEEP)
DEFAULT_MODE = WAKEGATE


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    os.replace(tmp, path)


def read_mode() -> str:
    """The current mode; a missing or invalid file reads as the
    resting mode, wakegate."""
    mode = _read_json(MODE_STATE_PATH).get("mode")
    return mode if mode in MODES else DEFAULT_MODE


def write_mode(mode: str) -> str:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    _write_json(MODE_STATE_PATH, {"mode": mode, "updated": time.time()})
    return mode

