"""Kiosk status light: the core's pushed page state (2026-11-18).

Daniel's design: a small light in the bottom-left of the TV page
with a text label -- GREEN idle, AMBER thinking (soft pulse),
BLUE speaking, VIOLET dim for Sleep, RED (slow pulse) for a
service fault he needs to investigate, the label naming the fault
("Model not loaded", "Speech service down", ...). Error outranks
everything including Sleep; then Sleep, speaking, thinking,
listening, idle.

Listening (PINK, added later the same evening at Daniel's
order): the gate is open and she is actively taking in his
speech -- WakeGate's post-wake capture window, or an RTC/call
session mid-utterance. The ambient mic is only wake-watching,
which is not listening in any sense worth a light; gate-open
capture is. It doubles as the silent wake-ack: he can see she
triggered instead of talking to air. In RTC it never overrides
speaking -- an onset during her playback (a barge candidate,
her own echo included) leaves the light blue until playback
has actually stopped.

State delivery is PUSHED end to end (Daniel's state-delivery
rule: state-driven/pushed, never polled). This hub computes the
core's slice of the page state; on every change the publisher
writes web/status_state.json (the web state-file pattern, atomic)
and POSTs the snapshot to lyra-web, which fans it out to the
pages over SSE. A slow heartbeat repost (15 s) lets lyra-web tell
a live core from a dead one. The page itself only ever receives
pushes; if its socket drops, the page shows red "Disconnected"
(page-side inference, not a state this module emits).

Inputs, all wired in production_main:
- activity: turn counter (WakeGate session processing, RTC/call
  turn tasks, dashboard-call turns) = thinking; a speech clock fed
  by every TV-mirrored synthesis (the WAV's own duration is the
  playback duration the page's job player runs) plus RTC sessions'
  real playback state = speaking; keyed capture flags (the
  WakeGate session's capturing state, RTC/call sessions'
  utterance activity) = listening.
- mode: the mode manager's applied mode (sleep shows violet).
- health: the dependency monitor below (lyra-llm with the model
  loaded, Kokoro, Parakeet TDT). Checking
  dependencies is service monitoring inside the core; the UI only
  ever receives the pushed result.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from pathlib import Path

_LOGGER = logging.getLogger("lyra_core.status")

WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"
STATUS_STATE_PATH = WEB_DIR / "status_state.json"
WEB_PUSH_URL = "http://127.0.0.1:8095/statuspush"

# Fault display order: the first failing dependency in this list
# is the one the label names.
FAULTS = (
    ("llm", "Model not loaded"),
    ("kokoro", "Speech service down"),
    ("parakeet", "STT service down"),
)

LABELS = {
    "idle": "Idle",
    "listening": "Listening\u2026",
    "thinking": "Thinking\u2026",
    "speaking": "Speaking",
    "sleep": "Sleeping",
}


class StatusHub:
    """Loop-side aggregator. All methods are called from the
    core's event loop (turn tasks, the speech funnel, RTC frame
    callbacks, the health monitor)."""

    def __init__(self) -> None:
        self._turns = 0
        self._speech_until = 0.0
        self._rtc_playing: set = set()
        self._listening: set = set()
        self._mode = "wakegate"
        self._health: dict[str, bool] = {}
        self._subs: list = []
        self._shown: tuple | None = None
        self._expiry: asyncio.TimerHandle | None = None

    # -- wiring ---------------------------------------------------

    def subscribe(self, fn) -> None:
        self._subs.append(fn)

    # -- inputs ---------------------------------------------------

    def turn_begin(self) -> None:
        self._turns += 1
        self._recompute()

    def turn_end(self) -> None:
        self._turns = max(0, self._turns - 1)
        self._recompute()

    def note_speech(self, seconds: float) -> None:
        """A TV-mirrored line of `seconds` starts playing now (or
        queues behind the one playing): extend the speech clock.
        Speaking ends when the clock runs out -- the expiry timer
        recomputes so the light falls back without waiting for
        another event."""
        if seconds <= 0:
            return
        now = time.monotonic()
        self._speech_until = max(now, self._speech_until) + seconds
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            if self._expiry is not None:
                self._expiry.cancel()
            delay = self._speech_until - now + 0.05
            self._expiry = loop.call_later(delay, self._recompute)
        self._recompute()

    def rtc_playing(self, key, playing: bool) -> None:
        if playing:
            self._rtc_playing.add(key)
        else:
            self._rtc_playing.discard(key)
        self._recompute()

    def set_listening(self, key, active: bool) -> None:
        """Gate-open capture, keyed like rtc_playing so concurrent
        sources (the WakeGate session, RTC/call sessions) compose:
        she is taking in the user's speech from `key` until the
        source clears the flag at dispatch or discard."""
        if active:
            self._listening.add(key)
        else:
            self._listening.discard(key)
        self._recompute()

    def set_mode(self, mode: str) -> None:
        if mode != self._mode:
            self._mode = mode
            self._recompute()

    def set_health(self, results: dict[str, bool]) -> None:
        if results != self._health:
            self._health = dict(results)
            self._recompute()

    # -- output ---------------------------------------------------

    def _fault(self) -> str | None:
        for name, label in FAULTS:
            if self._health.get(name) is False:
                return label
        return None

    def _activity(self) -> str:
        if self._rtc_playing or time.monotonic() < self._speech_until:
            return "speaking"
        if self._turns > 0:
            return "thinking"
        if self._listening:
            return "listening"
        return "idle"

    def snapshot(self) -> dict:
        fault = self._fault()
        activity = self._activity()
        if fault is not None:
            state, label = "error", fault
        elif self._mode == "sleep":
            state, label = "sleep", LABELS["sleep"]
        else:
            state = activity
            label = LABELS[activity]
        return {
            "state": state,
            "label": label,
            "fault": fault,
            "mode": self._mode,
            "activity": activity,
            "health": dict(self._health),
            "ts": time.time(),
        }

    def _recompute(self) -> None:
        snap = self.snapshot()
        shown = (snap["state"], snap["label"], snap["mode"],
                 snap["fault"])
        if shown == self._shown:
            return
        self._shown = shown
        _LOGGER.info("status: %s (%s)", snap["state"], snap["label"])
        self._notify(snap)

    def republish(self) -> None:
        """Heartbeat: push the current snapshot even when nothing
        changed, so lyra-web can tell a live core from a dead one."""
        self._notify(self.snapshot())

    def _notify(self, snap: dict) -> None:
        for fn in list(self._subs):
            try:
                fn(snap)
            except Exception:
                _LOGGER.exception("status subscriber failed")


class StatusPublisher:
    """The hub's subscriber: file + POST push to lyra-web.

    The file write follows the web state-file pattern (atomic tmp
    + rename) so lyra-web can seed from it at startup; the POST is
    the live push, fire-and-forget like the TV mirror -- a failed
    push must never disturb the core, and the heartbeat repost
    heals any miss."""

    def __init__(self, path: Path = STATUS_STATE_PATH,
                 url: str = WEB_PUSH_URL) -> None:
        self._path = path
        self._url = url

    def __call__(self, snap: dict) -> None:
        try:
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(snap))
            os.replace(tmp, self._path)
        except Exception:
            _LOGGER.exception("status file write failed")
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._post(snap))

    async def _post(self, snap: dict) -> None:
        def _send() -> None:
            req = urllib.request.Request(
                self._url, data=json.dumps(snap).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=2).read()
        try:
            await asyncio.to_thread(_send)
        except Exception:
            pass  # lyra-web may be restarting; the heartbeat heals it


# ---------------------------------------------------------------------
# Dependency health (service monitoring, core-side).
# ---------------------------------------------------------------------

LLM_MODELS_URL = "http://127.0.0.1:8094/v1/models"
KOKORO_HEALTH_URL = "http://127.0.0.1:8091/health"
PARAKEET_HEALTH_URL = "http://127.0.0.1:8096/health"
HEALTH_PERIOD_S = 5.0


def _get_json(url: str, timeout: float = 3.0) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            obj = json.loads(resp.read())
            return obj if isinstance(obj, dict) else None
    except Exception:
        return None


async def check_dependencies(llm_url: str = LLM_MODELS_URL,
                             kokoro_url: str = KOKORO_HEALTH_URL,
                             parakeet_url: str = PARAKEET_HEALTH_URL) -> dict[str, bool]:
    """One pass over the voice stack's dependencies. The LLM
    check requires the model list to be non-empty -- a llama-server
    that answers with no model loaded is exactly Daniel's example
    fault. URLs are parameters so tests can point the checker at
    dead endpoints without touching production services."""
    llm_obj, kokoro_obj, parakeet_obj = await asyncio.gather(
        asyncio.to_thread(_get_json, llm_url),
        asyncio.to_thread(_get_json, kokoro_url),
        asyncio.to_thread(_get_json, parakeet_url),
    )
    return {
        "llm": bool(llm_obj and llm_obj.get("data")),
        "kokoro": bool(kokoro_obj and kokoro_obj.get("ok") is True),
        "parakeet": bool(
            parakeet_obj and parakeet_obj.get("status") == "ok"),
    }


async def health_monitor(hub: StatusHub, **check_kwargs) -> None:
    """Check on a slow cadence; the hub pushes only on change, so
    a steady fault is one red state, not a stream of events."""
    while True:
        try:
            hub.set_health(await check_dependencies(**check_kwargs))
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("health check failed")
        await asyncio.sleep(HEALTH_PERIOD_S)
