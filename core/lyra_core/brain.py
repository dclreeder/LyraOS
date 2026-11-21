"""Lyra's brain switch: which mind answers her turns.

Daniel's addition (2026-11-21): a dashboard switch between the
local model (llama-server :8094, the core's tool-calling router
exactly as it has always run) and Muse, reached over the Muse
Gadget SDK (musegadget send-user-msg) into the dedicated
"Lyra - Muse brain" side chat. The switch changes ONLY the
brain: the wake gate, pipeline, modes, alarm, face and lip sync
are the core's and never notice.

The state lives in core/brain_state.json, owned by the core --
the same pattern as mode_state.json: the dashboard asks the core
to change it over the core API (POST /brain), the core persists
the choice here (the file's only writer), everyone else consumes
it from the status hub's pushed snapshot, and the router reads
the file at the start of every turn (SOUL.md is re-read per turn
the same way), so a flip takes effect on the very next sentence.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
BRAIN_STATE_PATH = CORE_DIR / "brain_state.json"

LOCAL = "local"
MUSE = "muse"
BRAINS = (LOCAL, MUSE)
DEFAULT_BRAIN = LOCAL


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


def read_brain() -> str:
    """The current brain; a missing or invalid file reads as the
    local model, her resting brain."""
    brain = _read_json(BRAIN_STATE_PATH).get("brain")
    return brain if brain in BRAINS else DEFAULT_BRAIN


def write_brain(brain: str) -> str:
    if brain not in BRAINS:
        raise ValueError(f"unknown brain {brain!r}")
    _write_json(BRAIN_STATE_PATH, {"brain": brain, "updated": time.time()})
    return brain
