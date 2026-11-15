"""Fedora-native alarm for the Lyra core (homelab plan Phase 3).

Replicates the proven HA alarm behaviour without HA:

- State migrates as-is from the Phase 0 snapshot: 06:00, enabled,
  Repeat=Weekdays. State lives in a JSON file the core owns and is
  reloaded at startup, so the alarm survives core restarts.
- The scheduler ticks like HA's time_pattern automation and fires
  when enabled and the repeat rule admits today. Catch-up rule (plan,
  "The alarm"): if the core STARTS within 30 minutes after the alarm
  time with the slot still armed and unfired, it fires late rather
  than never. A long-running core never fires late.
- Firing: bedroom light ON at 100% via the Shelly Dimmer Gen4's local
  HTTP RPC (readback-verified; the light STAYS ON afterwards — the
  morning routine is alarm -> light -> stop), box volume saved ->
  1.0 -> restored at ring end, the gained 15-minute tone served from
  the core's own HTTP endpoint, and a 15-minute hard stop that ends
  the ring even if nothing kills it.
- TV sounder (2026-11-15): when the composition wires a TvRinger
  (set_tv_ringer), firing sounds through the TV instead -- display
  forced on, the TV woken and its volume saved -> ring -> restored
  over ADB, the web page loops the tone and acks while sounding.
  A watchdog swaps the ring to the box if the TV path fails
  silently (no ack within 45 s, or acks stale for 30 s), and a TV
  start failure falls back to the box path before any ring state
  is entered.
- kill() stops the ring; while not ringing it cancels a pending
  snooze refire if one exists (Daniel's decision, 2026-11-11) and
  otherwise changes nothing and says so (the model owns the
  guard, not just the tools layer). snooze() stops the ring and
  refires in 9 minutes (HA's snooze timer parity).
- Morning note (2026-11-12): a one-shot, date-scoped spoken note
  can be stored on the state. A kill that ends a ring -- or
  cancels a snooze, Daniel is up either way -- on the note's date
  speaks it once, a couple of seconds after the ring ends,
  through the composition's announce path. The note is consumed
  BEFORE it is spoken, so it can never play twice.
- One authenticated HTTP endpoint (GET <kill_url_path>?t=<token>)
  lets Daniel's iOS Action Button kill the alarm; the shortcut gets
  repointed to it. The token is generated once, stored 0600 in the
  core's secrets file, and never logged or printed.

set_alarm()/kill()/snooze() are the clean surface the Phase 3 phrase
matcher will call; voice-set alarms inherit the current repeat mode,
same as they did on HA's script.set_alarm.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import secrets as _secrets
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from aioesphomeapi.model import MediaPlayerState

from .audio_http import AudioHttpServer
from .box_client import BoxClient
from .config import CoreConfig

_LOGGER = logging.getLogger("lyra_core.alarm")

REPEAT_MODES = ("Once", "Daily", "Weekdays", "Weekends")

# Migrated HA state (Phase 0 snapshot, 2026-11-11): 06:00 / on /
# Weekdays. last_fired_slot records that the 2026-11-11 slot already
# fired (under HA, at 06:00:05) so the core can never refire it.
MIGRATED_STATE: dict = {
    "time": "06:00",
    "enabled": True,
    "repeat": "Weekdays",
    "last_fired_slot": "2026-11-11",
    "snooze_until": None,
    "ringing": False,
    "ring_volume": 1.0,
    "saved_volume": None,
}


@dataclass
class AlarmState:
    time: str = "06:00"                    # "HH:MM", local
    enabled: bool = True
    repeat: str = "Weekdays"               # one of REPEAT_MODES
    last_fired_slot: str | None = None     # local date of last fired slot
    snooze_until: float | None = None      # epoch s; refire at/after this
    ringing: bool = False
    ring_volume: float = 1.0               # box volume during a ring
    saved_volume: float | None = None      # pre-ring box volume, persisted
                                           # so the ring-end restore
                                           # survives a core restart
    saved_tv_volume: float | None = None   # pre-ring TV volume, TV
                                           # sounder rings only
    morning_note: str | None = None        # one-shot spoken note, played
                                           # after a morning kill
    morning_note_date: str | None = None   # local ISO date the note
                                           # is meant for

    def to_json(self) -> dict:
        return {
            "time": self.time,
            "enabled": self.enabled,
            "repeat": self.repeat,
            "last_fired_slot": self.last_fired_slot,
            "snooze_until": self.snooze_until,
            "ringing": self.ringing,
            "ring_volume": self.ring_volume,
            "saved_volume": self.saved_volume,
            "saved_tv_volume": self.saved_tv_volume,
            "morning_note": self.morning_note,
            "morning_note_date": self.morning_note_date,
        }

    @classmethod
    def from_json(cls, raw: dict) -> "AlarmState":
        st = cls()
        if isinstance(raw.get("time"), str):
            try:
                _parse_hhmm(raw["time"])
                st.time = raw["time"]
            except ValueError:
                pass
        if isinstance(raw.get("enabled"), bool):
            st.enabled = raw["enabled"]
        if raw.get("repeat") in REPEAT_MODES:
            st.repeat = raw["repeat"]
        if isinstance(raw.get("last_fired_slot"), str):
            st.last_fired_slot = raw["last_fired_slot"]
        if isinstance(raw.get("snooze_until"), (int, float)):
            st.snooze_until = float(raw["snooze_until"])
        if isinstance(raw.get("ringing"), bool):
            st.ringing = raw["ringing"]
        rv = raw.get("ring_volume")
        if isinstance(rv, (int, float)) and not isinstance(rv, bool):
            if 0.0 <= float(rv) <= 1.0:
                st.ring_volume = float(rv)
        sv = raw.get("saved_volume")
        if isinstance(sv, (int, float)) and not isinstance(sv, bool):
            if 0.0 <= float(sv) <= 1.0:
                st.saved_volume = float(sv)
        tv = raw.get("saved_tv_volume")
        if isinstance(tv, (int, float)) and not isinstance(tv, bool):
            if 0.0 <= float(tv) <= 1.0:
                st.saved_tv_volume = float(tv)
        mn = raw.get("morning_note")
        if isinstance(mn, str) and mn.strip():
            st.morning_note = mn
        md = raw.get("morning_note_date")
        if isinstance(md, str):
            try:
                st.morning_note_date = date.fromisoformat(md).isoformat()
            except ValueError:
                pass
        return st


def _parse_hhmm(text: str) -> dtime:
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"not HH:MM: {text!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"not HH:MM: {text!r}")
    return dtime(hh, mm)


def repeat_admits(repeat: str, day: date) -> bool:
    if repeat in ("Once", "Daily"):
        return True
    if repeat == "Weekdays":
        return day.weekday() < 5
    if repeat == "Weekends":
        return day.weekday() >= 5
    return False


def _local_slot(day: date, t: dtime, tz: ZoneInfo) -> datetime:
    """The instant an alarm set for local time `t` on `day` fires.

    DST policy (explicit; dial-in 2026-11-11, item L):
    - fall-back overlap (the wall time occurs twice): fire at the
      FIRST occurrence (fold=0);
    - spring-forward gap (the wall time never occurs): fire at the
      first valid instant after the gap -- the transition instant
      itself, whose wall time is the gap's end (02:30 on 2026-03-08
      in America/New_York fires at 03:00 EDT).
    """
    naive = datetime.combine(day, t)
    candidate = naive.replace(tzinfo=tz, fold=0)
    # Round-trip through UTC: an existing wall time (including the
    # first of an ambiguous pair) comes back unchanged; a gap time
    # comes back shifted by the gap.
    round_tripped = candidate.astimezone(timezone.utc).astimezone(tz)
    if round_tripped.replace(tzinfo=None) == naive:
        return candidate
    # Gap: fold=1 reads the wall time with the post-gap offset and
    # lands before the transition; fold=0 lands after it. Bisect UTC
    # for the first instant carrying the post-gap offset -- that
    # instant is the transition itself, the gap's end.
    post_offset = naive.replace(tzinfo=tz, fold=1).utcoffset()
    lo = naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    hi = candidate.astimezone(timezone.utc)
    while (hi - lo).total_seconds() > 1:
        mid = lo + (hi - lo) / 2
        if mid.astimezone(tz).utcoffset() == post_offset:
            hi = mid
        else:
            lo = mid
    return hi.astimezone(tz)


def decide_fire(
    now: datetime,
    state: AlarmState,
    started_at: datetime | None,
    tz: ZoneInfo,
    catchup_window_s: float,
) -> str | None:
    """Pure fire decision: "fire" (in the alarm minute), "catchup"
    (core started late inside the catch-up window), or None.

    Catch-up requires the core to have STARTED after the slot: a
    long-running core that somehow missed the minute never fires
    late on its own."""
    if not state.enabled or state.ringing or state.snooze_until is not None:
        return None
    slot = _local_slot(now.date(), _parse_hhmm(state.time), tz)
    if not repeat_admits(state.repeat, slot.date()):
        return None
    if state.last_fired_slot == slot.date().isoformat():
        return None
    if slot <= now < slot + timedelta(seconds=60):
        return "fire"
    if (
        started_at is not None
        and started_at >= slot
        and now < slot + timedelta(seconds=catchup_window_s)
    ):
        return "catchup"
    return None


def next_fire_after(
    now: datetime, state: AlarmState, tz: ZoneInfo
) -> datetime | None:
    """Next scheduled firing (snooze refire wins if one is pending)."""
    if state.snooze_until is not None:
        refire = datetime.fromtimestamp(state.snooze_until, tz)
        if refire > now:
            return refire
    if not state.enabled:
        return None
    t = _parse_hhmm(state.time)
    for offset in range(9):
        day = now.date() + timedelta(days=offset)
        candidate = _local_slot(day, t, tz)
        if candidate <= now:
            continue
        if not repeat_admits(state.repeat, day):
            continue
        if state.last_fired_slot == day.isoformat():
            continue
        return candidate
    return None


class ShellyLight:
    """Shelly Gen4 local RPC over HTTP (stdlib urllib, run in a thread
    so the event loop never blocks on the light)."""

    def __init__(self, host: str, light_id: int = 0) -> None:
        self._base = f"http://{host}/rpc"
        self._id = light_id

    def _rpc_sync(self, method: str, params: dict) -> dict:
        # Shelly wants lowercase JSON booleans in the query string;
        # urlencode would render Python's True/False verbatim and the
        # device silently ignores the call.
        def _enc(value):
            if isinstance(value, bool):
                return "true" if value else "false"
            return value

        qs = urllib.parse.urlencode(
            {k: _enc(v) for k, v in params.items()}
        )
        with urllib.request.urlopen(
            f"{self._base}/{method}?{qs}", timeout=5
        ) as resp:
            payload = json.loads(resp.read())
        # GET-style RPC answers with the bare result (Light.Set even
        # answers null); a POSTed JSON-RPC frame would wrap it in
        # {"result": ...} and report failures as {"error": ...}.
        if payload is None:
            return {}
        if isinstance(payload, dict) and "error" in payload:
            raise RuntimeError(f"Shelly {method}: {payload['error']}")
        if isinstance(payload, dict) and "result" in payload:
            result = payload["result"]
            return result if isinstance(result, dict) else {}
        return payload if isinstance(payload, dict) else {}

    async def _rpc(self, method: str, params: dict) -> dict:
        return await asyncio.to_thread(self._rpc_sync, method, params)

    async def get_status(self) -> dict:
        return await self._rpc("Light.GetStatus", {"id": self._id})

    async def set_on(self, brightness: int) -> dict:
        """Turn on at brightness %; returns the readback status."""
        await self._rpc(
            "Light.Set", {"id": self._id, "on": True, "brightness": brightness}
        )
        return await self.get_status()

    async def set_off(self) -> dict:
        await self._rpc("Light.Set", {"id": self._id, "on": False})
        return await self.get_status()


BoxGetter = Callable[[], BoxClient | None]


class AlarmButtonFlasher:
    """Flashes the alarm button's LED bar while a ring sounds.

    The button is a Shelly Plus Wall Dimmer with nothing wired
    to its output (Daniel, 2026-11-12: "nothing is attached to
    the dimmer I just want an alarm button"). Its light.on /
    light.off webhooks call the core's kill endpoint, and its
    LED bar tracks the load-less output's brightness. At rest
    the button is MOMENTARY (Daniel's correction, 2026-11-12:
    "it's staying in the on position and not acting like a
    momentary button"): the device runs auto_off at 1 s, so a
    tap turns the output on -- firing the kill -- and it drops
    back off by itself instead of latching on. start() lifts
    the auto-off for the ring's duration so the bar can stay
    lit to flash; stop() restores it. One device truth no
    setting changes on this firmware: with the output off the
    bar keeps a single dot lit marking the last brightness
    level (documented in the manual; wd_ui has no switch for
    it), so parking zeroes the brightness first to leave that
    dot at the very bottom of the strip.

    The flash is BRIGHTNESS-ONLY (100 <-> 8 on a live output),
    because the device was measured to fire its webhooks on
    every on/off transition regardless of source (physical tap,
    RPC set, auto-off timer all count) but never on a
    brightness-only set -- a transition-based flash would kill
    the ring it announces. Every transition the flasher itself
    makes is wrapped in a webhook disable/enable dance (the
    disabled window is one LAN round trip) so it cannot
    self-kill a ring or cancel a pending snooze either.

    Failure policy: everything is logged and swallowed. The
    flasher is decoration on the ring; it must never break one.
    """

    FLASH_HIGH = 100
    FLASH_LOW = 8
    PULSE_S = 0.9
    MOMENTARY_S = 1.0

    def __init__(self, host: str, kill_url_path: str, light_id: int = 0) -> None:
        self._base = f"http://{host}/rpc"
        self._light_id = light_id
        self._kill_marker = kill_url_path
        self._hook_ids: list[int] = []
        self._task: asyncio.Task | None = None
        self._engaged = False

    # -- transport: POST JSON (GET-style RPC mangles booleans) ---

    def _rpc_sync(self, method: str, params: dict) -> dict:
        body = json.dumps(
            {"id": 1, "method": method, "params": params}
        ).encode()
        req = urllib.request.Request(
            self._base, data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode())
        if "error" in payload:
            raise RuntimeError(f"{method}: {payload['error']}")
        return payload.get("result") or {}

    async def _rpc(self, method: str, params: dict) -> dict:
        return await asyncio.to_thread(self._rpc_sync, method, params)

    # -- webhook helpers -------------------------------------------------

    async def _kill_hook_ids(self) -> list[int]:
        result = await self._rpc("Webhook.List", {})
        ids = []
        for hook in result.get("hooks", []):
            urls = hook.get("urls") or []
            if any(self._kill_marker in u for u in urls):
                ids.append(hook["id"])
        return ids

    async def _set_hooks(self, ids: list[int], enable: bool) -> None:
        for hid in ids:
            await self._rpc("Webhook.Update", {"id": hid, "enable": enable})

    async def _set_auto_off(self, enabled: bool) -> None:
        await self._rpc(
            "Light.SetConfig",
            {"id": self._light_id,
             "config": {"auto_off": enabled,
                        "auto_off_delay": self.MOMENTARY_S}},
        )

    async def _set_output(
        self, ids: list[int], *, on: bool, brightness: int | None = None
    ) -> None:
        """Set the output with the kill hooks disabled for
        exactly that transition and back on before returning --
        the flasher's own transitions must never fire a kill
        (into a ring it announces, or into a fresh snooze). If
        no kill hooks exist there is nothing to protect against
        and the set is direct."""
        params: dict = {"id": self._light_id, "on": on}
        if on and brightness is not None:
            params["brightness"] = brightness
        if ids:
            await self._set_hooks(ids, False)
        try:
            await self._rpc("Light.Set", params)
        finally:
            if ids:
                await self._set_hooks(ids, True)

    # -- lifecycle ---------------------------------------------------------

    async def reconcile(self) -> None:
        """Idle-state heal, run once at core start: kill hooks
        enabled, momentary auto-off in force, output off. Heals
        any state a mid-ring core death could have left on the
        device."""
        try:
            ids = await self._kill_hook_ids()
            await self._set_output(ids, on=False)
            await self._set_auto_off(True)
            _LOGGER.info(
                "alarm button: reconciled (kill hooks %s live, "
                "LED bar dark at rest)", ids,
            )
        except Exception as exc:
            _LOGGER.warning("alarm button reconcile failed: %s", exc)

    async def start(self) -> None:
        """Ring start: output on at full brightness (dance-
        protected), then pulse. The hooks are live again before
        this returns, so a tap at any point in the ring kills
        it exactly as it does at rest."""
        if self._engaged:
            return
        ids: list[int] = []
        try:
            ids = await self._kill_hook_ids()
            # The ring needs the output to STAY on for minutes:
            # lift the button's momentary auto-off for the ring's
            # duration (stop() puts it back).
            await self._set_auto_off(False)
            await self._set_output(ids, on=True, brightness=self.FLASH_HIGH)
        except Exception as exc:
            _LOGGER.error("alarm button flash start failed: %s", exc)
            try:
                await self._set_hooks(ids, True)
            except Exception:
                pass
            try:
                await self._set_auto_off(True)
            except Exception:
                pass
            return
        self._hook_ids = ids
        self._engaged = True
        self._task = asyncio.create_task(
            self._pulse(), name="lyra-alarm-flasher"
        )
        _LOGGER.info("alarm button: LED flash started")

    async def _pulse(self) -> None:
        low = False
        failed = False
        while True:
            await asyncio.sleep(self.PULSE_S)
            low = not low
            try:
                await self._rpc(
                    "Light.Set",
                    {"id": self._light_id,
                     "brightness": self.FLASH_LOW if low else self.FLASH_HIGH},
                )
            except Exception as exc:
                if not failed:
                    _LOGGER.warning(
                        "alarm button flash pulse failing: %s", exc
                    )
                    failed = True

    async def stop(self) -> None:
        """Ring end: stop pulsing and park the bar dark
        (dance-protected like every flasher set: parking must
        never fire a kill into a fresh snooze)."""
        if not self._engaged:
            return
        self._engaged = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        ids = self._hook_ids
        try:
            if not ids:
                ids = await self._kill_hook_ids()
            # Zero the level before the off: the bar's off-state
            # dot marks the last brightness, bottom is quietest.
            try:
                await self._rpc(
                    "Light.Set", {"id": self._light_id, "brightness": 0}
                )
            except Exception:
                pass
            await self._set_output(ids, on=False)
        except Exception as exc:
            _LOGGER.warning("alarm button park failed: %s", exc)
        try:
            await self._set_auto_off(True)
        except Exception as exc:
            _LOGGER.error(
                "alarm button: momentary auto-off restore failed: %s", exc
            )
        self._hook_ids = []
        _LOGGER.info("alarm button: LED flash stopped, bar dark")

    def halt(self) -> None:
        """Process-stop path: cancel the pulse without RPCs.
        Whatever state the device was left in, the next core
        start's reconcile() heals it."""
        if self._task is not None:
            self._task.cancel()
            self._task = None
        self._engaged = False


class Alarm:
    """Scheduler + firing engine + kill endpoint. One instance lives
    in the production composition for the life of the process."""

    def __init__(
        self,
        cfg: CoreConfig,
        *,
        box_getter: BoxGetter,
        http: AudioHttpServer,
    ) -> None:
        self._full_cfg = cfg
        self._cfg = cfg.alarm
        self._box_getter = box_getter
        self._http = http
        self._tz = ZoneInfo(self._cfg.timezone)
        self._state_path = Path(self._cfg.state_path).expanduser()
        self._secrets_path = Path(self._cfg.secrets_path).expanduser()
        self._state = self._load_state()
        self._token = self._load_or_create_token()
        self._light = ShellyLight(self._cfg.light_host, self._cfg.light_id)
        # Alarm button LED flasher (2026-11-12): pure config, so
        # it is built here like the light; an empty button_host
        # disables it. See AlarmButtonFlasher.
        self._flasher: AlarmButtonFlasher | None = (
            AlarmButtonFlasher(self._cfg.button_host, self._cfg.kill_url_path)
            if self._cfg.button_host
            else None
        )
        self._tone_bytes: bytes | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._task: asyncio.Task | None = None
        self._hard_stop_task: asyncio.Task | None = None
        self._started_at: datetime | None = None
        self._saved_volume: float | None = None
        self._saw_playing = False
        self._ring_box_lost = False
        self._recover_pending = False
        # TV sounder (2026-11-15): wired by the composition via
        # set_tv_ringer; None keeps the historical box-only rings.
        self._ringer = None
        self._ring_via_tv = False
        self._tv_ack_seen = False
        self._ring_watchdog_task: asyncio.Task | None = None
        self._tv_ring_handled = False
        # Morning note: the composition wires its announce path in
        # as the speaker (set_note_speaker); delivery runs as a
        # background task so kill() never waits on speech.
        self._note_speaker: Callable | None = None
        self._note_task: asyncio.Task | None = None
        # Grace period fire() allows for a box (re)connect before
        # declaring a miss (item I); benches shrink it.
        self._box_wait_s = 30.0

    # -- state + secrets ------------------------------------------------------

    def _load_state(self) -> AlarmState:
        if self._state_path.exists():
            try:
                raw = json.loads(self._state_path.read_text())
                if isinstance(raw, dict):
                    return AlarmState.from_json(raw)
            except Exception as exc:
                _LOGGER.error("alarm state unreadable (%s); using migrated", exc)
        state = AlarmState.from_json(dict(MIGRATED_STATE))
        self._state = state
        self._save_state()
        _LOGGER.info("alarm state initialised from migrated HA state")
        return state

    def _save_state(self) -> None:
        tmp = self._state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._state.to_json(), indent=2) + "\n")
        os.replace(tmp, self._state_path)

    def _load_or_create_token(self) -> str:
        """The kill-endpoint token. Stored 0600; never logged."""
        token = None
        if self._secrets_path.exists():
            try:
                raw = json.loads(self._secrets_path.read_text())
                if isinstance(raw.get("kill_token"), str) and raw["kill_token"]:
                    token = raw["kill_token"]
            except Exception:
                token = None
        if token is None:
            token = _secrets.token_urlsafe(32)
            fd = os.open(
                self._secrets_path,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                0o600,
            )
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps({"kill_token": token}, indent=2) + "\n")
            _LOGGER.info("alarm kill token generated (stored 0600, not logged)")
        try:
            os.chmod(self._secrets_path, 0o600)
        except OSError:
            pass
        return token

    # -- introspection for the matcher / logs ---------------------------------

    def http_auth_token(self) -> str:
        """The shared HTTP endpoint token (kill endpoint + Phase 4 API).

        The value is never logged; this accessor exists so the
        production composition can hand the SAME token to the core
        API instead of growing a second secret.
        """
        return self._token

    def _now(self) -> datetime:
        return datetime.now(self._tz)

    def tone_url(self) -> str:
        http = self._full_cfg.http
        return f"http://{http.public_host}:{http.port}{self._cfg.tone_url_path}"

    def next_fire(self) -> datetime | None:
        return next_fire_after(self._now(), self._state, self._tz)

    def summary(self) -> dict:
        nf = self.next_fire()
        return {
            "time": self._state.time,
            "enabled": self._state.enabled,
            "repeat": self._state.repeat,
            "ringing": self._state.ringing,
            "ring_volume": self._state.ring_volume,
            "snooze_until": self._state.snooze_until,
            "morning_note": self._state.morning_note,
            "morning_note_date": self._state.morning_note_date,
            "next_fire": nf.isoformat() if nf else None,
        }

    # -- lifecycle -------------------------------------------------------------

    def validate_assets(self) -> bool:
        """Load the alarm tone; NEVER raises (dial-in 2026-11-11,
        item F). A missing/unreadable tone degrades the ALARM only:
        the failure is logged loudly, the tone route answers 503
        while no tone is loaded, and a ring becomes light-only --
        the core's voice service must never crash-loop over an
        audio file. The production composition calls this BEFORE
        starting background services; start() retries it if the
        tone is still unloaded. Returns True when the tone is in
        memory."""
        tone_path = Path(self._cfg.tone_path).expanduser()
        try:
            self._tone_bytes = tone_path.read_bytes()
        except OSError as exc:
            self._tone_bytes = None
            _LOGGER.error(
                "alarm tone UNREADABLE at %s (%s); alarm DEGRADED: "
                "rings will turn the light on but cannot play a tone",
                tone_path, exc,
            )
            return False
        return True

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._started_at = self._now()
        if self._state.ringing:
            # The box-side playback is not tracked across a core
            # restart; a persisted ringing flag means the previous
            # process died mid-ring. Clear it rather than resurrect,
            # and queue the interrupted-ring recovery (item G): on
            # the next box connect, recovery stops any lingering
            # playback and restores the persisted pre-ring volume.
            _LOGGER.warning(
                "alarm state said ringing at startup; clearing "
                "(a ring does not survive a core restart) and "
                "queueing interrupted-ring recovery"
            )
            self._state.ringing = False
            self._save_state()
            self._recover_pending = True
        if self._ringer is not None and self._state.saved_tv_volume is not None:
            # A TV ring was interrupted by the restart: stop any
            # lingering page ring and restore the TV volume. (The
            # box-side recovery also runs; it is a no-op for a TV
            # ring because saved_volume was never set.)
            saved_tv = self._state.saved_tv_volume
            self._state.saved_tv_volume = None
            self._save_state()
            asyncio.create_task(
                self._ringer.recover(saved_tv), name="lyra-alarm-tv-recover"
            )
        if self._tone_bytes is None:
            self.validate_assets()
        self._http.add_route(self._cfg.tone_url_path, self._serve_tone)
        self._http.add_route(self._cfg.kill_url_path, self._serve_kill)
        self._task = asyncio.create_task(self._run(), name="lyra-alarm")
        if self._flasher is not None:
            asyncio.create_task(
                self._flasher.reconcile(), name="lyra-alarm-flasher-reconcile"
            )
        _LOGGER.info(
            "alarm armed: time=%s enabled=%s repeat=%s next_fire=%s "
            "(tone %s at %s)",
            self._state.time,
            self._state.enabled,
            self._state.repeat,
            self.next_fire(),
            (
                f"{len(self._tone_bytes)} bytes"
                if self._tone_bytes is not None
                else "MISSING -- alarm degraded"
            ),
            self.tone_url(),
        )

    async def stop(self) -> None:
        for task in (self._task, self._hard_stop_task, self._note_task,
                     self._ring_watchdog_task):
            if task is not None:
                task.cancel()
        self._task = None
        self._hard_stop_task = None
        self._note_task = None
        self._ring_watchdog_task = None
        if self._flasher is not None:
            self._flasher.halt()

    # -- scheduler --------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("alarm tick failed")
            await asyncio.sleep(self._cfg.tick_s)

    async def _tick(self) -> None:
        now = self._now()
        st = self._state
        if st.ringing:
            if self._ring_via_tv:
                # The TV watchdog minds this ring; the box is not
                # involved and its state means nothing here.
                return
            # Ring-state recovery (item G): if the box drops mid-ring
            # the ring cannot be watched; when the box comes back,
            # conclude the ring cleanly (stop + restore the saved
            # volume) instead of letting it hang until the hard stop.
            box = self._box_getter()
            connected = box is not None and box.is_connected
            if connected and self._ring_box_lost:
                self._ring_box_lost = False
                await self._end_ring("box_reconnected")
                return
            if not connected:
                if not self._ring_box_lost:
                    self._ring_box_lost = True
                    _LOGGER.warning(
                        "alarm: box disconnected mid-ring; the ring "
                        "will be concluded (volume restored) when "
                        "the box reconnects, or by the hard stop"
                    )
                return
            self._watch_playback()
            return
        if self._recover_pending:
            self._run_recovery()
        if st.snooze_until is not None:
            if now.timestamp() >= st.snooze_until:
                overdue = now.timestamp() - st.snooze_until
                st.snooze_until = None
                self._save_state()
                if overdue <= self._cfg.catchup_window_s:
                    await self.fire(reason="snooze")
                else:
                    _LOGGER.warning(
                        "snooze refire dropped (overdue %.0fs)", overdue
                    )
            return
        action = decide_fire(
            now, st, self._started_at, self._tz, self._cfg.catchup_window_s
        )
        if action:
            await self.fire(reason=action)

    def _run_recovery(self) -> None:
        """Interrupted-ring recovery (item G): runs on a tick once
        the box is reachable again after a ring was cut short (core
        restart mid-ring, or a ring that ended while the box was
        gone). Stops any lingering tone playback and restores the
        persisted pre-ring volume. One-shot: clears itself once the
        box is reachable, whether or not a volume was saved."""
        box = self._box_getter()
        if box is None or not box.is_connected:
            return
        self._recover_pending = False
        try:
            box.media_stop()
        except Exception as exc:
            _LOGGER.error("alarm recovery: box stop failed: %s", exc)
        saved = self._state.saved_volume
        if saved is not None:
            try:
                box.media_set_volume(saved)
            except Exception as exc:
                # Keep the saved value and re-queue: discarding it
                # here would strand the box at ring volume with no
                # retry (Codex recheck 2026-11-11).
                _LOGGER.error(
                    "alarm recovery: volume restore to %s failed "
                    "(%s); saved value kept, recovery re-queued",
                    saved, exc,
                )
                self._recover_pending = True
                return
            self._state.saved_volume = None
            self._save_state()
        _LOGGER.info(
            "alarm: interrupted-ring recovery done (playback "
            "stopped, volume restored to %s)",
            saved,
        )

    def _watch_playback(self) -> None:
        """If the tone finished on its own before the hard stop, end
        the ring (restore volume) instead of waiting out the timer."""
        box = self._box_getter()
        if box is None or not box.is_connected:
            return
        ms = box.media_state
        if ms == MediaPlayerState.PLAYING:
            self._saw_playing = True
        elif self._saw_playing and ms in (
            MediaPlayerState.IDLE,
            MediaPlayerState.OFF,
            MediaPlayerState.NONE,
        ):
            self._saw_playing = False
            asyncio.get_running_loop().create_task(
                self._end_ring("playback_ended")
            )

    # -- firing -------------------------------------------------------------------

    async def fire(self, *, reason: str) -> bool:
        st = self._state
        if st.ringing:
            return False
        now = self._now()
        self._recover_pending = False  # a fresh ring supersedes recovery
        self._ring_box_lost = False
        # (a) Light ON at 100%, readback-verified. It stays on after
        # the ring — the morning routine is alarm -> light -> stop.
        try:
            status = await self._light.set_on(100)
            if status.get("output") is True and int(status.get("brightness", -1)) == 100:
                _LOGGER.info("alarm: light ON at 100% (readback verified)")
            else:
                _LOGGER.error("alarm: light readback mismatch: %r", status)
        except Exception as exc:
            _LOGGER.error("alarm: light RPC failed (%s); ring continues", exc)
        # (b-tv) TV sounder (2026-11-15): with a ringer wired, the
        # ring sounds through the TV and the box is not required.
        # A start failure falls through to the box path below --
        # before any ring state is entered, exactly like a box miss.
        if self._ringer is not None:
            if await self._fire_tv(reason=reason):
                return True
            _LOGGER.error(
                "alarm: TV ring could not start; falling back to "
                "the speaker box"
            )
        # (b) The box must be reachable BEFORE the ring state is
        # entered (item I): the persisted state must never claim a
        # ring that cannot sound. A bounded wait covers the catch-up
        # race (a core restart lands the fire seconds before the
        # supervisor's box reconnect finishes). A truly absent box
        # means a MISSED alarm: the slot is consumed exactly as a
        # fired one (no all-morning retries), the miss is logged
        # loudly, ringing is never set -- and the light from step
        # (a) still came on, which is half the wake-up.
        box = await self._wait_for_box()
        if box is None:
            if reason != "snooze":
                slot = _local_slot(now.date(), _parse_hhmm(st.time), self._tz)
                st.last_fired_slot = slot.date().isoformat()
            if st.repeat == "Once":
                st.enabled = False
            st.snooze_until = None
            self._save_state()
            _LOGGER.error(
                "alarm MISSED (reason=%s): box not connected; the "
                "light is on but no tone can play and no ring state "
                "was entered",
                reason,
            )
            return False
        # (c) Enter the ring and persist it, THEN hand off the tone:
        # volume saved (in memory AND in the persisted state, so a
        # mid-ring restart can still restore it -- item G) -> set
        # the ring volume -> play the tone from our endpoint.
        st.ringing = True
        if reason != "snooze":
            slot = _local_slot(now.date(), _parse_hhmm(st.time), self._tz)
            st.last_fired_slot = slot.date().isoformat()
        st.snooze_until = None
        if st.repeat == "Once":
            st.enabled = False
        self._saved_volume = box.media_volume
        if isinstance(self._saved_volume, (int, float)):
            st.saved_volume = float(self._saved_volume)
        self._save_state()
        self._saw_playing = False
        _LOGGER.info(
            "alarm FIRING (reason=%s) time=%s repeat=%s", reason, st.time, st.repeat
        )
        if self._tone_bytes is None:
            _LOGGER.error(
                "alarm: tone asset missing; this ring is light-only "
                "(see validate_assets)"
            )
        _LOGGER.info(
            "alarm: box volume saved=%s -> ring volume %s",
            self._saved_volume, st.ring_volume,
        )
        try:
            box.media_set_volume(st.ring_volume)
            box.media_play(self.tone_url())
        except Exception as exc:
            _LOGGER.error("alarm: box playback start failed: %s", exc)
        # (d) 15-minute hard stop.
        self._hard_stop_task = asyncio.create_task(
            self._hard_stop(), name="lyra-alarm-hardstop"
        )
        # (e) Alarm button LED flash (2026-11-12). Brightness-only
        # pulsing; the flasher swallows its own failures, so this
        # can never break the ring it decorates.
        if self._flasher is not None:
            await self._flasher.start()
        return True

    async def _fire_tv(self, *, reason: str) -> bool:
        """Fire the ring through the TV sounder. The ringer starts
        BEFORE the ring state is entered (its readiness checks are
        the TV path's version of the box wait): on False nothing was
        persisted and the caller falls back to the box."""
        st = self._state
        if not await self._ringer.start(st.ring_volume):
            return False
        now = self._now()
        st.ringing = True
        if reason != "snooze":
            slot = _local_slot(now.date(), _parse_hhmm(st.time), self._tz)
            st.last_fired_slot = slot.date().isoformat()
        st.snooze_until = None
        if st.repeat == "Once":
            st.enabled = False
        st.saved_tv_volume = self._ringer.saved_tv_volume
        self._save_state()
        self._ring_via_tv = True
        self._tv_ack_seen = False
        _LOGGER.info(
            "alarm FIRING via TV (reason=%s) time=%s repeat=%s; "
            "TV volume saved=%s -> ring volume %s",
            reason, st.time, st.repeat, st.saved_tv_volume, st.ring_volume,
        )
        # 15-minute hard stop, same as the box path.
        self._hard_stop_task = asyncio.create_task(
            self._hard_stop(), name="lyra-alarm-hardstop"
        )
        self._ring_watchdog_task = asyncio.create_task(
            self._ring_watchdog(), name="lyra-alarm-tv-watchdog"
        )
        if self._flasher is not None:
            await self._flasher.start()
        return True

    async def _ring_watchdog(self) -> None:
        """Mind a TV ring: the page must keep acking that it is
        sounding. No ack within 45 s of the fire -- or acks stale
        for 30 s mid-ring -- means the TV path failed silently:
        swap the ring to the box so the alarm still sounds."""
        loop = asyncio.get_running_loop()
        last_fresh = loop.time()
        while self._state.ringing and self._ring_via_tv:
            await asyncio.sleep(5.0)
            if not (self._state.ringing and self._ring_via_tv):
                return
            age = await self._ringer.ack_age()
            if age is not None and age < 10.0:
                self._tv_ack_seen = True
                last_fresh = loop.time()
                continue
            limit = 30.0 if self._tv_ack_seen else 45.0
            if loop.time() - last_fresh >= limit:
                _LOGGER.error(
                    "alarm: TV ring is not sounding (last ack %s, "
                    "ack_seen=%s); swapping the ring to the box",
                    f"{age:.0f}s ago" if age is not None else "never",
                    self._tv_ack_seen,
                )
                await self._swap_ring_to_box()
                return

    async def _swap_ring_to_box(self) -> None:
        """Mid-ring fallback: stop the TV ring and sound the rest
        of the ring on the box (the pre-2026-11-15 path)."""
        self._ring_via_tv = False
        if self._ringer is not None:
            await self._ringer.stop()
        if self._state.saved_tv_volume is not None:
            self._state.saved_tv_volume = None
            self._save_state()
        box = await self._wait_for_box()
        if box is None:
            _LOGGER.error(
                "alarm: TV ring failed AND the box is unreachable; "
                "the ring continues light-only until the hard stop"
            )
            return
        self._saved_volume = box.media_volume
        if isinstance(self._saved_volume, (int, float)):
            self._state.saved_volume = float(self._saved_volume)
            self._save_state()
        self._saw_playing = False
        _LOGGER.info(
            "alarm: ring swapped to the box (volume saved=%s -> %s)",
            self._saved_volume, self._state.ring_volume,
        )
        try:
            box.media_set_volume(self._state.ring_volume)
            box.media_play(self.tone_url())
        except Exception as exc:
            _LOGGER.error("alarm: box fallback playback failed: %s", exc)

    async def _stop_tv_ring(self) -> None:
        """End a TV ring: cancel the watchdog, stop the page ring
        and restore the TV volume. Flags _stop_playback that the
        box side has nothing to do."""
        task = self._ring_watchdog_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._ring_watchdog_task = None
        self._ring_via_tv = False
        self._tv_ring_handled = True
        if self._ringer is not None:
            await self._ringer.stop()
        if self._state.saved_tv_volume is not None:
            self._state.saved_tv_volume = None
            self._save_state()
        _LOGGER.info("alarm: TV ring stopped; TV volume restored")

    async def _wait_for_box(self) -> BoxClient | None:
        """Poll the box getter for up to self._box_wait_s: long
        enough for a supervisor reconnect racing a catch-up fire to
        land, short enough that a genuinely absent box misses
        honestly instead of hanging the scheduler."""
        deadline = asyncio.get_running_loop().time() + self._box_wait_s
        while True:
            box = self._box_getter()
            if box is not None and box.is_connected:
                return box
            if asyncio.get_running_loop().time() >= deadline:
                return None
            await asyncio.sleep(1.0)

    async def _hard_stop(self) -> None:
        await asyncio.sleep(self._cfg.hard_stop_s)
        await self._end_ring("hard_stop")

    def _stop_playback(self) -> None:
        """Stop the tone and restore the saved volume (light untouched).

        If the box is unreachable the restore cannot happen now:
        the persisted saved volume stays in place and recovery is
        queued, so the next box connect restores it (item G) --
        the box holds its volume in NVS across a disconnect, so
        leaving it at ring volume would strand it loud."""
        if self._tv_ring_handled:
            # A TV ring was already stopped by _stop_tv_ring; the
            # box never played and its volume was never touched.
            self._tv_ring_handled = False
            self._saved_volume = None
            return
        box = self._box_getter()
        if box is not None and box.is_connected:
            try:
                box.media_stop()
            except Exception as exc:
                _LOGGER.error("alarm: box stop failed: %s", exc)
            restored = self._saved_volume is None
            if self._saved_volume is not None:
                try:
                    box.media_set_volume(self._saved_volume)
                    restored = True
                except Exception as exc:
                    _LOGGER.error("alarm: volume restore failed: %s", exc)
            if restored:
                if self._state.saved_volume is not None:
                    self._state.saved_volume = None
                    self._save_state()
                _LOGGER.info(
                    "alarm: playback stopped; volume restored to %s",
                    self._saved_volume,
                )
            else:
                # Keep the persisted value and queue recovery: a
                # failed restore must not strand the box at ring
                # volume with no retry (Codex recheck 2026-11-11).
                self._recover_pending = True
                _LOGGER.warning(
                    "alarm: volume restore to %s failed; deferred "
                    "to interrupted-ring recovery",
                    self._saved_volume,
                )
        elif self._saved_volume is not None:
            self._recover_pending = True
            _LOGGER.warning(
                "alarm: box unreachable at ring end; volume restore "
                "to %s deferred until the box reconnects",
                self._saved_volume,
            )
        self._saved_volume = None

    async def _end_ring(self, reason: str) -> None:
        st = self._state
        if not st.ringing:
            return
        st.ringing = False
        self._ring_box_lost = False
        self._save_state()
        task = self._hard_stop_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        self._hard_stop_task = None
        if self._flasher is not None:
            await self._flasher.stop()
        if self._ring_via_tv:
            await self._stop_tv_ring()
        self._stop_playback()
        _LOGGER.info("alarm ring ended (%s); light stays on", reason)

    # -- the matcher's surface -----------------------------------------------------

    async def kill(self) -> dict:
        """Stop the ring -- or cancel a pending snooze refire.

        Three cases, decided HERE in the model (dial-in 2026-11-11
        item H put the guard in the model; Daniel's amendment the
        same evening restored the snooze-cancelling case on
        purpose -- a kill press during the snooze window is the
        user changing their mind, not a stray press):

        - Ringing: end the ring (stop playback, restore volume).
        - Not ringing, snooze pending: cancel the refire. Clearing
          snooze_until is final -- the scheduler refires off that
          field alone -- and is persisted before returning.
        - Neither: change NOTHING and return the honest
          not-ringing result."""
        st = self._state
        if st.ringing:
            await self._end_ring("kill")
            _LOGGER.info("alarm kill: ring stopped")
            note = self._deliver_morning_note()
            return {
                "ok": True, "was_ringing": True,
                "snooze_cleared": False, "morning_note": note,
            }
        if st.snooze_until is not None:
            st.snooze_until = None
            self._save_state()
            _LOGGER.info("alarm kill: pending snooze refire cancelled")
            note = self._deliver_morning_note()
            return {
                "ok": True, "was_ringing": False,
                "snooze_cleared": True, "morning_note": note,
            }
        _LOGGER.info("alarm kill: not ringing, no snooze pending; "
                     "nothing changed")
        return {
            "ok": False,
            "reason": "not ringing",
            "was_ringing": False,
            "snooze_cleared": False,
        }

    # -- morning note -------------------------------------------------------

    def set_tv_ringer(self, ringer) -> None:
        """Wire the TV sounder (composition, 2026-11-15). With a
        ringer set, rings sound through the TV and the box path is
        the fallback; without one, rings are box-only (benches)."""
        self._ringer = ringer

    def set_note_speaker(self, speaker) -> None:
        """Wire the composition's speak-a-line path (its announce
        composition: text in, speech out of the box)."""
        self._note_speaker = speaker

    def morning_note(self) -> dict:
        return {
            "morning_note": self._state.morning_note,
            "morning_note_date": self._state.morning_note_date,
        }

    def set_morning_note(self, text: str, for_date: str) -> dict:
        text = " ".join(str(text).split())
        if not text:
            raise ValueError("morning note text is empty")
        if len(text) > 300:
            raise ValueError("morning note too long (300 chars max)")
        day = date.fromisoformat(str(for_date))  # ValueError if not ISO
        self._state.morning_note = text
        self._state.morning_note_date = day.isoformat()
        self._save_state()
        _LOGGER.info("alarm morning note set for %s", day.isoformat())
        return self.morning_note()

    def clear_morning_note(self) -> dict:
        self._state.morning_note = None
        self._state.morning_note_date = None
        self._save_state()
        _LOGGER.info("alarm morning note cleared")
        return self.morning_note()

    def _deliver_morning_note(self) -> str | None:
        """Called from kill() once Daniel is up (ring ended or
        snooze cancelled). Schedules today's note to be spoken
        once, in the background, and returns its text; returns
        None when there is nothing to say. A note dated in the
        past is cleared unsaid; a future note is left in place."""
        st = self._state
        note, ndate = st.morning_note, st.morning_note_date
        if not note:
            return None
        today = self._now().date().isoformat()
        if ndate is not None and ndate < today:
            _LOGGER.info(
                "alarm morning note for %s expired unsaid; cleared", ndate
            )
            self.clear_morning_note()
            return None
        if ndate != today:
            return None
        # Consume BEFORE speaking: the note can never play twice,
        # even if the core restarts mid-delivery.
        self.clear_morning_note()
        if self._note_speaker is None:
            _LOGGER.warning(
                "alarm morning note had no speaker wired; dropped: %r", note
            )
            return None
        self._note_task = asyncio.create_task(
            self._speak_morning_note(note), name="lyra-alarm-note"
        )
        return note

    async def _speak_morning_note(self, note: str) -> None:
        # A beat after the ring stops: lets the kill's own spoken
        # confirmation (the voice-kill path) finish first, and
        # gives the volume restore a moment to land so the note
        # speaks at everyday volume.
        try:
            await asyncio.sleep(2.0)
            await self._note_speaker(note)
            _LOGGER.info("alarm morning note spoken")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _LOGGER.error("alarm morning note delivery failed: %s", exc)

    async def snooze(self) -> dict:
        """Stop the ring; refire in snooze_s (9 min, HA parity)."""
        st = self._state
        if not st.ringing:
            return {"ok": False, "reason": "not ringing"}
        st.ringing = False
        task = self._hard_stop_task
        if task is not None:
            task.cancel()
        self._hard_stop_task = None
        if self._flasher is not None:
            await self._flasher.stop()
        if self._ring_via_tv:
            await self._stop_tv_ring()
        self._stop_playback()
        st.snooze_until = self._now().timestamp() + self._cfg.snooze_s
        self._save_state()
        refire = datetime.fromtimestamp(st.snooze_until, self._tz)
        _LOGGER.info("alarm snoozed; refire at %s", refire)
        return {"ok": True, "refire_at": refire.isoformat()}

    def set_alarm(self, hhmm: str) -> dict:
        """Voice-set path for the future matcher: sets the time and
        enables the alarm; the current repeat mode is inherited."""
        t = _parse_hhmm(hhmm)
        self._state.time = f"{t.hour:02d}:{t.minute:02d}"
        self._state.enabled = True
        self._state.last_fired_slot = None
        self._state.snooze_until = None
        self._save_state()
        _LOGGER.info("alarm set: %s", self.summary())
        return self.summary()

    def set_enabled(self, enabled: bool) -> dict:
        """Enable/disable without touching time or repeat (the
        matcher's alarm on/off). Returns the state summary."""
        self._state.enabled = bool(enabled)
        self._save_state()
        _LOGGER.info("alarm enabled=%s: %s", self._state.enabled, self.summary())
        return self.summary()

    def set_ring_volume(self, volume: float) -> dict:
        """Store the volume the box rings at (0.0-1.0). The fire
        sequence saves the current box volume, sets this for the
        ring, and restores the saved volume at ring end."""
        v = float(volume)
        if not 0.0 <= v <= 1.0:
            raise ValueError(f"ring volume out of range 0.0-1.0: {volume!r}")
        self._state.ring_volume = v
        self._save_state()
        _LOGGER.info("alarm ring volume set: %s", self.summary())
        return self.summary()

    # -- HTTP routes (registered on the core's audio endpoint) ---------------------

    def _serve_tone(self, query: dict) -> tuple[int, str, bytes]:
        if self._tone_bytes is None:
            return 503, "text/plain", b"tone not loaded\n"
        return 200, "audio/opus", self._tone_bytes

    def _serve_kill(self, query: dict) -> tuple[int, str, bytes]:
        supplied = (query.get("t") or [None])[0]
        if not supplied or not hmac.compare_digest(supplied, self._token):
            _LOGGER.warning("alarm kill endpoint: rejected (bad token)")
            return 403, "text/plain", b"forbidden\n"
        if self._loop is None:
            return 503, "text/plain", b"alarm not running\n"
        try:
            future = asyncio.run_coroutine_threadsafe(self.kill(), self._loop)
            result = future.result(timeout=10)
        except Exception as exc:
            _LOGGER.error("alarm kill via endpoint failed: %s", exc)
            return 500, "text/plain", b"kill failed\n"
        _LOGGER.info("alarm kill via HTTP endpoint: %r", result)
        return 200, "application/json", (json.dumps(result) + "\n").encode()


def _self_check() -> None:
    """Scheduler-math table test. Pure: in-memory states, fixed
    datetimes, no files, no network, no box. Run:
        python -m lyra_core.alarm"""
    tz = ZoneInfo("America/New_York")
    W = timedelta(seconds=1800)  # catch-up window (config default)

    def st(**kw) -> AlarmState:
        base = dict(
            time="06:00", enabled=True, repeat="Weekdays",
            last_fired_slot=None, snooze_until=None, ringing=False,
        )
        base.update(kw)
        return AlarmState(**base)

    def at(y, m, d, hh, mm, ss=0) -> datetime:
        return datetime(y, m, d, hh, mm, ss, tzinfo=tz)

    thu_start = at(2026, 11, 11, 12, 0)   # core started Wed midday
    cases = [
        # (label, now, state, started_at, expected)
        ("weekday 06:00 fires",
         at(2026, 11, 12, 6, 0, 30), st(), thu_start, "fire"),
        ("weekend 06:00 skipped (Weekdays)",
         at(2026, 11, 14, 6, 0, 30), st(), thu_start, None),
        ("weekend 06:00 fires (Weekends mode)",
         at(2026, 11, 14, 6, 0, 30), st(repeat="Weekends"), thu_start, "fire"),
        ("Once fires",
         at(2026, 11, 12, 6, 0, 30), st(repeat="Once"), thu_start, "fire"),
        ("disabled never fires",
         at(2026, 11, 12, 6, 0, 30), st(enabled=False), thu_start, None),
        ("catch-up: started 06:20 armed+unfired",
         at(2026, 11, 12, 6, 20, 5), st(), at(2026, 11, 12, 6, 20), "catchup"),
        ("no catch-up at 06:45",
         at(2026, 11, 12, 6, 45), st(), at(2026, 11, 12, 6, 45), None),
        ("long-running core never fires late (06:20)",
         at(2026, 11, 12, 6, 20, 5), st(), at(2026, 11, 12, 5, 0), None),
        ("already fired today: no double fire",
         at(2026, 11, 12, 6, 0, 30), st(last_fired_slot="2026-11-12"),
         thu_start, None),
        ("snooze pending suppresses schedule",
         at(2026, 11, 12, 6, 0, 30), st(snooze_until=9999999999.0),
         thu_start, None),
    ]
    for label, now, state, started, expected in cases:
        got = decide_fire(now, state, started, tz, W.total_seconds())
        status = "ok" if got == expected else "FAIL"
        print(f"[{status}] {label}: decide_fire -> {got!r} (want {expected!r})")
        assert got == expected, label

    # Once-disables is a fire() mutation: after a Once firing the
    # persisted state has enabled=False, so the next day decides None.
    after_once = st(repeat="Once", enabled=False, last_fired_slot="2026-11-12")
    got = decide_fire(at(2026, 11, 13, 6, 0, 30), after_once, thu_start, tz, 1800.0)
    assert got is None, "Once must not fire twice"
    print("[ok] Once fires then disables: next day -> None")

    # DST policy (item L): a gap time fires at the first valid
    # instant after the gap (the transition instant, 03:00 EDT);
    # an overlap time fires at its FIRST occurrence (fold=0, EDT).
    gap_slot = _local_slot(date(2026, 3, 8), _parse_hhmm("02:30"), tz)
    assert gap_slot == datetime(2026, 3, 8, 3, 0, tzinfo=tz), gap_slot
    print(f"[ok] DST gap: 2026-03-08 02:30 -> {gap_slot.isoformat()}")
    fb_slot = _local_slot(date(2026, 11, 1), _parse_hhmm("01:30"), tz)
    assert fb_slot.fold == 0 and fb_slot.utcoffset() == timedelta(hours=-4), fb_slot
    print(f"[ok] DST overlap: 2026-11-01 01:30 -> {fb_slot.isoformat()} (first)")
    dst_cases = [
        ("DST gap: 02:30 alarm fires at 03:00:30 EDT",
         datetime(2026, 3, 8, 3, 0, 30, tzinfo=tz),
         st(time="02:30", repeat="Daily"), at(2026, 3, 8, 0, 0), "fire"),
        ("DST gap: nothing fires at 01:59:59 EST",
         at(2026, 3, 8, 1, 59, 59),
         st(time="02:30", repeat="Daily"), at(2026, 3, 8, 0, 0), None),
        ("DST overlap: fires at first 01:30 (EDT)",
         datetime(2026, 11, 1, 1, 30, 30, tzinfo=tz),
         st(time="01:30", repeat="Daily"), at(2026, 10, 31, 12, 0), "fire"),
        ("DST overlap: second 01:30 (EST) does not refire",
         datetime(2026, 11, 1, 1, 30, 30, tzinfo=tz, fold=1),
         st(time="01:30", repeat="Daily", last_fired_slot="2026-11-01"),
         at(2026, 10, 31, 12, 0), None),
    ]
    for label, now, state, started, expected in dst_cases:
        got = decide_fire(now, state, started, tz, W.total_seconds())
        status = "ok" if got == expected else "FAIL"
        print(f"[{status}] {label}: decide_fire -> {got!r} (want {expected!r})")
        assert got == expected, label
    nf_gap = next_fire_after(
        at(2026, 3, 7, 12, 0), st(time="02:30", repeat="Daily"), tz)
    assert nf_gap == datetime(2026, 3, 8, 3, 0, tzinfo=tz), nf_gap
    print(f"[ok] DST gap next-fire -> {nf_gap.isoformat()}")
    nf_fb = next_fire_after(
        at(2026, 10, 31, 12, 0), st(time="01:30", repeat="Daily"), tz)
    assert nf_fb == fb_slot, nf_fb
    print(f"[ok] DST overlap next-fire -> {nf_fb.isoformat()}")

    # Next-fire readback for the migrated production state.
    migrated = AlarmState.from_json(dict(MIGRATED_STATE))
    nf = next_fire_after(at(2026, 11, 11, 13, 0), migrated, tz)
    assert nf == at(2026, 11, 12, 6, 0), nf
    print(f"[ok] migrated state next fire from Wed 13:00 -> {nf}")
    print("alarm self-check OK: all scheduler cases pass")


if __name__ == "__main__":
    _self_check()
