"""Tools: the fixed set the router (LLM-classified, slot-validated) can call.

Each tool executes ONE action and verifies it by readback, then
reports a ToolResult whose ``confirmation`` is the short spoken line
(Decision D2) built from the ACTUAL outcome -- a readback that
contradicts the command produces a sentence saying what really
happened, never a success claim. The set is closed: light, tv brightness,
tv volume, alarm, clock, weather. Nothing here can call anything outside it
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
import re
import subprocess
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

    async def set_ring_volume(self, volume: float) -> ToolResult:
        try:
            self._alarm.set_ring_volume(volume)
        except ValueError:
            return ToolResult(
                False,
                "rejected: ring volume must be 0-100 percent",
                self.summary(),
            )
        readback = self._alarm.summary()  # the stored value, re-read
        return ToolResult(
            True,
            f"ring_volume set: {_pct(readback['ring_volume'])} percent",
            readback,
        )

    async def get_ring_volume(self) -> ToolResult:
        readback = self._alarm.summary()
        return ToolResult(
            True,
            f"ring_volume: {_pct(readback['ring_volume'])} percent",
            readback,
        )

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
