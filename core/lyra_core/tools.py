"""Tools: the fixed set the router (LLM-classified, slot-validated) can call.

Each tool executes ONE action and verifies it by readback, then
reports a ToolResult whose ``confirmation`` is the short spoken line
(Decision D2) built from the ACTUAL outcome -- a readback that
contradicts the command produces a sentence saying what really
happened, never a success claim. The set is closed: light, tv brightness,
tv volume, alarm, clock, weather, jellyfin. Nothing here can call anything outside it
(the Companion lesson: a deterministic router cannot bluff, and
neither can its tools).

Hardware access mirrors the proven paths:
- Light: the alarm module's ShellyLight RPC client (reused, not
  forked) against the Shelly Dimmer Gen4 at 192.168.12.204.
- Alarm: the core's own Alarm module. snooze() is gated HERE, in
  the tool, on the alarm actually ringing; kill() delegates the
  decision to the model (ringing -> stop; snooze pending ->
  cancel the refire; neither -> honest refusal).
- Weather: Open-Meteo REST direct from the Fedora (no key). The raw
  API carries precipitation probability, which the HA integration
  never exposed -- the spoken report includes it again.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .alarm import Alarm, ShellyLight

_LOGGER = logging.getLogger("lyra_core.tools")


@dataclass
class ToolResult:
    ok: bool
    confirmation: str  # the spoken line, built from the verified outcome
    state: dict = field(default_factory=dict)


def _pct(volume: float) -> int:
    return int(round(volume * 100))


# ---------------------------------------------------------------------------
# Light (Shelly Dimmer Gen4)
# ---------------------------------------------------------------------------


class LightTool:
    """Bedroom ceiling light via the alarm module's ShellyLight.

    Every action is followed by the client's GetStatus readback
    (ShellyLight.set_on/set_off already return it); the confirmation
    is composed from that readback, not from the request."""

    def __init__(self, light: ShellyLight) -> None:
        self._light = light

    async def get_state(self) -> dict | None:
        try:
            st = await self._light.get_status()
        except Exception:
            return None
        return {"on": bool(st.get("output")), "brightness": st.get("brightness")}

    async def turn_on(self) -> ToolResult:
        try:
            before = await self._light.get_status()
            brightness = before.get("brightness")
            if not isinstance(brightness, int) or not 1 <= brightness <= 100:
                brightness = 100  # no remembered level: full, like the alarm
            status = await self._light.set_on(brightness)
        except Exception as exc:
            _LOGGER.warning("light on failed: %s", exc)
            return ToolResult(False, "rejected: light unreachable")
        if status.get("output") is True:
            return ToolResult(
                True, f"light on: brightness {status.get('brightness')} percent",
                {"on": True, "brightness": status.get("brightness")},
            )
        return ToolResult(False, "rejected: light still off", {"on": False})

    async def turn_off(self) -> ToolResult:
        try:
            status = await self._light.set_off()
        except Exception as exc:
            _LOGGER.warning("light off failed: %s", exc)
            return ToolResult(False, "rejected: light unreachable")
        if status.get("output") is False:
            return ToolResult(True, "light: off", {"on": False})
        return ToolResult(False, "rejected: light still on", {"on": True})

    async def set_brightness(self, percent: int) -> ToolResult:
        try:
            status = await self._light.set_on(percent)
        except Exception as exc:
            _LOGGER.warning("light dim failed: %s", exc)
            return ToolResult(False, "rejected: light unreachable")
        actual = status.get("brightness")
        if status.get("output") is True and isinstance(actual, int):
            if abs(actual - percent) <= 1:
                return ToolResult(
                    True, f"light set: brightness {actual} percent",
                    {"on": True, "brightness": actual},
                )
            return ToolResult(
                False, f"rejected: light brightness is {actual} percent",
                {"on": True, "brightness": actual},
            )
        return ToolResult(False, "rejected: light still off", {"on": False})

    async def adjust_brightness(self, delta_points: int) -> ToolResult:
        """Relative change ("dim the light", "a little brighter"):
        read the LIVE Shelly state, shift by delta_points, clamp
        1..100 -- a down-step never lands on off -- then set;
        set_brightness's readback confirmation speaks the resulting
        absolute level, same as an absolute dim."""
        try:
            before = await self._light.get_status()
        except Exception as exc:
            _LOGGER.warning("light adjust read failed: %s", exc)
            return ToolResult(False, "rejected: light unreachable")
        base = before.get("brightness")
        if not isinstance(base, int) or not 1 <= base <= 100:
            base = 100  # no remembered level: same fallback as turn_on
        target = min(100, max(1, base + delta_points))
        return await self.set_brightness(target)


# ---------------------------------------------------------------------------
# TV brightness (lyra-web viewer, local HTTP)
# ---------------------------------------------------------------------------

LYRA_WEB = "http://127.0.0.1:8095"


def _http_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode())


class TvTool:
    """Brightness of the TV showing Lyra's web viewer (lyra-web
    :8095). The level lives in lyra-web's /dim state; the page polls
    it and fades its veil. Every set is verified by a GET readback,
    like every other tool here."""

    def __init__(self, base: str = LYRA_WEB) -> None:
        self._base = base.rstrip("/")

    async def _get(self) -> float | None:
        try:
            data = await asyncio.to_thread(_http_json, f"{self._base}/dim")
            return float(data["brightness"])
        except Exception as exc:
            _LOGGER.warning("tv brightness read failed: %s", exc)
            return None

    async def get_brightness(self) -> ToolResult:
        level = await self._get()
        if level is None:
            return ToolResult(False, "rejected: TV viewer unreachable", {})
        pct = _pct(level)
        return ToolResult(True, f"tv brightness: {pct} percent",
                          {"brightness": level, "percent": pct})

    async def set_brightness(self, percent: int) -> ToolResult:
        percent = max(0, min(100, int(percent)))
        try:
            await asyncio.to_thread(
                _http_json, f"{self._base}/dim", {"brightness": percent / 100.0}
            )
        except Exception as exc:
            _LOGGER.warning("tv brightness set failed: %s", exc)
            return ToolResult(False, "rejected: TV viewer unreachable", {})
        level = await self._get()
        if level is None:
            return ToolResult(False, "rejected: TV brightness unconfirmed", {})
        pct = _pct(level)
        if abs(pct - percent) <= 1:
            return ToolResult(True, f"tv brightness: {pct} percent",
                              {"brightness": level, "percent": pct})
        return ToolResult(False, f"rejected: TV brightness is {pct} percent",
                          {"brightness": level, "percent": pct})

    async def adjust_brightness(self, delta_points: int) -> ToolResult:
        level = await self._get()
        if level is None:
            return ToolResult(False, "rejected: TV viewer unreachable", {})
        return await self.set_brightness(_pct(level) + int(delta_points))


class TvVolumeTool:
    """Volume of the TV itself (the Fire TV at 192.168.12.132),
    driven over ADB: STREAM_MUSIC on the TV runs 0..100, so percent
    IS the index. Sets go through `media volume --set` and are
    verified by a dumpsys readback, like every other tool here.
    This is the TV's speaker volume -- not the screen brightness
    (TvTool)."""

    ADB_TARGET = "192.168.12.132:5555"

    def _adb(self, *args: str) -> str | None:
        out = self._adb_once(*args)
        if out is None:
            # The adb server loses the TV connection across host
            # reboots and adbd restarts, and nothing else ever
            # reconnects it -- the dashboard sat at "TV unreachable"
            # after the 2026-11-15 consolidation reboot for exactly
            # this reason. Self-heal: connect once, retry once.
            try:
                subprocess.run(
                    ["adb", "connect", self.ADB_TARGET],
                    capture_output=True, text=True, timeout=8,
                )
            except Exception:
                pass
            out = self._adb_once(*args)
        return out

    def _adb_once(self, *args: str) -> str | None:
        try:
            done = subprocess.run(
                ["adb", "-s", self.ADB_TARGET, "shell", *args],
                capture_output=True, text=True, timeout=8,
            )
            if done.returncode != 0:
                return None
            return done.stdout or ""
        except Exception as exc:
            _LOGGER.warning("tv volume adb failed: %s", exc)
            return None

    def _read_percent(self) -> int | None:
        out = self._adb("dumpsys", "audio")
        if not out:
            return None
        sec = out.split("- STREAM_MUSIC:", 1)
        if len(sec) < 2:
            return None
        m = re.search(r"2 \(speaker\): (\d+)", sec[1][:600])
        return int(m.group(1)) if m else None

    async def get_volume(self) -> ToolResult:
        pct = await asyncio.to_thread(self._read_percent)
        if pct is None:
            return ToolResult(False, "rejected: TV unreachable", {})
        return ToolResult(True, f"tv volume: {pct} percent",
                          {"volume": pct / 100.0, "percent": pct})

    async def set_volume(self, percent: int) -> ToolResult:
        percent = max(0, min(100, int(percent)))
        out = await asyncio.to_thread(
            self._adb, "media", "volume", "--stream", "3",
            "--set", str(percent))
        if out is None:
            return ToolResult(False, "rejected: TV unreachable", {})
        pct = await asyncio.to_thread(self._read_percent)
        if pct is None:
            return ToolResult(False, "rejected: TV volume unconfirmed", {})
        if abs(pct - percent) <= 2:
            return ToolResult(True, f"tv volume: {pct} percent",
                              {"volume": pct / 100.0, "percent": pct})
        return ToolResult(False, f"rejected: TV volume is {pct} percent",
                          {"volume": pct / 100.0, "percent": pct})

    async def adjust_volume(self, delta_points: int) -> ToolResult:
        pct = await asyncio.to_thread(self._read_percent)
        if pct is None:
            return ToolResult(False, "rejected: TV unreachable", {})
        return await self.set_volume(pct + int(delta_points))


# ---------------------------------------------------------------------------
# Alarm (delegates to the core's Alarm module)
# ---------------------------------------------------------------------------


class AlarmTool:
    """The router's surface onto the Alarm module. Confirmations are
    compact fact readbacks, not sentences (2026-11-12, same change as
    the weather digest): the router model composes the spoken reply
    from the facts plus the summary() state read AFTER the action.
    snooze checks the ringing flag here and refuses without touching
    the module when it isn't ringing. kill delegates to the module,
    which owns the three-way decision (stop the ring / cancel a
    pending snooze / honest refusal); the facts say which happened."""

    def __init__(self, alarm: Alarm) -> None:
        self._alarm = alarm

    def summary(self) -> dict:
        return self._alarm.summary()

    async def set_alarm(self, hhmm: str) -> ToolResult:
        try:
            self._alarm.set_alarm(hhmm)
        except ValueError:
            return ToolResult(False, "rejected: invalid time", self.summary())
        readback = self._alarm.summary()
        return ToolResult(True, f"alarm set: time={readback['time']}", readback)

    async def kill(self) -> ToolResult:
        result = await self._alarm.kill()
        if result.get("was_ringing"):
            return ToolResult(True, "alarm killed: was ringing", self.summary())
        if result.get("snooze_cleared"):
            return ToolResult(True, "alarm kill: pending snooze cancelled", self.summary())
        return ToolResult(False, "rejected: alarm not ringing", self.summary())

    async def snooze(self) -> ToolResult:
        if not self._alarm.summary()["ringing"]:
            return ToolResult(False, "rejected: alarm not ringing", self.summary())
        result = await self._alarm.snooze()
        if result.get("ok"):
            return ToolResult(True, "snoozed: refire in 9 minutes", self.summary())
        return ToolResult(False, "rejected: alarm not ringing", self.summary())

    async def set_enabled(self, enabled: bool) -> ToolResult:
        self._alarm.set_enabled(enabled)
        readback = self._alarm.summary()
        sentence = f"alarm enabled set: {str(readback['enabled']).lower()}"
        return ToolResult(readback["enabled"] == enabled, sentence, readback)

    async def get_morning_note(self) -> ToolResult:
        note = self._alarm.morning_note()
        if note.get("morning_note"):
            return ToolResult(
                True,
                f"morning_note date={note.get('morning_note_date')}: "
                f"{note['morning_note']}",
                self.summary(),
            )
        return ToolResult(True, "morning_note: none set", self.summary())

    async def set_morning_note(self, text: str, for_date: str) -> ToolResult:
        try:
            self._alarm.set_morning_note(text, for_date)
        except ValueError:
            return ToolResult(
                False, "rejected: invalid morning note", self.summary()
            )
        readback = self._alarm.morning_note()
        return ToolResult(
            True,
            f"morning_note set: date={readback.get('morning_note_date')}",
            self.summary(),
        )

    async def clear_morning_note(self) -> ToolResult:
        self._alarm.clear_morning_note()
        return ToolResult(True, "morning_note: cleared", self.summary())


# ---------------------------------------------------------------------------
# Clock (America/New_York; HH:MM military readbacks)
# ---------------------------------------------------------------------------


def _ordinal(day: int) -> str:
    if 11 <= day <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


class ClockTool:
    def __init__(self, timezone: str = "America/New_York", now_fn=None) -> None:
        self._tz = ZoneInfo(timezone)
        self._now_fn = now_fn or (lambda: datetime.now(self._tz))

    def time_result(self) -> ToolResult:
        now = self._now_fn()
        return ToolResult(True, f"time: {now:%H:%M}", {"time": f"{now:%H:%M}"})

    def date_result(self) -> ToolResult:
        now = self._now_fn()
        return ToolResult(
            True,
            f"date: {now:%A} {now:%B} {_ordinal(now.day)} {now:%Y}",
            {"date": now.date().isoformat()},
        )


# ---------------------------------------------------------------------------
# Weather (Open-Meteo, direct)
# ---------------------------------------------------------------------------

_WMO_TEXT = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail",
    99: "thunderstorms with hail",
}


def _hour_start_index(times: list, tz: ZoneInfo) -> int | None:
    """Index of the current hour in an Open-Meteo hourly time list."""
    now = datetime.now(tz).replace(minute=0, second=0, microsecond=0)
    for i, raw in enumerate(times):
        try:
            tt = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if tt.tzinfo is None:
            tt = tt.replace(tzinfo=tz)
        if tt >= now:
            return i
    return None


def _daily_line(daily: dict, i: int, focus: str = "overview") -> str:
    probs = daily.get("precipitation_probability_max") or []
    codes = daily.get("weather_code") or []
    winds = daily.get("wind_speed_10m_max") or []
    try:
        day = datetime.fromisoformat(daily["time"][i])
    except (ValueError, IndexError):
        return ""
    label = "Today" if i == 0 else ("Tomorrow" if i == 1 else f"{day:%a %b %d}")
    cond = _WMO_TEXT.get(int(codes[i]), "unknown conditions") if i < len(codes) and codes[i] is not None else "unknown conditions"
    temps = f"high {int(round(daily['temperature_2m_max'][i]))}F, low {int(round(daily['temperature_2m_min'][i]))}F"
    rain = f"rain chance up to {int(round(probs[i]))}%" if i < len(probs) and probs[i] is not None else None
    wind = f"wind up to {int(round(winds[i]))} mph" if i < len(winds) and winds[i] is not None else None
    if focus == "rain" and rain:
        parts = [rain, temps, cond]
    elif focus == "wind" and wind:
        parts = [wind, temps, cond]
    else:
        parts = [temps, cond] + [x for x in (rain, wind) if x]
    return f"{label}: " + ", ".join(parts) + "."


def _hourly_line(times, temps, codes, probs, winds, i: int, focus: str) -> str:
    try:
        hh = f"{datetime.fromisoformat(times[i]):%H}:00"
    except ValueError:
        hh = str(times[i])
    temp = f"{int(round(temps[i]))}" if i < len(temps) and temps[i] is not None else "?"
    cond = _WMO_TEXT.get(int(codes[i]), "?") if i < len(codes) and codes[i] is not None else "?"
    prob = f"{int(round(probs[i]))}%" if i < len(probs) and probs[i] is not None else "-"
    wind = f"{int(round(winds[i]))}mph" if i < len(winds) and winds[i] is not None else "-"
    if focus == "rain":
        return f"{hh} {cond} rain {prob}"
    if focus == "wind":
        return f"{hh} {cond} wind {wind} {temp}F"
    if focus == "temperature":
        return f"{hh} {temp} {cond}"
    return f"{hh} {temp} {cond} rain {prob} wind {wind}"


def compose_weather_digest(data: dict, tz: ZoneInfo, period: str = "overview", focus: str = "overview") -> str:
    """The weather tool's facts, unscripted and sliced: the code
    extracts exactly from the API response; period/focus (the router
    model's choice, from the question asked) decide which slice.
    The model composes the spoken answer; the code writes no
    sentences about the weather itself."""
    cur = data["current"]
    now = datetime.now(tz)
    lines = [f"Weather data for home. Now {now:%a %b %d, %H:%M}."]
    lines.append(
        f"Now: {int(round(cur['temperature_2m']))}F, "
        f"{_WMO_TEXT.get(int(cur['weather_code']), 'unknown conditions')}, "
        f"feels like {int(round(cur['apparent_temperature']))}F, "
        f"humidity {int(round(cur['relative_humidity_2m']))}%, "
        f"wind {int(round(cur['wind_speed_10m']))} mph."
    )
    daily = data["daily"]
    hourly = data.get("hourly") or {}
    times = hourly.get("time") or []
    temps = hourly.get("temperature_2m") or []
    hcodes = hourly.get("weather_code") or []
    hprobs = hourly.get("precipitation_probability") or []
    hwinds = hourly.get("wind_speed_10m") or []
    start = _hour_start_index(times, tz)
    day_count = len(daily.get("time") or [])

    def day_hours(day_offset: int):
        target = (now + timedelta(days=day_offset)).date().isoformat()
        return [i for i, raw in enumerate(times) if str(raw)[:10] == target]

    def add_hours(indices, header):
        if not indices:
            return
        lines.append(header)
        for i in indices:
            lines.append(_hourly_line(times, temps, hcodes, hprobs, hwinds, i, focus))

    if period == "now":
        if start is not None:
            add_hours(list(range(start, min(start + 6, len(times)))), "Next 6 hours (time, conditions, values):")
    elif period == "today":
        if day_count > 0:
            lines.append(_daily_line(daily, 0, focus))
        idx = [i for i in day_hours(0) if start is not None and i >= start]
        add_hours(idx, "Today, remaining hours (time, conditions, values):")
    elif period == "tomorrow":
        if day_count > 1:
            lines.append(_daily_line(daily, 1, focus))
        add_hours(day_hours(1), "Tomorrow, hourly (time, conditions, values):")
    elif period == "week":
        for i in range(day_count):
            lines.append(_daily_line(daily, i, focus))
    else:  # overview
        for i in range(min(3, day_count)):
            lines.append(_daily_line(daily, i, focus))
        if start is not None:
            add_hours(list(range(start, min(start + 24, len(times)))), "Next 24 hours (time, temp F, conditions, rain chance, wind):")
    return "\n".join(lines)


class WeatherTool:
    def __init__(
        self,
        latitude: float,
        longitude: float,
        timezone: str = "America/New_York",
        fetcher=None,
    ) -> None:
        tz_q = urllib.parse.quote(timezone, safe="")
        self._url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={latitude}&longitude={longitude}"
            "&current=temperature_2m,weather_code,apparent_temperature,relative_humidity_2m,wind_speed_10m"
            "&hourly=temperature_2m,weather_code,precipitation_probability,wind_speed_10m"
            "&daily=temperature_2m_max,temperature_2m_min,weather_code,precipitation_probability_max,wind_speed_10m_max"
            "&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&timezone={tz_q}&forecast_days=7"
        )
        self._tz = ZoneInfo(timezone)
        self._fetcher = fetcher or self._fetch_live

    async def _fetch_live(self) -> dict:
        def _get() -> dict:
            req = urllib.request.Request(
                self._url, headers={"User-Agent": "lyra-core"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.loads(resp.read())

        return await asyncio.to_thread(_get)

    async def report(self, period: str = "overview", focus: str = "overview") -> ToolResult:
        period = period if period in ("overview", "now", "today", "tomorrow", "week") else "overview"
        focus = focus if focus in ("overview", "rain", "temperature", "wind") else "overview"
        try:
            data = await self._fetcher()
            text = compose_weather_digest(data, self._tz, period, focus)
        except Exception as exc:
            _LOGGER.warning("weather fetch/digest failed: %s", exc)
            return ToolResult(False, "I couldn't get the weather.")
        return ToolResult(True, text, {})


# ---------------------------------------------------------------------------
# Jellyfin (local server + the Desktop client on this machine)
# ---------------------------------------------------------------------------

JELLYFIN_BASE = "http://127.0.0.1:8097"
JELLYFIN_ENV = Path("~/Documents/APIKeys/.env").expanduser()
JELLYFIN_TV_OUTPUT = "HDMI-A-1"
_TICKS_PER_SECOND = 10_000_000

# One-shot window mover: the tool spawns the listener, loads the
# script into KWin, and the script moves the Jellyfin client window
# to the TV output, fullscreens it, and reports back through the
# listener. Nothing stays running afterward -- this is a function
# call, not a background sorter (the standing guard script was
# deleted at Daniel's order on 2026-11-21 and stays deleted).
_MOVER_LISTENER = '''
import sys
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

xml = (
    '<node><interface name="org.lyra.JellyfinMove">'
    '<method name="Report">'
    '<arg type="s" name="text" direction="in"/>'
    '</method></interface></node>'
)
node = Gio.DBusNodeInfo.new_for_xml(xml)
out_path = sys.argv[1]
loop = GLib.MainLoop()

def on_call(conn, sender, path, iface, method, params, invocation):
    with open(out_path, "w") as fh:
        fh.write(params.unpack()[0])
    invocation.return_value(None)

def on_acquired(conn, name):
    conn.register_object("/Move", node.interfaces[0], on_call, None, None)
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w") as fh:
            fh.write("ready")

Gio.bus_own_name(Gio.BusType.SESSION, "org.lyra.JellyfinMove",
                 Gio.BusNameOwnerFlags.NONE, None, on_acquired, None)
# The backstop must outlive the whole move+verify: the
# fullscreen settle polling runs to ~a minute. (At 15s the
# listener died mid-verification and every later report
# vanished -- the tool then polled a dead bus, 2026-11-21.)
# The tool terminates the listener itself; this only guards
# a leaked process.
GLib.timeout_add_seconds(180, loop.quit)
loop.run()
'''

_MOVER_JS = """
var wins = workspace.windowList();
var report = 'no-window';
for (var i = 0; i < wins.length; i++) {
  var w = wins[i];
  var cls = (w.resourceClass || '').toLowerCase();
  if (cls.indexOf('jellyfin') < 0) continue;
  var screens = workspace.screens;
  var target = null;
  for (var j = 0; j < screens.length; j++) {
    if (screens[j].name === 'HDMI-A-1') { target = screens[j]; break; }
  }
  if (!target) {
    report = 'no-tv-output';
  } else {
    if (!w.output || w.output.name !== 'HDMI-A-1') {
      workspace.sendClientToScreen(w, target);
    }
    // Only set what is wrong: re-setting fullscreen on an
    // already fullscreen window restarts its transition and
    // the readback then lags for seconds.
    if (!w.fullScreen) {
      try { w.fullScreen = true; } catch (e) {}
    }
    report = 'at=' + (w.output ? w.output.name : '?')
      + ' fs=' + (w.fullScreen ? 'true' : 'false');
  }
  break;
}
callDBus('org.lyra.JellyfinMove', '/Move', 'org.lyra.JellyfinMove', 'Report', report);
"""

# Read-only companion to the mover: reports the client window's
# output and fullscreen state without touching anything, so the
# tool can poll the settled state after a set.
_STATE_JS = """
var wins = workspace.windowList();
var report = 'no-window';
for (var i = 0; i < wins.length; i++) {
  var w = wins[i];
  var cls = (w.resourceClass || '').toLowerCase();
  if (cls.indexOf('jellyfin') < 0) continue;
  report = 'at=' + (w.output ? w.output.name : '?')
    + ' fs=' + (w.fullScreen ? 'true' : 'false');
  break;
}
callDBus('org.lyra.JellyfinMove', '/Move', 'org.lyra.JellyfinMove', 'Report', report);
"""


def _jellyfin_key() -> str | None:
    """The Jellyfin API key from Daniel's central APIKeys .env.
    Read fresh on every call so a rotation never needs a core
    restart; the value lives only in memory, never logged."""
    try:
        lines = JELLYFIN_ENV.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if line.startswith("JELLYFIN_API_KEY="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


def _jf_request(method: str, path: str, key: str,
                params: dict | None = None) -> tuple[int, object]:
    url = JELLYFIN_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, method=method)
    # Jellyfin 12 takes API keys ONLY in the Authorization header;
    # X-Emby-Token gets a 401 (learned 2026-11-21).
    req.add_header("Authorization", f'MediaBrowser Token="{key}"')
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return 0, None


def _ticks_clock(ticks) -> str:
    secs = int((ticks or 0) // _TICKS_PER_SECOND)
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _item_label(item: dict) -> str:
    name = item.get("Name") or "unknown title"
    if item.get("Type") == "Episode":
        series = item.get("SeriesName") or ""
        s = item.get("ParentIndexNumber")
        e = item.get("IndexNumber")
        ep = f" S{s}E{e}" if s is not None and e is not None else ""
        return f"{series}{ep}: {name}" if series else name
    year = item.get("ProductionYear")
    return f"{name} ({year})" if year else name


def _watched_note(item: dict) -> str:
    ud = item.get("UserData") or {}
    if ud.get("Played"):
        return "watched"
    unplayed = ud.get("UnplayedItemCount")
    if isinstance(unplayed, int) and unplayed > 0:
        return f"{unplayed} unwatched"
    if (ud.get("PlaybackPositionTicks") or 0) > 0:
        return "in progress"
    return "unwatched"


class JellyfinTool:
    """Jellyfin on the TV. The server runs on this machine
    (127.0.0.1:8097); the player is the Jellyfin Desktop client on
    this machine, launched on demand, moved to the TV output, and
    driven through the server's session API. Every outcome is
    verified against the session's own report (NowPlayingItem /
    PlayState) before it is claimed -- a command having been sent
    is never treated as proof it happened. Playback runs through
    the machine's default audio like everything else of hers, so
    pausing the movie to hear a command is an ordinary control
    call."""

    CLIENT_APP = "org.jellyfin.JellyfinDesktop"
    CLIENT_NAME = "Jellyfin Desktop"
    _PLAYABLE = ("Movie", "Series", "Episode")

    def __init__(self) -> None:
        self._user_id: str | None = None

    # -- plumbing ----------------------------------------------------

    async def _call(self, method: str, path: str,
                    params: dict | None = None) -> tuple[int, object]:
        key = _jellyfin_key()
        if not key:
            _LOGGER.warning("jellyfin: no API key in %s", JELLYFIN_ENV)
            return 0, None
        return await asyncio.to_thread(_jf_request, method, path, key, params)

    async def _primary_user_id(self) -> str | None:
        if self._user_id:
            return self._user_id
        code, data = await self._call("GET", "/Users")
        if code == 200 and isinstance(data, list) and data:
            self._user_id = data[0].get("Id")
        return self._user_id

    async def _sessions(self) -> list:
        code, data = await self._call("GET", "/Sessions")
        if code != 200 or not isinstance(data, list):
            return []
        return data

    async def _desktop_session(self) -> dict | None:
        for sess in await self._sessions():
            if (sess.get("Client") == self.CLIENT_NAME
                    and sess.get("SupportsRemoteControl")):
                return sess
        return None

    async def _search(self, query: str) -> list[dict] | None:
        """Playable library matches, or None when the server call
        itself failed (unreachable / unauthorized)."""
        params: dict = {
            "SearchTerm": query, "Recursive": "true", "Limit": 10,
        }
        uid = await self._primary_user_id()
        if uid:
            params["UserId"] = uid
        code, data = await self._call("GET", "/Items", params)
        if code != 200 or not isinstance(data, dict):
            return None
        return [i for i in data.get("Items") or []
                if i.get("Type") in self._PLAYABLE]

    # -- find ----------------------------------------------------------

    async def find(self, query: str) -> ToolResult:
        query = (query or "").strip()
        if not query:
            return ToolResult(False, "rejected: no search title given", {})
        matches = await self._search(query)
        if matches is None:
            return ToolResult(False, "rejected: jellyfin server unreachable", {})
        if not matches:
            return ToolResult(True, f'jellyfin search "{query}": no matches',
                              {"matches": []})
        lines = [f'jellyfin search "{query}": {len(matches)} match(es)']
        brief = []
        for item in matches:
            kind = (item.get("Type") or "").lower()
            lines.append(
                f"- {_item_label(item)}, {kind}, {_watched_note(item)}")
            brief.append({
                "id": item.get("Id"), "name": item.get("Name"),
                "type": item.get("Type"),
                "year": item.get("ProductionYear"),
            })
        return ToolResult(True, "\n".join(lines), {"matches": brief})

    # -- play ----------------------------------------------------------

    def _resolve(self, matches: list[dict], title: str) -> dict | None:
        want = title.strip().lower()
        for item in matches:
            if (item.get("Name") or "").strip().lower() == want:
                return item
        if len(matches) == 1:
            return matches[0]
        return None

    async def _next_episode(self, series: dict) -> dict | None:
        uid = await self._primary_user_id()
        if not uid:
            return None
        code, data = await self._call("GET", "/Shows/NextUp", {
            "UserId": uid, "SeriesId": series.get("Id"), "Limit": 1,
        })
        if code == 200 and isinstance(data, dict):
            items = data.get("Items") or []
            if items:
                return items[0]
        # NextUp keys off the last-played episode, so a series
        # whose finale is played but early episodes are not
        # (Daniel's Seven Havens: E4-E12 played, E1-E3 not)
        # returns nothing. Fall back to the episode list: an
        # in-progress episode first, else the first unplayed.
        code, data = await self._call("GET", "/Items", {
            "ParentId": series.get("Id"), "Recursive": "true",
            "IncludeItemTypes": "Episode", "UserId": uid,
            "SortBy": "ParentIndexNumber,IndexNumber", "Limit": 50,
        })
        if code == 200 and isinstance(data, dict):
            episodes = data.get("Items") or []
            for ep in episodes:
                ud = ep.get("UserData") or {}
                if not ud.get("Played") and (ud.get("PlaybackPositionTicks") or 0) > 0:
                    return ep
            for ep in episodes:
                if not (ep.get("UserData") or {}).get("Played"):
                    return ep
        return None

    async def play(self, title: str) -> ToolResult:
        title = (title or "").strip()
        if not title:
            return ToolResult(False, "rejected: no title given", {})
        matches = await self._search(title)
        if matches is None:
            return ToolResult(False, "rejected: jellyfin server unreachable", {})
        if not matches:
            return ToolResult(
                False, f'jellyfin: nothing in the library matches "{title}"', {})
        item = self._resolve(matches, title)
        if item is None:
            names = "; ".join(
                f"{_item_label(m)} ({(m.get('Type') or '').lower()})"
                for m in matches[:4])
            return ToolResult(
                False, f'jellyfin: several matches for "{title}": {names}', {})
        target = item
        if item.get("Type") == "Series":
            target = await self._next_episode(item)
            if target is None:
                return ToolResult(
                    False,
                    f"jellyfin: no unwatched episode of {_item_label(item)}", {})
        session, screen, fullscreen = await self._ensure_session()
        if session is None:
            return ToolResult(
                False, "rejected: jellyfin player did not start", {})
        params: dict = {"ItemIds": target.get("Id"), "PlayCommand": "PlayNow"}
        start = (target.get("UserData") or {}).get("PlaybackPositionTicks") or 0
        if start > 0:
            params["StartPositionTicks"] = start
        # The Playing endpoint takes its arguments as query
        # parameters; a JSON body fails validation (400).
        code, _ = await self._call(
            "POST", f"/Sessions/{session['Id']}/Playing", params)
        if code not in (200, 204):
            return ToolResult(
                False, f"rejected: jellyfin refused the play command ({code})", {})
        label = _item_label(target)
        for _ in range(6):
            await asyncio.sleep(1.0)
            sess = await self._desktop_session()
            npi = (sess or {}).get("NowPlayingItem") or {}
            if npi.get("Id") == target.get("Id"):
                where = ""
                if screen == "tv":
                    where = " on the TV, fullscreen" if fullscreen else " on the TV"
                return ToolResult(
                    True, f"jellyfin playing: {label}{where}",
                    {"playing": label, "item_id": target.get("Id"),
                     "screen": screen, "fullscreen": fullscreen})
        return ToolResult(
            False, "rejected: jellyfin did not start playing", {})

    async def _ensure_session(self) -> tuple[dict | None, str, bool]:
        """The Desktop client's session, launching the client first
        when needed. Returns (session, screen, fullscreen): screen
        is 'tv' when the mover's report verifies the client window
        sits on the TV output, and fullscreen says whether that
        report verified the window fullscreen there. Her launches
        are fullscreen-on-the-TV launches -- verified, not assumed."""
        session = await self._desktop_session()
        if session is None:
            env = dict(os.environ)
            env.setdefault("WAYLAND_DISPLAY", "wayland-1")
            env.setdefault("DISPLAY", ":0")
            try:
                subprocess.Popen(
                    ["flatpak", "run", self.CLIENT_APP],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, env=env,
                )
            except Exception as exc:
                _LOGGER.warning("jellyfin client launch failed: %s", exc)
                return None, "launch-failed", False
            for _ in range(30):
                await asyncio.sleep(1.5)
                session = await self._desktop_session()
                if session is not None:
                    break
            if session is None:
                return None, "no-session", False
        screen, fullscreen = await self._move_to_tv()
        return session, screen, fullscreen

    async def _move_to_tv(self) -> tuple[str, bool]:
        try:
            return await asyncio.to_thread(self._move_to_tv_sync)
        except Exception as exc:
            _LOGGER.warning("jellyfin move failed: %s", exc)
            return "unconfirmed", False

    def _move_to_tv_sync(self) -> tuple[str, bool]:
        report = Path("/tmp/lyra_jellyfin_move.txt")
        ready = Path("/tmp/lyra_jellyfin_ready.txt")
        listener_src = Path("/tmp/lyra_jellyfin_listener.py")
        mover_js = Path("/tmp/lyra_jellyfin_mover.js")
        for stale in (report, ready):
            try:
                stale.unlink()
            except OSError:
                pass
        listener_src.write_text(_MOVER_LISTENER)
        mover_js.write_text(_MOVER_JS)
        listener = subprocess.Popen(
            # System python, NOT sys.executable: the core runs on
            # a venv without PyGObject and the listener needs gi
            # (the first version died on that import, so every
            # move reported 'unconfirmed', 2026-11-21).
            ["/usr/bin/python3", str(listener_src), str(report), str(ready)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            # Wait until the listener actually owns its bus name:
            # a fixed sleep races the script's report and loses it
            # (the first live play moved the window to the TV but
            # the report went nowhere, 2026-11-21).
            deadline = time.time() + 5
            while not ready.exists() and time.time() < deadline:
                time.sleep(0.2)
            if not ready.exists():
                return "unconfirmed", False
            bus = ["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
                   "org.kde.kwin.Scripting"]

            def run_scripts() -> None:
                subprocess.run(bus + ["start"], capture_output=True, timeout=8)

            def read_report(wait_s: float) -> str | None:
                deadline = time.time() + wait_s
                while time.time() < deadline:
                    if report.exists():
                        text = report.read_text().strip()
                        try:
                            report.unlink()
                        except OSError:
                            pass
                        return text
                    time.sleep(0.3)
                return None

            def parse(text: str | None) -> tuple[str, bool] | None:
                if text is None or not text.startswith("at="):
                    return None
                fields = dict(
                    part.split("=", 1) for part in text.split()
                    if "=" in part)
                return fields.get("at"), fields.get("fs") == "true"

            state_js = Path("/tmp/lyra_jellyfin_state.js")
            state_js.write_text(_STATE_JS)
            try:
                subprocess.run(
                    bus + ["loadScript", "ss", str(mover_js), "jellyfinmove"],
                    capture_output=True, timeout=8)
                # Phase 1: the mover puts the window right. It only
                # touches what is wrong, so a warm window's report
                # is its true state, immediately.
                run_scripts()
                text = read_report(8)
                seen = parse(text)
                if seen is not None and seen == (JELLYFIN_TV_OUTPUT, True):
                    return "tv", True
                if text == "no-tv-output":
                    return "unconfirmed", False
                if seen is None:
                    run_scripts()
                    text = read_report(5)
                    seen = parse(text)
                    if seen is not None and seen == (JELLYFIN_TV_OUTPUT, True):
                        return "tv", True
                    if text == "no-tv-output":
                        return "unconfirmed", False
                # Phase 2: poll the settled state with the
                # read-only script. A cold-launched client can
                # apply its own initial (windowed) state AFTER the
                # mover's first set, so twice during the polling
                # the mover runs again to re-assert fullscreen on
                # the by-then-ready window.
                subprocess.run(
                    bus + ["unloadScript", "s", "jellyfinmove"],
                    capture_output=True, timeout=8)

                def load(name: str, path: Path) -> None:
                    subprocess.run(
                        bus + ["loadScript", "ss", str(path), name],
                        capture_output=True, timeout=8)

                def unload(name: str) -> None:
                    subprocess.run(
                        bus + ["unloadScript", "s", name],
                        capture_output=True, timeout=8)

                # One script loaded at a time: with the mover and
                # the state script loaded together, start() produced
                # no reports at all (2026-11-21).
                load("jellyfinstate", state_js)
                best = seen
                for poll in range(8):
                    if poll in (2, 5):
                        unload("jellyfinstate")
                        load("jellyfinmove", mover_js)
                        run_scripts()
                        seen = parse(read_report(5))
                        if seen is not None:
                            best = seen
                            if seen == (JELLYFIN_TV_OUTPUT, True):
                                return "tv", True
                        unload("jellyfinmove")
                        load("jellyfinstate", state_js)
                    time.sleep(1.5)
                    run_scripts()
                    seen = parse(read_report(5))
                    if seen is not None:
                        best = seen
                        if seen == (JELLYFIN_TV_OUTPUT, True):
                            return "tv", True
                if best is not None:
                    return ("tv" if best[0] == JELLYFIN_TV_OUTPUT
                            else "unconfirmed"), best[1]
                return "unconfirmed", False
            finally:
                # Leave nothing loaded behind: a resident mover
                # would be a background sorter by another name.
                subprocess.run(
                    bus + ["unloadScript", "s", "jellyfinmove"],
                    capture_output=True, timeout=8)
                subprocess.run(
                    bus + ["unloadScript", "s", "jellyfinstate"],
                    capture_output=True, timeout=8)
                try:
                    state_js.unlink()
                except OSError:
                    pass
        finally:
            try:
                listener.terminate()
            except Exception:
                pass
            for path in (listener_src, mover_js, report, ready):
                try:
                    path.unlink()
                except OSError:
                    pass

    async def fullscreen(self) -> ToolResult:
        """Put the player fullscreen on the TV, launching it first
        when it is not running (the same launch path play uses).
        The claim comes from the mover's readback of the window's
        own output and fullscreen state, never from the request."""
        session, screen, fullscreen = await self._ensure_session()
        if session is None:
            return ToolResult(
                False, "rejected: jellyfin player did not start", {})
        if screen == "tv" and fullscreen:
            return ToolResult(
                True, "jellyfin: fullscreen on the TV",
                {"screen": "tv", "fullscreen": True})
        if screen == "tv":
            return ToolResult(
                False, "rejected: jellyfin is on the TV but not fullscreen",
                {"screen": "tv", "fullscreen": False})
        return ToolResult(
            False, "rejected: jellyfin window placement unconfirmed",
            {"screen": screen, "fullscreen": fullscreen})

    # -- control ---------------------------------------------------------

    async def control(self, action: str) -> ToolResult:
        action = (action or "").strip().lower()
        session = await self._desktop_session()
        if session is None:
            return ToolResult(
                False, "rejected: jellyfin player is not running", {})
        sid = session["Id"]
        npi = session.get("NowPlayingItem") or {}
        label = npi.get("Name")
        if action == "stop":
            if not label:
                return ToolResult(False, "rejected: nothing is playing", {})
            code, _ = await self._call("POST", f"/Sessions/{sid}/Playing/Stop")
            if code not in (200, 204):
                return ToolResult(
                    False, f"rejected: jellyfin refused stop ({code})", {})
            await asyncio.sleep(1.0)
            sess = await self._desktop_session()
            if not (sess or {}).get("NowPlayingItem"):
                return ToolResult(True, f"jellyfin stopped: {label}", {})
            return ToolResult(False, "rejected: jellyfin is still playing", {})
        if action in ("pause", "resume"):
            if not label:
                return ToolResult(False, "rejected: nothing is playing", {})
            verb = "Pause" if action == "pause" else "Unpause"
            code, _ = await self._call(
                "POST", f"/Sessions/{sid}/Playing/{verb}")
            if code not in (200, 204):
                return ToolResult(
                    False, f"rejected: jellyfin refused {action} ({code})", {})
            await asyncio.sleep(1.0)
            sess = await self._desktop_session()
            paused = ((sess or {}).get("PlayState") or {}).get("IsPaused")
            if action == "pause" and paused is True:
                return ToolResult(True, f"jellyfin paused: {label}", {})
            if action == "resume" and paused is False:
                return ToolResult(True, f"jellyfin playing: {label}", {})
            return ToolResult(
                False, f"rejected: jellyfin {action} unconfirmed", {})
        if action in ("skip_forward", "skip_back"):
            if not label:
                return ToolResult(False, "rejected: nothing is playing", {})
            play_state = session.get("PlayState") or {}
            pos = play_state.get("PositionTicks") or 0
            delta_s = 30 if action == "skip_forward" else -15
            target = max(0, pos + delta_s * _TICKS_PER_SECOND)
            code, _ = await self._call(
                "POST", f"/Sessions/{sid}/Playing/Seek",
                {"SeekPositionTicks": int(target)})
            if code not in (200, 204):
                return ToolResult(
                    False, f"rejected: jellyfin refused seek ({code})", {})
            await asyncio.sleep(1.0)
            sess = await self._desktop_session()
            new_pos = ((sess or {}).get("PlayState") or {}).get("PositionTicks")
            runtime = npi.get("RunTimeTicks")
            if new_pos is not None and abs(new_pos - target) < 15 * _TICKS_PER_SECOND:
                direction = "forward" if delta_s > 0 else "back"
                return ToolResult(
                    True,
                    f"jellyfin skipped {direction}: at {_ticks_clock(new_pos)}"
                    f" of {_ticks_clock(runtime)}",
                    {"position_ticks": new_pos})
            return ToolResult(False, "rejected: jellyfin seek unconfirmed", {})
        return ToolResult(False, f"rejected: unknown jellyfin action {action!r}", {})

    # -- status ----------------------------------------------------------

    async def _resume_lines(self) -> list[str]:
        uid = await self._primary_user_id()
        if not uid:
            return []
        code, data = await self._call(
            "GET", f"/Users/{uid}/Items/Resume",
            {"UserId": uid, "Limit": 3})
        if code != 200 or not isinstance(data, dict):
            return []
        lines = []
        for item in data.get("Items") or []:
            ud = item.get("UserData") or {}
            pct = ud.get("PlayedPercentage")
            note = f", {int(round(pct))}% watched" if pct else ""
            lines.append(f"- continue: {_item_label(item)}{note}")
        return lines

    async def status(self) -> ToolResult:
        session = await self._desktop_session()
        if session is not None:
            npi = session.get("NowPlayingItem") or {}
            if npi.get("Name"):
                play_state = session.get("PlayState") or {}
                pos = play_state.get("PositionTicks")
                runtime = npi.get("RunTimeTicks")
                mode = "paused" if play_state.get("IsPaused") else "playing"
                return ToolResult(
                    True,
                    f"jellyfin now playing: {_item_label(npi)} -- "
                    f"{_ticks_clock(pos)} of {_ticks_clock(runtime)}, {mode}",
                    {"playing": npi.get("Name"),
                     "paused": bool(play_state.get("IsPaused"))})
            lines = ["jellyfin: player open, nothing playing"]
            state: dict = {"playing": None}
        else:
            lines = ["jellyfin: player not running"]
            state = {"playing": None}
        lines.extend(await self._resume_lines())
        return ToolResult(True, "\n".join(lines), state)
