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

The state lives in web/mode_state.json -- the web state-file
pattern (display_state.json, and sleep_state.json before it) --
because three processes read it: the dashboard writes it (the mode
selector), the core applies it (capture / RTC acceptance), and
lyra-web reports it in /roomstate for the TV page. Writes are
atomic (tmp + rename) so no reader ever sees a torn file. The old
sleep_state.json is retired by this file: sleep is a mode now, and
this is the only switch.

The core also publishes web/rtc_state.json ({"connected": bool}):
core-owned status, read by the dashboard and lyra-web. Mode is
intent (dashboard -> core); rtc_state is fact (core -> everyone).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
MODE_STATE_PATH = WEB_DIR / "mode_state.json"
RTC_STATE_PATH = WEB_DIR / "rtc_state.json"

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


def read_rtc_state() -> dict:
    data = _read_json(RTC_STATE_PATH)
    return {"connected": bool(data.get("connected", False)),
            "updated": data.get("updated")}


def write_rtc_state(connected: bool) -> None:
    _write_json(RTC_STATE_PATH,
                {"connected": bool(connected), "updated": time.time()})
