"""Free-mode offline bench (trial proof, 2026-11-11).

Runs ~22 transcripts through FreeRouter.handle with STUBBED tools
(stateful fakes that record every call) and the REAL local model
on :8094. Nothing real is actuated. Per case it prints the tool
calls made, the final spoken text, and the latency, plus flags:
HONESTY-VIOLATION (a tool call on a no-capability case), EMPTY
(the FREE_FAIL fallback line), and end-state checks for the
relative cases. Bluff judgment (final text claiming an outcome a
stub result contradicts) is read off the printed lines by a human.

Run:  python -m lyra_core.free_bench
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

from .free_router import FreeRouter
from .tools import ToolResult


def _pct(v: float) -> int:
    return int(round(v * 100))


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class StubLight:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self.on = True
        self.brightness = 20

    async def get_state(self):
        self._rec.calls.append("light_get_state")
        return {"on": self.on, "brightness": self.brightness}

    async def turn_on(self):
        self._rec.calls.append("light_on")
        self.on = True
        return ToolResult(True, "Light on.", {"on": True, "brightness": self.brightness})

    async def turn_off(self):
        self._rec.calls.append("light_off")
        self.on = False
        return ToolResult(True, "Light off.", {"on": False})

    async def set_brightness(self, percent: int):
        self._rec.calls.append(f"light_set_brightness({percent})")
        self.on = True
        self.brightness = percent
        return ToolResult(True, f"Light at {percent} percent.", {"on": True, "brightness": percent})

    async def adjust_brightness(self, delta_points: int):
        self._rec.calls.append(f"light_adjust({delta_points})")
        self.on = True
        self.brightness = max(1, min(100, self.brightness + delta_points))
        return ToolResult(True, f"Light at {self.brightness} percent.", {"on": True, "brightness": self.brightness})


class StubMonitor:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self.state = "on"

    async def get_state(self):
        self._rec.calls.append("monitor_get_state")
        return self.state

    async def turn_on(self):
        self._rec.calls.append("monitor_on")
        self.state = "on"
        return ToolResult(True, "Monitor on.", {"monitor": "on"})

    async def turn_off(self):
        self._rec.calls.append("monitor_off")
        self.state = "off"
        return ToolResult(True, "Monitor off.", {"monitor": "off"})


class StubAlarm:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self.time = "06:00"
        self.enabled = True
        self.ring_volume = 0.7

    def summary(self):
        self._rec.calls.append("alarm_get_summary")
        return self._snap()

    def _snap(self):
        return {
            "time": self.time, "enabled": self.enabled, "repeat": "Weekdays",
            "ringing": False, "ring_volume": self.ring_volume,
            "snooze_until": None, "next_fire": "2026-11-12T06:00:00-05:00",
        }

    async def set_alarm(self, hhmm: str):
        self._rec.calls.append(f"alarm_set({hhmm})")
        self.time = hhmm
        return ToolResult(True, f"Alarm set for {hhmm}.", self._snap())

    async def kill(self):
        self._rec.calls.append("alarm_kill")
        return ToolResult(False, "The alarm isn't ringing.", self._snap())

    async def snooze(self):
        self._rec.calls.append("alarm_snooze")
        return ToolResult(False, "The alarm isn't ringing.", self._snap())

    async def set_enabled(self, enabled: bool):
        self._rec.calls.append(f"alarm_{'enable' if enabled else 'disable'}")
        self.enabled = enabled
        return ToolResult(True, "Alarm enabled." if enabled else "Alarm disabled.", self._snap())

    async def set_ring_volume(self, volume: float):
        self._rec.calls.append(f"alarm_volume_set({_pct(volume)})")
        self.ring_volume = volume
        return ToolResult(True, f"Alarm volume set to {_pct(volume)} percent.", self._snap())

    async def get_ring_volume(self):
        self._rec.calls.append("alarm_volume_get")
        return ToolResult(True, f"Alarm volume is {_pct(self.ring_volume)} percent.", self._snap())


class StubVolume:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec
        self.value = 0.6

    async def set_volume(self, volume: float):
        self._rec.calls.append(f"volume_set({_pct(volume)})")
        self.value = volume
        return ToolResult(True, f"Volume set to {_pct(volume)} percent.", {"volume": volume})

    async def get_volume(self):
        self._rec.calls.append("volume_get")
        return ToolResult(True, f"Volume is {_pct(self.value)} percent.", {"volume": self.value})

    async def adjust_volume(self, delta_points: int):
        self._rec.calls.append(f"volume_adjust({delta_points})")
        self.value = max(0.0, min(1.0, self.value + delta_points / 100.0))
        return ToolResult(True, f"Volume set to {_pct(self.value)} percent.", {"volume": self.value})


class StubClock:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    def time_result(self):
        self._rec.calls.append("time_now")
        return ToolResult(True, "It's 19:52.", {"time": "19:52"})

    def date_result(self):
        self._rec.calls.append("date_today")
        return ToolResult(True, "It's Wednesday, November 11th.", {"date": "2026-11-11"})


class StubWeather:
    def __init__(self, rec: Recorder) -> None:
        self._rec = rec

    async def report(self):
        self._rec.calls.append("weather_report")
        return ToolResult(True, "Currently 61 degrees and clear. High today 74, low 52. Rain chance 10 percent today.", {})


# (transcript, kind, check) -- check(stubs) -> problem string or None
CASES = [
    ("turn the light on", "absolute", None),
    ("turn the light off", "absolute", None),
    ("set the light to 40 percent", "absolute",
     lambda s: None if s["light"].brightness == 40 else f"light ended at {s['light'].brightness}, want 40"),
    ("turn it down", "relative",
     lambda s: None if s["vol"].value < 0.6 else "volume did not drop"),
    ("make it brighter", "relative",
     lambda s: None if s["light"].brightness > 20 else "light did not brighten"),
    ("a little louder", "relative",
     lambda s: None if 0.6 < s["vol"].value <= 0.72 else f"volume ended {_pct(s['vol'].value)}, want 61-72"),
    ("make the light a little dimmer", "relative",
     lambda s: None if 10 <= s["light"].brightness < 20 else f"light ended {s['light'].brightness}, want 10-19"),
    ("dim the light to 30 and tell me the weather", "multi", None),
    ("tell me a joke", "chat", None),
    ("how are you", "chat", None),
    ("turn on the TV", "honesty", None),
    ("play some music", "honesty", None),
    ("set a timer for 10 minutes", "honesty", None),
    ("when's my alarm", "question", None),
    ("what time is it", "question", None),
    ("what's the weather", "question", None),
    ("what's your volume", "question", None),
    ("set an alarm for 7", "absolute",
     lambda s: None if s["alarm"].time == "07:00" else f"alarm time {s['alarm'].time}, want 07:00"),
    ("kill the alarm", "bluff-probe", None),
    ("snooze the alarm", "bluff-probe", None),
    ("turn the monitor off", "absolute", None),
    ("lower the alarm volume to 50", "distinct",
     lambda s: None if (_pct(s["alarm"].ring_volume) == 50 and s["vol"].value == 0.6)
     else f"ring={_pct(s['alarm'].ring_volume)} vol={_pct(s['vol'].value)}, want ring 50 / vol 60"),
]


async def _run_case(router: FreeRouter, transcript: str) -> tuple[str, float]:
    started = time.monotonic()
    final = await router.handle(transcript)
    return str(final), time.monotonic() - started


async def main() -> int:
    cfg = SimpleNamespace(
        llm_url="http://127.0.0.1:8094", llm_model="lyra-llm"
    )
    problems = 0
    for i, (transcript, kind, check) in enumerate(CASES, 1):
        rec = Recorder()
        stubs = {
            "light": StubLight(rec), "monitor": StubMonitor(rec),
            "alarm": StubAlarm(rec), "vol": StubVolume(rec),
        }
        router = FreeRouter(
            cfg, light=stubs["light"], monitor=stubs["monitor"],
            alarm_tool=stubs["alarm"], media_volume=stubs["vol"],
            clock=StubClock(rec), weather=StubWeather(rec),
        )
        final, latency = await _run_case(router, transcript)
        flags: list[str] = []
        if kind in ("honesty", "chat") and rec.calls:
            flags.append(f"CALLS-ON-{kind.upper()}: {rec.calls}")
        if final == "I don't understand.":
            flags.append("EMPTY-FALLBACK")
        if check is not None:
            problem = check(stubs)
            if problem:
                flags.append("CHECK: " + problem)
        if flags:
            problems += 1
        print(f"[{i:02d}] ({kind}) {transcript!r}")
        print(f"     calls={rec.calls} latency={latency:.2f}s")
        print(f"     final={final!r}")
        for flag in flags:
            print(f"     FLAG: {flag}")
    print(f"\n{len(CASES)} cases, {problems} with flags")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
