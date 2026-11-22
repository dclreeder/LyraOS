#!/usr/bin/env python3
"""Lyra home dashboard -- a module of lyra-web since the
2026-11-19 web merge: lyra_web.py starts this module's server
(192.168.12.144:8098, LAN only) inside its own process; there is no separate
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

Bind: 192.168.12.144:8098 (home LAN only; tailnet reachability
removed 2026-11-20 at Daniel's order).
The page file stays at dashboard/dashboard.html in the tree.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
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


def _sink_volume() -> int | None:
    # Fedora's default-sink volume as a percent, read live via pactl
    # (the first channel's figure; channels are balanced in
    # practice). None when the read fails -- the Audio card shows
    # "-" rather than a made-up number.
    try:
        out = subprocess.run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                             capture_output=True, text=True, timeout=4).stdout
        m = re.search(r"(\d+)%", out)
        return int(m.group(1)) if m else None
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
                "default_sink": "", "default_source": "", "source_muted": None,
                "sink_volume": None}
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
            "source_muted": _source_muted(),
            "sink_volume": _sink_volume()}


def audio_set(kind: str, device: str) -> dict:
    state = audio_devices()
    pool = state["sinks"] if kind == "sink" else state["sources"]
    if device not in {d["name"] for d in pool}:
        raise ValueError("unknown audio device")
    cmd = "set-default-sink" if kind == "sink" else "set-default-source"
    subprocess.run(["pactl", cmd, device], capture_output=True, timeout=4)
    return audio_devices()


def audio_volume_set(percent: int) -> dict:
    # Speaker volume for Fedora's DEFAULT sink -- whichever sink the
    # Speakers dropdown currently picks. Clamped to 0-100: the
    # dashboard never over-amplifies. Returns the re-read state, so
    # the slider shows the verified volume (the Audio card's
    # verify-by-reread pattern, same as the monitor setters).
    percent = max(0, min(100, int(percent)))
    subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@",
                    f"{percent}%"], capture_output=True, timeout=4)
    return audio_devices()


# The page veil ("dim") has exactly one home: lyra-web's dim.json, the
# file lyra_web.get_dim() reads for the pushed page state and the core's
# tv tool writes through :8095/dim -- the path the Lyra kiosk card's
# veil slider takes via /api/action. The dashboard only READS the file
# here, to render that slider truthfully; veil writes from the slider
# go through the same tv-tool path. One dim variable, one write path.
DIM_FILE = Path(__file__).resolve().parent / "dim.json"


def _veil_percent() -> int:
    # Current black-veil level as a percent: 100 - dim brightness.
    try:
        dim = float(json.loads(DIM_FILE.read_text()).get("brightness", 1.0))
    except Exception:
        dim = 1.0
    return round((1.0 - max(0.0, min(1.0, dim))) * 100)


# --- Monitor (2026-11-20): the ViewSonic VX2467 on DP-2, Lyra's primary
# display since the monitor move. Brightness here is the panel's REAL
# backlight level over DDC/CI (VCP 0x10) via ddcutil -- never an overlay.
# Contrast gets the same treatment on VCP 0x12. The card's third
# slider, the black veil, is NOT a panel control: it drives lyra-web's
# page dim (the one dim state the TV card's brightness slider and the
# core's tv tool share), for going darker than the panel's own floor.
# Every ddcutil call is pinned to the ViewSonic's I2C bus, resolved from
# `ddcutil detect` by EDID model + serial; no display number is guessed
# and the TV (which does not answer DDC/CI) can never be addressed. The
# on/off toggle enables/disables the DP-2 output itself via kscreen --
# off removes the output so her kiosk window migrates to the TV, on
# brings it back (the follow-the-output behavior proven 2026-11-20).
# It is NOT DDC power (VCP D6): the output toggle is the migration path.
# Reads on this panel are flaky, so they retry; a read that still fails
# reports brightness as None (unknown), never an invented number. All
# ddcutil use is serialized on _DDC_LOCK: the DDC channel does not
# tolerate overlapping transactions.

MONITOR_OUTPUT = "DP-2"
_MONITOR_EDID_MODEL = "VX2467"
_MONITOR_EDID_SERIAL = "XMW241842736"
_DDC_LOCK = threading.Lock()
_monitor_bus: int | None = None


def _monitor_forget_bus() -> None:
    global _monitor_bus
    _monitor_bus = None


def _monitor_resolve_bus() -> int | None:
    """I2C bus of the ViewSonic, matched on its EDID (model + serial)."""
    global _monitor_bus
    if _monitor_bus is not None:
        return _monitor_bus
    try:
        out = subprocess.run(["ddcutil", "detect"], capture_output=True,
                             text=True, timeout=25).stdout
    except Exception:
        return None
    for block in out.split("\n\n"):
        if _MONITOR_EDID_MODEL in block and _MONITOR_EDID_SERIAL in block:
            m = re.search(r"/dev/i2c-(\d+)", block)
            if m:
                _monitor_bus = int(m.group(1))
                break
    return _monitor_bus


def _monitor_read_vcp(code: str) -> int | None:
    bus = _monitor_resolve_bus()
    if bus is None:
        return None
    for _ in range(4):
        try:
            out = subprocess.run(
                ["ddcutil", "--bus", str(bus), "getvcp", code],
                capture_output=True, text=True, timeout=15).stdout
            m = re.search(r"current value\s*=\s*(\d+)", out)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        time.sleep(0.3)
    # Persistent failure: the bus may have been re-enumerated since the
    # resolve; forget it so the next call re-detects instead of
    # hammering a stale bus forever.
    _monitor_forget_bus()
    return None


def _kscreen_env() -> dict:
    # lyra-web runs as a user service and normally inherits the session
    # environment (WAYLAND_DISPLAY included) from the user manager. If
    # the display var is ever missing, discover the compositor socket
    # so kscreen-doctor can still reach KWin.
    env = dict(os.environ)
    if "WAYLAND_DISPLAY" not in env:
        runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
        for sock in sorted(Path(runtime).glob("wayland-*")):
            if sock.suffix != ".lock":
                env["WAYLAND_DISPLAY"] = sock.name
                break
    return env


def _monitor_output_state() -> dict | None:
    try:
        out = subprocess.run(["kscreen-doctor", "-j"], capture_output=True,
                             text=True, timeout=8, env=_kscreen_env()).stdout
        outputs = json.loads(out).get("outputs", [])
    except Exception:
        return None
    for output in outputs:
        if output.get("name") == MONITOR_OUTPUT:
            return output
    return None


def _monitor_state() -> dict:
    output = _monitor_output_state()
    if output is None:
        return {"ok": False, "error": "DP-2 output not found",
                "enabled": None, "brightness": None, "contrast": None,
                "veil": _veil_percent()}
    enabled = bool(output.get("enabled"))
    # A disabled output is not being driven, so there are no live panel
    # values to report; brightness and contrast are only read while it
    # is enabled. The veil is page state, independent of the panel, so
    # it is always reported.
    brightness = _monitor_read_vcp("10") if enabled else None
    contrast = _monitor_read_vcp("12") if enabled else None
    return {"ok": True, "enabled": enabled, "brightness": brightness,
            "contrast": contrast, "veil": _veil_percent()}


def monitor_state() -> dict:
    with _DDC_LOCK:
        return _monitor_state()


def _monitor_set_vcp(code: str, value: int, label: str) -> dict:
    value = max(0, min(100, int(value)))
    with _DDC_LOCK:
        ok = False
        for _ in range(2):
            bus = _monitor_resolve_bus()
            if bus is None:
                break
            try:
                res = subprocess.run(
                    ["ddcutil", "--bus", str(bus), "setvcp", code, str(value)],
                    capture_output=True, text=True, timeout=20)
                ok = res.returncode == 0
            except Exception:
                ok = False
            if ok:
                break
            _monitor_forget_bus()
        if not ok:
            raise ValueError(f"ddcutil could not set monitor {label}")
        time.sleep(0.3)
        # The returned state carries the read-back value, not the
        # requested one (the Audio card's verify-by-reread pattern).
        return _monitor_state()


def monitor_set_brightness(value: int) -> dict:
    return _monitor_set_vcp("10", value, "brightness")


def monitor_set_contrast(value: int) -> dict:
    return _monitor_set_vcp("12", value, "contrast")


def monitor_set_enabled(on: bool) -> dict:
    with _DDC_LOCK:
        verb = "enable" if on else "disable"
        try:
            subprocess.run(
                ["kscreen-doctor", f"output.{MONITOR_OUTPUT}.{verb}"],
                capture_output=True, text=True, timeout=15, env=_kscreen_env())
        except Exception as exc:
            raise ValueError(f"kscreen-doctor {verb} failed: {exc}")
        # Give KWin a moment to apply the change (and migrate her
        # kiosk window) before reading the state back.
        time.sleep(0.6)
        return _monitor_state()


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
    mode, connected, ok, locked = "wakegate", False, False, False
    if _state_provider is not None:
        try:
            snap = _state_provider()
            mode = snap.get("mode") or "wakegate"
            connected = bool(snap.get("rtc_connected"))
            ok = bool(snap.get("core_seen"))
            locked = bool(snap.get("mode_locked"))
        except Exception:
            mode, connected, ok, locked = "wakegate", False, False, False
    if mode not in MODES:
        mode = "wakegate"
    return {"mode": mode, "rtc_connected": connected, "ok": ok,
            "locked": locked}


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
    # Sleep is entered only by the nightly sleep cycle (memory
    # build Phase 2), never by hand: this switch is an indicator
    # now. The core refuses a manual sleep regardless, the page
    # disables the toggle, and a stale client just gets the state.
    return sleep_state()


def memory_reports() -> list:
    """The curation report view (memory build Phase 2): one
    dated file per curated day under memory/reports/, read-only
    here, latest first. The dashboard never writes memory."""
    out = []
    try:
        reports = Path("~/Documents/LyraOS/memory/reports").expanduser()
        for path in sorted(reports.glob("*.md"), reverse=True)[:10]:
            out.append({"day": path.stem,
                        "text": path.read_text(encoding="utf-8")[:4000]})
    except Exception:
        pass
    return out


def memory_sources() -> list:
    """Uploaded sources with mining status (Phase 4), folded
    from the sources manifest, latest first. Read-only here."""
    folded: dict = {}
    try:
        manifest = Path("~/Documents/LyraOS/memory/sources/manifest.jsonl").expanduser()
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("id"):
                folded.setdefault(entry["id"], {}).update(entry)
    except Exception:
        return []
    rows = [{"id": e.get("id"), "name": e.get("name"),
             "uploaded": str(e.get("uploaded") or e.get("ts") or "")[:10],
             "size": e.get("size"), "status": e.get("status"),
             "chunks_done": e.get("chunks_done") or 0,
             "chunks_total": e.get("chunks_total")}
            for e in folded.values()]
    return list(reversed(rows))


def memory_upload(filename: str, data: bytes) -> dict:
    """The mailbox (memory Phase 4): an upload lands in
    memory/sources/ as an immutable original plus a manifest
    line, and chunking/embedding is kicked off detached. This
    is the only write the dashboard makes into memory, and it
    writes raw material -- never curated memory."""
    import hashlib
    from datetime import datetime, timezone
    name = Path(filename).name
    ext = Path(name).suffix.lower()
    if ext not in (".txt", ".md", ".log", ".json", ".csv", ".pdf"):
        return {"ok": False, "error": "text files and PDFs only"}
    if not data:
        return {"ok": False, "error": "empty file"}
    if len(data) > 50 * 1024 * 1024:
        return {"ok": False, "error": "over the 50 MB limit"}
    if ext == ".pdf" and not data.startswith(b"%PDF"):
        return {"ok": False, "error": "that file is not a PDF"}
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name) or "upload.txt"
    digest = hashlib.sha256(data).hexdigest()
    sid = (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
           + "-" + digest[:6])
    dest_dir = Path("~/Documents/LyraOS/memory/sources").expanduser()
    dest_dir.mkdir(parents=True, exist_ok=True)
    stored = f"{sid}__{safe}"
    (dest_dir / stored).write_bytes(data)
    entry = {"id": sid, "name": name, "file": stored,
             "uploaded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "size": len(data), "sha256": digest,
             "status": "pending", "chunks_done": 0}
    with open(dest_dir / "manifest.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")
    try:
        env = dict(os.environ,
                   XDG_RUNTIME_DIR="/run/user/1000",
                   DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus",
                   PYTHONPATH="/home/dclreeder/Documents/LyraOS/core")
        subprocess.Popen(
            ["systemd-run", "--user", "--collect",
             "--setenv", "PYTHONPATH=/home/dclreeder/Documents/LyraOS/core",
             "--unit", f"lyra-source-ingest-{sid}",
             "/home/dclreeder/Documents/esphome-probe/venv/bin/python",
             "-m", "lyra_core.sources", "ingest", sid],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass  # the curator pass ingests on demand if the spawn failed
    return {"ok": True, "id": sid, "name": name, "size": len(data)}


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
    state["monitor"] = monitor_state()
    state["sleep"] = sleep_state()
    state["mode"] = mode_state()
    state["memory_reports"] = memory_reports()
    state["memory_sources"] = memory_sources()
    return state


ALLOWED = {
    "light": {"state", "on", "off", "set_brightness"},
    "alarm": {"summary", "set", "kill", "snooze", "enable", "disable",
              "note_get"},
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
        if self.path == "/api/memory-upload":
            try:
                data = self.rfile.read(length) if length else b""
                self._json(200, memory_upload(
                    self.headers.get("X-Filename") or "upload.txt", data))
            except Exception as exc:
                self._json(400, {"ok": False, "error": str(exc)})
            return
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
                if "volume" in payload:
                    self._json(200, {"ok": True,
                                     "audio": audio_volume_set(int(payload["volume"]))})
                    return
                kind = str(payload.get("kind", ""))
                if kind not in ("sink", "source"):
                    raise ValueError("kind must be sink or source")
                self._json(200, {"ok": True,
                                 "audio": audio_set(kind, str(payload.get("device", "")))})
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            return
        if self.path == "/api/monitor":
            try:
                if "brightness" in payload:
                    self._json(200, {"ok": True, "monitor":
                                     monitor_set_brightness(int(payload["brightness"]))})
                elif "contrast" in payload:
                    self._json(200, {"ok": True, "monitor":
                                     monitor_set_contrast(int(payload["contrast"]))})
                elif "on" in payload:
                    self._json(200, {"ok": True, "monitor":
                                     monitor_set_enabled(bool(payload["on"]))})
                else:
                    self._json(400, {"ok": False,
                                     "error": "brightness, contrast or on required"})
            except Exception as exc:
                self._json(502, {"ok": False, "error": str(exc)})
            return
        self._json(404, {"ok": False, "error": "not found"})


def make_server(port: int = 8098, host: str = "192.168.12.144") -> ThreadingHTTPServer:
    # LAN-only bind (2026-11-20): no dashboard on the tailnet. The
    # TV viewer keeps its own :8095 bind and Serve :443 mapping.
    return ThreadingHTTPServer((host, port), Handler)


if __name__ == "__main__":
    make_server().serve_forever()
