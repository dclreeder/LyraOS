#!/usr/bin/env python3
"""Lyra home dashboard - a small LAN page served from the Fedora.

Recreates Daniel's old Home Assistant 'Home' dashboard with live state
and working controls. Everything runs through lyra-core's own /tool API
(:8099, token read server-side from the core's secrets file - the
browser never sees it) plus Jellyfin's sessions API (key read
server-side from APIKeys/.env) and pactl for the PC mic mute.

Bind: 0.0.0.0:8098 (home LAN + tailnet only by network design).
"""
from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
CORE = "http://127.0.0.1:8099"
JELLYFIN = "http://127.0.0.1:8096"
PAGE = Path(__file__).with_name("dashboard.html")


def _call_url():
    try:
        return Path(__file__).with_name("call_url.txt").read_text().strip()
    except Exception:
        return "#"



def _kill_token() -> str:
    data = json.loads((HOME / "Documents/LyraOS/core/alarm_secrets.json").read_text())
    return data["kill_token"]


def _jellyfin_key() -> str:
    for line in (HOME / "Documents/APIKeys/.env").read_text().splitlines():
        if line.startswith("JELLYFIN_API_KEY="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise RuntimeError("JELLYFIN_API_KEY not found")


TOKEN = _kill_token()
JF_KEY = _jellyfin_key()


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


def jellyfin_now_playing() -> list[dict]:
    _, _, raw = _req(f"{JELLYFIN}/Sessions?api_key={JF_KEY}", timeout=5.0)
    out = []
    for s in json.loads(raw):
        item = s.get("NowPlayingItem")
        if not item:
            continue
        ps = s.get("PlayState") or {}
        runtime = item.get("RunTimeTicks") or 0
        pos = ps.get("PositionTicks") or 0
        title = item.get("Name", "")
        if item.get("SeriesName"):
            ep = ""
            if item.get("ParentIndexNumber") is not None and item.get("IndexNumber") is not None:
                ep = f"S{item['ParentIndexNumber']} E{item['IndexNumber']} - "
            title = f"{item['SeriesName']} - {ep}{title}"
        out.append({
            "title": title,
            "type": item.get("Type", ""),
            "user": s.get("UserName", ""),
            "client": s.get("Client", ""),
            "device": s.get("DeviceName", ""),
            "paused": bool(ps.get("IsPaused")),
            "pct": round(100 * pos / runtime) if runtime else 0,
            "image_id": item.get("SeriesId") or item.get("Id"),
        })
    return out


def mic_state() -> dict:
    try:
        out = subprocess.run(["pactl", "get-source-mute", "@DEFAULT_SOURCE@"],
                             capture_output=True, text=True, timeout=4).stdout
        return {"muted": "yes" in out.lower(), "ok": True}
    except Exception as exc:
        return {"muted": None, "ok": False, "error": str(exc)}


def mic_set(muted: bool) -> dict:
    subprocess.run(["pactl", "set-source-mute", "@DEFAULT_SOURCE@",
                    "1" if muted else "0"], capture_output=True, timeout=4)
    return mic_state()


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


def full_state() -> dict:
    state: dict = {"core_online": core_health()}
    for key, tool, action in (
        ("alarm", "alarm", "summary"),
        ("light", "light", "state"),
        ("monitor", "monitor", "state"),
        ("volume", "media_volume", "get"),
        ("tv", "tv", "get"),
        ("tv_volume", "tv_volume", "get"),
    ):
        try:
            state[key] = core_tool(tool, action, {})
        except Exception as exc:
            state[key] = {"ok": False, "error": str(exc)}
    try:
        state["jellyfin"] = {"ok": True, "now_playing": jellyfin_now_playing()}
    except Exception as exc:
        state["jellyfin"] = {"ok": False, "error": str(exc), "now_playing": []}
    state["mic"] = mic_state()
    state["display"] = display_state()
    return state


ALLOWED = {
    "light": {"state", "on", "off", "set_brightness"},
    "monitor": {"state", "on", "off"},
    "alarm": {"summary", "set", "kill", "snooze", "enable", "disable",
              "ring_volume_get", "ring_volume_set", "note_get"},
    "media_volume": {"get", "set"},
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
        m = re.fullmatch(r"/api/poster/([A-Za-z0-9]+)", self.path)
        if m:
            try:
                _, ctype, raw = _req(
                    f"{JELLYFIN}/Items/{m.group(1)}/Images/Primary?api_key={JF_KEY}",
                    timeout=6.0)
                self._send(200, ctype, raw)
            except Exception:
                self._json(404, {"ok": False})
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
        if self.path == "/api/mic":
            try:
                self._json(200, {"ok": True, "mic": mic_set(bool(payload.get("muted")))})
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/display":
            try:
                self._json(200, {"ok": True, "display": display_set(bool(payload.get("on")))})
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8098), Handler).serve_forever()
