"""TV alarm sounder (2026-11-15): the alarm rings through the TV.

Daniel removed the speaker box from the output equation entirely --
the TV is the only audio out -- so the alarm's sounder moved too.
The Alarm state machine owns the ring (schedule, kill, snooze, hard
stop, morning note); this class is just the noise-maker it drives
when the composition wires one in via Alarm.set_tv_ringer:

- start(): make sure her face is up (write the display state on --
  the route-apply path unit starts the kiosk), wake the TV panel
  over ADB, save the TV's current volume and set the ring volume,
  then ask lyra-web to ring. The web page loops the alarm tone and
  acks while it is actually sounding; the Alarm's watchdog reads
  those acks and swaps the ring to the box if the TV path fails
  silently. start() reports failure (returns False) when lyra-web
  is unreachable or the TV cannot be read over ADB at all -- a TV
  that ADB cannot see is asleep or off, and a ring nobody hears is
  worse than a fallback.
- stop(): tell lyra-web to stop and restore the saved TV volume.
- recover(): startup cleanup after a core restart mid-ring -- stop
  any lingering page ring and restore the persisted TV volume.

Everything is best-effort and logged; nothing here raises into the
alarm. ADB usage mirrors TvVolumeTool (same target, same readback).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
import time
import urllib.request
from pathlib import Path

_LOGGER = logging.getLogger("lyra_core.tv_ring")


class TvRinger:
    WEB_BASE = "http://127.0.0.1:8095"
    ADB_TARGET = "192.168.12.132:5555"
    DISPLAY_STATE_PATH = Path(
        "~/Documents/LyraOS/web/display_state.json"
    ).expanduser()

    def __init__(self) -> None:
        # Pre-ring TV volume (0..1), held between start() and stop()
        # and mirrored into the alarm state by the Alarm so a core
        # restart mid-ring can still restore it.
        self.saved_tv_volume: float | None = None

    # -- lyra-web ------------------------------------------------------

    def _web_sync(self, method: str, path: str,
                  payload: dict | None = None) -> dict | None:
        try:
            data = (
                json.dumps(payload).encode() if payload is not None else None
            )
            req = urllib.request.Request(
                self.WEB_BASE + path, data=data, method=method,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception as exc:
            _LOGGER.warning("tv ring: lyra-web %s %s failed: %s",
                            method, path, exc)
            return None

    # -- TV over ADB (mirrors TvVolumeTool) -----------------------------

    def _adb(self, *args: str) -> str | None:
        out = self._adb_once(*args)
        if out is None:
            # Same self-heal as TvVolumeTool: reconnect once and
            # retry, so a dropped adb connection cannot silently
            # cost a morning ring its TV.
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
            _LOGGER.warning("tv ring: adb failed: %s", exc)
            return None

    def _read_tv_percent(self) -> int | None:
        out = self._adb("dumpsys", "audio")
        if not out:
            return None
        sec = out.split("- STREAM_MUSIC:", 1)
        if len(sec) < 2:
            return None
        m = re.search(r"2 \(speaker\): (\d+)", sec[1][:600])
        return int(m.group(1)) if m else None

    def _set_tv_percent(self, percent: int) -> bool:
        percent = max(0, min(100, int(percent)))
        out = self._adb("media", "volume", "--stream", "3",
                        "--set", str(percent))
        if out is None:
            return False
        readback = self._read_tv_percent()
        return readback is not None and abs(readback - percent) <= 2

    # -- display + panel -------------------------------------------------

    def _ensure_display_on(self) -> None:
        """Write the display state ON in place (same file + same
        in-place style as the dashboard) so lyra-route-watch starts
        the kiosk and her face -- the ringing page -- comes up."""
        try:
            state = {"display_on": True, "updated": time.time()}
            try:
                with open(self.DISPLAY_STATE_PATH, "r+") as f:
                    f.seek(0)
                    json.dump(state, f)
                    f.truncate()
            except FileNotFoundError:
                self.DISPLAY_STATE_PATH.write_text(json.dumps(state))
        except Exception as exc:
            _LOGGER.warning("tv ring: display state write failed: %s", exc)

    # -- the sounder surface the Alarm drives ----------------------------

    def _start_sync(self, ring_volume: float) -> bool:
        self._ensure_display_on()
        # Wake the panel, then give it a moment: a TV in light sleep
        # needs a beat before its audio service answers dumpsys.
        self._adb("input", "keyevent", "KEYCODE_WAKEUP")
        current = None
        for _ in range(3):
            time.sleep(1.0)
            current = self._read_tv_percent()
            if current is not None:
                break
        if current is None:
            _LOGGER.error(
                "tv ring: TV unreachable over ADB after wake; "
                "the ring cannot be heard on the TV"
            )
            return False
        self.saved_tv_volume = current / 100.0
        target = round(max(0.0, min(1.0, ring_volume)) * 100)
        if abs(current - target) > 2 and not self._set_tv_percent(target):
            _LOGGER.error(
                "tv ring: could not set the TV volume to %d%% "
                "(read %d%%); ringing anyway at the current volume",
                target, current,
            )
        resp = self._web_sync("POST", "/ring", {"ringing": True})
        if not (resp and resp.get("ok")):
            _LOGGER.error("tv ring: lyra-web refused the ring start")
            # Undo the volume change on the way out: the caller is
            # falling back to the box and nothing else would ever
            # restore the TV volume this start just set.
            self._stop_sync()
            return False
        _LOGGER.info(
            "tv ring: sounding on the TV (volume saved=%s -> %d%%)",
            self.saved_tv_volume, target,
        )
        return True

    async def start(self, ring_volume: float) -> bool:
        """Begin the ring. False means the TV path cannot sound --
        the Alarm then falls back to the box path."""
        try:
            return await asyncio.to_thread(self._start_sync, ring_volume)
        except Exception:
            _LOGGER.exception("tv ring: start failed")
            return False

    def _stop_sync(self) -> None:
        self._web_sync("POST", "/ring", {"ringing": False})
        if self.saved_tv_volume is not None:
            target = round(self.saved_tv_volume * 100)
            if self._set_tv_percent(target):
                _LOGGER.info("tv ring: TV volume restored to %d%%", target)
            else:
                _LOGGER.error(
                    "tv ring: TV volume restore to %d%% unconfirmed",
                    target,
                )
            self.saved_tv_volume = None

    async def stop(self) -> None:
        try:
            await asyncio.to_thread(self._stop_sync)
        except Exception:
            _LOGGER.exception("tv ring: stop failed")

    async def ack_age(self) -> float | None:
        """Seconds since the web page last confirmed it is sounding
        the ring (None: no ack on record / lyra-web unreachable)."""
        resp = await asyncio.to_thread(self._web_sync, "GET", "/ring")
        if not resp:
            return None
        age = resp.get("ack_age_s")
        return float(age) if isinstance(age, (int, float)) else None

    async def recover(self, saved_tv_volume: float | None) -> None:
        """Core restarted mid-TV-ring: stop any lingering page ring
        and restore the persisted pre-ring TV volume."""
        self.saved_tv_volume = saved_tv_volume
        await self.stop()
        _LOGGER.info(
            "tv ring: interrupted-ring recovery done (volume %s)",
            saved_tv_volume,
        )
