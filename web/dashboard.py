#!/usr/bin/env python3
"""Lyra home dashboard -- a module of lyra-web since the
2026-11-19 web merge: lyra_web.py starts this module's server
(0.0.0.0:8098) inside its own process; there is no separate
dashboard service anymore. Same page, same /api/* routes as
the standalone server had.

Recreates Daniel's old Home Assistant 'Home' dashboard with live state
and working controls. Everything runs through lyra-core's own /tool API
(:8099, token read server-side from the core's secrets file - the
browser never sees it) plus pactl for Fedora's audio defaults
and the default-source mic mute. Mode and the RTC peer fact are
read from lyra-web's in-memory pushed state (injected by
lyra_web via set_state_provider below); mode writes still go
to the core API, which owns mode.

Bind: 0.0.0.0:8098 (home LAN + tailnet only by network design).
The page file stays at dashboard/dashboard.html in the tree.
"""
from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
CORE = "http://127.0.0.1:8099"
PAGE = (Path(__file__).resolve().parent.parent
        / "dashboard" / "dashboard.html")



def _kill_token() -> str:
    data = json.loads((HOME / "Documents/LyraOS/core/alarm_secrets.json").read_text())
    return data["kill_token"]


TOKEN = _kill_token()


def _req(url: str, data: bytes | None = None, timeout: float = 10.0):
    r = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return resp.status, resp.headers.get_content_type(), resp.read()


def core_tool(tool: str, action: str | None, args: dict) -> dict:
    body = json.dumps({"tool": tool, "action": action, "args": args}).encode()
    _, _, raw = _req(f"{CORE}/tool?t={TOKEN}", data=body)
    return json.loads(raw)


def core_health() -> bool:
    try:
        _, _, raw = _req(f"{CORE}/healthz", timeout=3.0)
        return raw.decode().strip() == "ok"
    except Exception:
        return False


def _source_muted() -> bool | None:
    try:
        out = subprocess.run(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"],
                             capture_output=True, text=True, timeout=4).stdout
        return "yes" in out.lower()
    except Exception:
        return None


# The raw ReSpeaker source node: WakeGate's pinned capture since
# the 2026-11-19 feed split (see lyra_core/localmic.py). The mute
# toggle below sets BOTH this node and the default source --
# muting the physical mic silences WakeGate's ears and the
# echo-cancelled feed (which captures from this same device).
RAW_SOURCE = ("alsa_input.usb-Seeed_Studio_reSpeaker_XVF3800_"
              "4-Mic_Array_100070894262500433-00.analog-stereo")


def audio_mute_set(muted: bool) -> dict:
    # The mic mute rides Fedora's DEFAULT source AND the raw
    # ReSpeaker node: since the 2026-11-19 feed split, WakeGate
    # captures the raw node rather than the default source, so
    # muting only the default would leave her room ears open.
    # Muting the physical mic silences both feeds, which is what
    # a mic mute means. Same pactl mechanism the old standalone
    # Mic Mute toggle used; the control now lives on the Audio
    # card.
    flag = "1" if muted else "0"
    subprocess.run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@", flag],
                   capture_output=True, timeout=4)
    subprocess.run(["pactl", "set-source-mute", RAW_SOURCE, flag],
                   capture_output=True, timeout=4)
    return audio_devices()


def audio_devices() -> dict:
    # Fedora audio devices via PipeWire (2026-11-16, Daniel's ask).
    # The dashboard only flips the OS defaults: Lyra's voice follows
    # the default sink, and the default source steers the
    # echo-cancelled feed and every default-following consumer.
    # WakeGate is the one exception: since the 2026-11-19 feed
    # split its capture is pinned to the raw ReSpeaker node, so
    # the source dropdown does not move her room ears. Node names
    # are the stable ids (wpctl numbers change on replug).
    # *.monitor sources are output loopbacks, not microphones.
    sinks: list[dict] = []
    sources: list[dict] = []
    try:
        out = subprocess.run(["pw-dump"], capture_output=True, text=True,
                             timeout=6).stdout
        nodes = json.loads(out)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "sinks": [], "sources": [],
                "default_sink": "", "default_source": "", "source_muted": None}
    for obj in nodes:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        props = (obj.get("info") or {}).get("props") or {}
        name = props.get("node.name")
        cls = props.get("media.class")
        if not name:
            continue
        entry = {"name": name,
                 "label": props.get("node.description")
                 or props.get("node.nick") or name}
        if cls == "Audio/Sink":
            sinks.append(entry)
        elif cls == "Audio/Source" and not name.endswith(".monitor"):
            sources.append(entry)

    def _default(cmd: str) -> str:
        try:
            return subprocess.run(["pactl", cmd], capture_output=True,
                                  text=True, timeout=4).stdout.strip()
        except Exception:
            return ""

    return {"ok": True, "sinks": sinks, "sources": sources,
            "default_sink": _default("get-default-sink"),
            "default_source": _default("get-default-source"),
            "source_muted": _source_muted()}


def audio_set(kind: str, device: str) -> dict:
    state = audio_devices()
    pool = state["sinks"] if kind == "sink" else state["sources"]
    if device not in {d["name"] for d in pool}:
        raise ValueError("unknown audio device")
    cmd = "set-default-sink" if kind == "sink" else "set-default-source"
    subprocess.run(["pactl", cmd, device], capture_output=True, timeout=4)
    return audio_devices()


DISPLAY_STATE_FILE = HOME / "Documents/LyraOS/web/display_state.json"


def display_state() -> dict:
    try:
        data = json.loads(DISPLAY_STATE_FILE.read_text())
        return {"active": bool(data.get("display_on", True)), "ok": True}
    except Exception as exc:
        return {"active": None, "ok": False, "error": str(exc)}


def display_set(on: bool) -> dict:
    # The state file is the source of truth; lyra-route-watch.path sees
    # the write and lyra-route-apply.service enforces it (kiosk + route).
    DISPLAY_STATE_FILE.write_text(
        json.dumps({"display_on": bool(on), "updated": time.time()}))
    return display_state()


MODES = ("wakegate", "rtc", "sleep")

# Mode / RTC reads (2026-11-19 web merge): lyra_web injects a
# provider over its in-memory merged state -- the core's pushed
# status slice carries both the applied mode and the RTC peer
# fact, so the dashboard reads them from there instead of the
# core API and the deleted rtc_state.json. Mode WRITES still go
# to the core API (POST /mode in mode_set): the core owns mode.
_state_provider = None


def set_state_provider(fn) -> None:
    global _state_provider
    _state_provider = fn


def mode_state() -> dict:
    mode, connected, ok = "wakegate", False, False
    if _state_provider is not None:
        try:
            snap = _state_provider()
            mode = snap.get("mode") or "wakegate"
            connected = bool(snap.get("rtc_connected"))
            ok = bool(snap.get("core_seen"))
        except Exception:
            mode, connected, ok = "wakegate", False, False
    if mode not in MODES:
        mode = "wakegate"
    return {"mode": mode, "rtc_connected": connected, "ok": ok}


def mode_set(mode: str) -> dict:
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    body = json.dumps({"mode": mode}).encode()
    _req(f"{CORE}/mode?t={TOKEN}", data=body, timeout=15.0)
    # The read-back comes from the pushed in-memory state; give
    # the core's push a moment to land so the card shows the
    # mode it just set.
    end = time.time() + 3.0
    while time.time() < end:
        if mode_state()["mode"] == mode:
            break
        time.sleep(0.1)
    return mode_state()


def sleep_state() -> dict:
    # Sleep is a MODE now (2026-11-16): the TV card's sleep switch
    # reads the mode like everything else -- there is no
    # separate sleep state anymore (the old sleep_state.json is
    # retired; the Phase 2 sleep cycle will select the mode too).
    return {"active": mode_state()["mode"] == "sleep", "ok": True}


def sleep_set(on: bool) -> dict:
    # Toggling sleep on selects the sleep mode; toggling it off
    # returns her to the resting WakeGate mode.
    if on:
        mode_set("sleep")
    elif mode_state()["mode"] == "sleep":
        mode_set("wakegate")
    return sleep_state()


def full_state() -> dict:
    state: dict = {"core_online": core_health()}
    for key, tool, action in (
        ("alarm", "alarm", "summary"),
        ("light", "light", "state"),
        ("tv", "tv", "get"),
        ("tv_volume", "tv_volume", "get"),
    ):
        try:
            state[key] = core_tool(tool, action, {})
        except Exception as exc:
            state[key] = {"ok": False, "error": str(exc)}
    state["audio"] = audio_devices()
    state["display"] = display_state()
    state["sleep"] = sleep_state()
    state["mode"] = mode_state()
    return state


ALLOWED = {
    "light": {"state", "on", "off", "set_brightness"},
    "alarm": {"summary", "set", "kill", "snooze", "enable", "disable",
              "ring_volume_get", "ring_volume_set", "note_get"},
    "tv": {"get", "set", "adjust"},
    "tv_volume": {"get", "set", "adjust"},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj):
        self._send(code, "application/json", json.dumps(obj).encode())

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, "text/html; charset=utf-8", PAGE.read_text().encode())
            return
        if self.path == "/api/state":
            self._json(200, full_state())
            return
        self._json(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            self._json(400, {"ok": False, "error": "bad json"})
            return
        if self.path == "/api/action":
            tool = str(payload.get("tool", "")).lower()
            action = payload.get("action")
            action = str(action).lower() if action else None
            args = payload.get("args") or {}
            if tool not in ALLOWED or action not in ALLOWED[tool]:
                self._json(400, {"ok": False, "error": "action not allowed"})
                return
            try:
                self._json(200, core_tool(tool, action, args))
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/display":
            try:
                self._json(200, {"ok": True, "display": display_set(bool(payload.get("on")))})
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/mode":
            try:
                self._json(200, {"ok": True,
                                 "mode": mode_set(str(payload.get("mode", "")))})
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/sleep":
            try:
                self._json(200, {"ok": True, "sleep": sleep_set(bool(payload.get("on")))})
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/audio":
            try:
                if "muted" in payload:
                    self._json(200, {"ok": True,
                                     "audio": audio_mute_set(bool(payload["muted"]))})
                    return
                kind = str(payload.get("kind", ""))
                if kind not in ("sink", "source"):
                    raise ValueError("kind must be sink or source")
                self._json(200, {"ok": True,
                                 "audio": audio_set(kind, str(payload.get("device", "")))})
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})


def make_server(port: int = 8098) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


if __name__ == "__main__":
    make_server().serve_forever()
