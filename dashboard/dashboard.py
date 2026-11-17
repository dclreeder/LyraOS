#!/usr/bin/env python3
"""Lyra home dashboard - a small LAN page served from the Fedora.

Recreates Daniel's old Home Assistant 'Home' dashboard with live state
and working controls. Everything runs through lyra-core's own /tool API
(:8099, token read server-side from the core's secrets file - the
browser never sees it) plus pactl for Fedora's audio defaults
and the default-source mic mute.

Bind: 0.0.0.0:8098 (home LAN + tailnet only by network design).
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
PAGE = Path(__file__).with_name("dashboard.html")


def _call_url():
    try:
        return Path(__file__).with_name("call_url.txt").read_text().strip()
    except Exception:
        return "#"



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


def audio_mute_set(muted: bool) -> dict:
    # The mic mute rides Fedora's DEFAULT source (the localmic
    # feeder captures it): muting the default source means Lyra
    # hears nothing. Same pactl mechanism the old standalone Mic
    # Mute toggle used; the control now lives on the Audio card.
    subprocess.run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@",
                    "1" if muted else "0"], capture_output=True, timeout=4)
    return audio_devices()


def audio_devices() -> dict:
    # Fedora audio devices via PipeWire (2026-11-16, Daniel's ask).
    # The dashboard only flips the OS defaults: Lyra's voice follows
    # the default sink and the localmic feeder captures the default
    # source, so these defaults ARE Lyra's speaker/mic choice. Node
    # names are the stable ids (wpctl numbers change on replug).
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


SLEEP_STATE_FILE = HOME / "Documents/LyraOS/web/sleep_state.json"


def sleep_state() -> dict:
    try:
        data = json.loads(SLEEP_STATE_FILE.read_text())
        return {"active": bool(data.get("sleep", False)), "ok": True}
    except Exception:
        return {"active": False, "ok": True}


def sleep_set(on: bool) -> dict:
    # Sleep mode (2026-11-15, Daniel's ask): her sleep state -- the TV
    # page shows the sleep plate and fades her model out while it is
    # on. The Phase 2 sleep cycle will drive this same state later.
    SLEEP_STATE_FILE.write_text(
        json.dumps({"sleep": bool(on), "updated": time.time()}))
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
            self._send(200, "text/html; charset=utf-8", PAGE.read_text().replace("{{CALL_URL}}", _call_url()).encode())
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


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8098), Handler).serve_forever()
