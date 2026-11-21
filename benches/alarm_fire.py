"""Alarm fire-path dry run (Phase 3 bench). NO real hardware:

- The sounder is a FakeRinger recording every call; the fire
  sequence must be exactly: light on(100) + readback ->
  ringer.start -> [kill] -> ringer.stop, with the light never
  touched again (it stays on) and NO volume save/restore
  anywhere (the tone plays at the default sink's own volume;
  there is no per-stream volume override).
- The light is a FakeLight standing in for the Shelly RPC (the
  real RPC path gets its own one-shot round-trip proof
  separately).
- State/secrets live in a temp dir; the production state file
  and the real light are never touched. No audio is played
  (the FakeRinger only records).

Also covered: the 15-minute hard stop (asserted as the config
constant, then exercised at a compressed 0.3 s), snooze (stops
the ring, refires via the scheduler tick), and Once (fires,
then disables itself in the persisted state).

Run via benches/run_all.py (suite: alarm_fire).
"""
from __future__ import annotations
import asyncio
import dataclasses
import json
import tempfile
import time
from pathlib import Path

# --- suite bootstrap: repo-relative import paths (core + this dir) ---
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.join(_HERE, "..", "core"), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end suite bootstrap ---
from lyra_core.alarm import Alarm, ShellyLight  # noqa: F401  (ShellyLight: API reference)
from lyra_core.audio_http import AudioHttpServer
from lyra_core.config import CoreConfig


class FakeLight:
    def __init__(self, calls: list) -> None:
        self._calls = calls
        self.output = False
        self.brightness = 30

    async def set_on(self, brightness: int) -> dict:
        self._calls.append(("light.set_on", brightness))
        self.output = True
        self.brightness = brightness
        status = {"output": True, "brightness": brightness}
        self._calls.append(("light.readback", status))
        return status

    async def set_off(self) -> dict:
        self._calls.append(("light.set_off",))
        self.output = False
        return {"output": False}


class FakeRinger:
    """The default-sink sounder's bench double: records start
    and stop."""

    def __init__(self, calls: list) -> None:
        self._calls = calls

    async def start(self) -> bool:
        self._calls.append(("ringer.start",))
        return True

    async def stop(self) -> None:
        self._calls.append(("ringer.stop",))


def make_alarm(tmp: str, calls: list, **alarm_overrides) -> Alarm:
    cfg = CoreConfig.load()
    alarm_cfg = dataclasses.replace(
        cfg.alarm,
        state_path=f"{tmp}/state.json",
        secrets_path=f"{tmp}/secrets.json",
        button_host="",
        **alarm_overrides,
    )
    cfg = dataclasses.replace(cfg, alarm=alarm_cfg)
    http = AudioHttpServer("127.0.0.1", 0)
    alarm = Alarm(cfg, http=http)
    alarm.set_ringer(FakeRinger(calls))
    alarm._light = FakeLight(calls)  # type: ignore[assignment]
    alarm._flasher = None
    # The real tone asset stands in for validate_assets' load:
    # fire() takes the sounding path only when the tone loaded.
    assert alarm.validate_assets(), "tone asset must load for the bench"
    return alarm


async def amain() -> None:
    # ---- Part 1: fire -> kill, exact call sequence -------------
    with tempfile.TemporaryDirectory() as tmp:
        calls: list = []
        alarm = make_alarm(tmp, calls)
        assert alarm._cfg.hard_stop_s == 900.0, "hard stop must be 15 min"
        print("[ok] hard-stop constant = 900 s (15 min)")
        await alarm.start()
        try:
            assert await alarm.fire(reason="fire") is True
            names = [c[0] for c in calls]
            assert names == [
                "light.set_on", "light.readback",
                "ringer.start",
            ], names
            assert calls[0] == ("light.set_on", 100)
            assert calls[2] == ("ringer.start",), calls[2]
            print("[ok] fire sequence: light 100% -> ringer.start; "
                  "no volume save/restore anywhere")
            result = await alarm.kill()
            assert result["ok"] is True and result["was_ringing"] is True
            assert result["snooze_cleared"] is False
            assert result["morning_note"] is None
            names = [c[0] for c in calls]
            assert names[3:] == ["ringer.stop"], names
            assert "light.set_off" not in names, "light must stay on"
            print("[ok] kill: ringer stopped; light untouched")
            persisted = json.loads(Path(f"{tmp}/state.json").read_text())
            assert persisted["ringing"] is False
            assert persisted["last_fired_slot"] is not None
            assert "saved_volume" not in persisted
            assert "saved_tv_volume" not in persisted
            print(f"[ok] persisted after kill: {persisted}")
        finally:
            await alarm.stop()

    # ---- Part 2: hard stop ends the ring by itself -------------
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        alarm = make_alarm(tmp, calls, hard_stop_s=0.3)
        await alarm.start()
        try:
            await alarm.fire(reason="fire")
            assert alarm._state.ringing is True
            await asyncio.sleep(0.7)
            assert alarm._state.ringing is False, "hard stop did not end ring"
            names = [c[0] for c in calls]
            assert names[-1] == "ringer.stop", names
            print("[ok] hard stop (compressed 0.3 s) ended the ring")
        finally:
            await alarm.stop()

    # ---- Part 3: snooze stops, refires via tick -----------------
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        alarm = make_alarm(tmp, calls)
        await alarm.start()
        try:
            await alarm.fire(reason="fire")
            before = time.time()
            result = await alarm.snooze()
            assert result["ok"] is True
            delta = alarm._state.snooze_until - before
            assert 535 <= delta <= 545, delta  # 9 min = 540 s
            assert alarm._state.ringing is False
            starts = [c for c in calls if c[0] == "ringer.start"]
            assert len(starts) == 1
            print(f"[ok] snooze: ring stopped, refire in {delta:.0f} s (9 min)")
            alarm._state.snooze_until = time.time() - 1  # due now
            await alarm._tick()
            starts = [c for c in calls if c[0] == "ringer.start"]
            assert len(starts) == 2, "snooze refire did not happen"
            assert alarm._state.ringing is True
            print("[ok] snooze refire via scheduler tick: second start issued")
            await alarm.kill()
        finally:
            await alarm.stop()

    # ---- Part 4: Once fires, then disables itself ---------------
    with tempfile.TemporaryDirectory() as tmp:
        calls = []
        alarm = make_alarm(tmp, calls)
        alarm._state.repeat = "Once"
        alarm._save_state()
        await alarm.fire(reason="fire")
        persisted = json.loads(Path(f"{tmp}/state.json").read_text())
        assert persisted["repeat"] == "Once"
        assert persisted["enabled"] is False, persisted
        print("[ok] Once: fired and persisted enabled=False")
        await alarm.kill()

    print("alarm bench OK: all fire-path assertions pass")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
