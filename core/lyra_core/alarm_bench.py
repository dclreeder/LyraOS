"""Alarm fire-path dry run (Phase 3 bench). NO real hardware:

- The box is a FakeBox recording every media call; the fire sequence
  must be exactly: light on(100) + readback -> volume read -> volume
  set 1.0 -> play tone URL -> [kill] -> stop -> volume restore 0.6,
  with the light never touched again (it stays on).
- The light is a FakeLight standing in for the Shelly RPC (the real
  RPC path gets its own one-shot round-trip proof separately).
- State/secrets live in a temp dir; the production state file, the
  real box and the real light are never touched. No audio is played.

Also covered: the 15-minute hard stop (asserted as the configured
constant, then exercised at a compressed 0.3 s), snooze (stops the
ring, restores volume, refires via the scheduler tick), and Once
(fires, then disables itself in the persisted state).

Run:  python -m lyra_core.alarm_bench
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import tempfile
import time
from pathlib import Path

from .alarm import Alarm, ShellyLight  # noqa: F401  (ShellyLight: API reference)
from .audio_http import AudioHttpServer
from .config import CoreConfig


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


class FakeBox:
    def __init__(self, calls: list) -> None:
        self._calls = calls
        self._volume = 0.6
        self.is_connected = True
        self.media_state = None

    @property
    def media_volume(self) -> float:
        self._calls.append(("box.volume_read", self._volume))
        return self._volume

    def media_set_volume(self, volume: float) -> None:
        self._calls.append(("box.set_volume", volume))
        self._volume = volume

    def media_play(self, url: str) -> None:
        self._calls.append(("box.play", url))

    def media_stop(self) -> None:
        self._calls.append(("box.stop",))


def make_alarm(tmp: str, calls: list, **alarm_overrides) -> Alarm:
    cfg = CoreConfig.load()
    alarm_cfg = dataclasses.replace(
        cfg.alarm,
        state_path=f"{tmp}/state.json",
        secrets_path=f"{tmp}/secrets.json",
        **alarm_overrides,
    )
    cfg = dataclasses.replace(cfg, alarm=alarm_cfg)
    http = AudioHttpServer("127.0.0.1", 0)
    box = FakeBox(calls)
    alarm = Alarm(cfg, box_getter=lambda: box, http=http)
    alarm._light = FakeLight(calls)  # type: ignore[assignment]
    return alarm


async def amain() -> None:
    # --- Part 1: fire -> kill, exact call sequence ---------------------
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
                "box.volume_read", "box.set_volume", "box.play",
            ], names
            assert calls[0] == ("light.set_on", 100)
            assert calls[2] == ("box.volume_read", 0.6)
            assert calls[3] == ("box.set_volume", 1.0)
            assert calls[4][1].endswith("/alarm/tone.opus"), calls[4]
            print("[ok] fire sequence: light 100% -> vol read 0.6 -> "
                  "vol 1.0 -> play tone URL")
            result = await alarm.kill()
            assert result == {"ok": True, "was_ringing": True,
                              "snooze_cleared": False}
            names = [c[0] for c in calls]
            assert names[5:] == ["box.stop", "box.set_volume"], names
            assert calls[6] == ("box.set_volume", 0.6)
            assert "light.set_off" not in names, "light must stay on"
            print("[ok] kill: stop -> volume restored 0.6; light untouched")
            persisted = json.loads(Path(f"{tmp}/state.json").read_text())
            assert persisted["ringing"] is False
            assert persisted["last_fired_slot"] is not None
            print(f"[ok] persisted after kill: {persisted}")
        finally:
            await alarm.stop()

    # --- Part 2: hard stop ends the ring by itself ----------------------
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
            assert names[-2:] == ["box.stop", "box.set_volume"], names
            print("[ok] hard stop (compressed 0.3 s) ended ring + restored volume")
        finally:
            await alarm.stop()

    # --- Part 3: snooze stops, restores, refires via tick ----------------
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
            plays = [c for c in calls if c[0] == "box.play"]
            assert len(plays) == 1
            print(f"[ok] snooze: ring stopped, refire in {delta:.0f} s (9 min)")
            alarm._state.snooze_until = time.time() - 1  # due now
            await alarm._tick()
            plays = [c for c in calls if c[0] == "box.play"]
            assert len(plays) == 2, "snooze refire did not happen"
            assert alarm._state.ringing is True
            print("[ok] snooze refire via scheduler tick: second play issued")
            await alarm.kill()
        finally:
            await alarm.stop()

    # --- Part 4: Once fires, then disables itself --------------------------
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
