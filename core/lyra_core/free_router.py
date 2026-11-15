"""Free-mode router (trial, 2026-11-11): the local 8B runs the turn.

Daniel's experiment, ordered after the caged LLM router shipped:
"The reason to swap is to loosen the cage... I want to test the
model without it." So on this path there is NO intent enum, NO
slot validators, NO canned confirmations. The model gets the
verified transcript, a compact brief, and tool definitions that
mirror the tools layer; it interprets freely -- it may chain
getters into setters (relative changes), answer conversationally
with no tool call at all, or decline in its own words.

The one floor that stays is the anti-bluff floor (the Companion
lesson, 2026-11-08): actions happen ONLY through the tools layer,
whose methods verify by readback, and every tool result (ok +
confirmation + state) is fed back to the model as a tool message
BEFORE it composes its final spoken text. The brief instructs it
to report only what the tool results state.

Failure discipline: the whole turn has a hard deadline
(FREE_DEADLINE_S). A timeout, transport error, or empty/garbled
final text speaks the caged fallback line ("I don't understand.")
and logs a grep-able FREE_FAIL line with the raw model output, so
the trial's true failure rate is measurable from the journal. A
free-mode failure NEVER silently reroutes through the caged
router mid-turn.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .router import DIDNT_CATCH, NOT_UNDERSTOOD, LlmError, looks_garbled, normalize
from .tools import ToolResult

_LOGGER = logging.getLogger("lyra_core.free_router")

FREE_DEADLINE_S = 10.0
HISTORY_MAX_TURNS = 12
HISTORY_MAX_CHARS = 24000
HISTORY_IDLE_CLEAR_S = 1800.0
MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 6

_TZ = ZoneInfo("America/New_York")


def _stamped(command: str) -> str:
    """Prefix a user turn with the moment it was spoken, e.g.
    '[10:32 AM, Sun Nov 15] what time is it?'. Added 2026-11-15
    after the model answered a time question with zero tool calls
    and an invented 10:23 at 10:32: the stamp is fresh data
    attached to THIS turn, and because the turn slice is what
    history stores, every past turn keeps its own stamp — an old
    time reads as an old moment, not as now."""
    return f"[{datetime.now(_TZ):%I:%M %p, %a %b %d}] {command}"

_FALLBACK_BRIEF = """You are Lyra, a voice assistant in a speaker box in Daniel's bedroom. Daniel speaks to you out loud, and your replies are spoken aloud back to him. Speak in plain sentences, never lists or markdown, and when a tool gives you data, summarize it in a sentence or two instead of reciting it line by line.

The tools are your hands: they control the bedroom ceiling light, the PC monitor, your own speaking volume (the volume tools), and the alarm clock with its separate ring volume (the alarm tools), and they can fetch the time, the date, and the weather. Calling a tool is how anything actually happens, and each tool result tells you what happened. The time zone is America/New_York. On weekdays Daniel's alarm rings at 06:00 and he leaves for work around 06:45."""

_SOUL_PATH = Path(__file__).resolve().parent.parent / "SOUL.md"


def load_soul() -> str:
    """Her standing instructions: SOUL.md at the project root,
    re-read on every turn so editing the file changes her without
    a restart. Falls back to the built-in brief when the file is
    missing or empty."""
    try:
        text = _SOUL_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return _FALLBACK_BRIEF
    return text or _FALLBACK_BRIEF


SYSTEM_BRIEF = load_soul()


def _fn(name: str, description: str, properties: dict, required: list) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_PCT = {"type": "integer", "description": "Percent, 0 to 100."}
_DELTA = {
    "type": "integer",
    "description": "Signed percentage points to change by (negative lowers).",
}

TOOL_SCHEMAS: list[dict] = [
    _fn("light_on", "Turn the bedroom ceiling light on.", {}, []),
    _fn("light_off", "Turn the bedroom ceiling light off.", {}, []),
    _fn("light_set_brightness", "Set the ceiling light brightness.",
        {"percent": _PCT}, ["percent"]),
    _fn("light_adjust", "Change the ceiling light brightness by signed points.",
        {"delta_points": _DELTA}, ["delta_points"]),
    _fn("light_get_state", "Get the ceiling light's current on/off state and brightness.", {}, []),
    _fn("monitor_on", "Turn the PC monitor on.", {}, []),
    _fn("monitor_off", "Turn the PC monitor off.", {}, []),
    _fn("monitor_get_state", "Get the PC monitor's current power state.", {}, []),
    _fn("alarm_set", "Set the alarm time (24-hour HH:MM).",
        {"time": {"type": "string", "description": "HH:MM, 24-hour."}}, ["time"]),
    _fn("alarm_enable", "Enable the alarm.", {}, []),
    _fn("alarm_disable", "Disable the alarm.", {}, []),
    _fn("alarm_kill", "Kill the alarm: stops a ring, or cancels a pending snooze.", {}, []),
    _fn("alarm_snooze", "Snooze the alarm (only works while it is ringing).", {}, []),
    _fn("alarm_get_summary", "Get the alarm's time, enabled state, repeat, ring volume and whether it is ringing.", {}, []),
    _fn("alarm_volume_set", "Set the ALARM RING volume (not the speaking volume).",
        {"percent": _PCT}, ["percent"]),
    _fn("alarm_volume_get", "Get the ALARM RING volume.", {}, []),
    _fn("volume_set", "Set your everyday speaking volume.",
        {"percent": _PCT}, ["percent"]),
    _fn("volume_adjust", "Change your speaking volume by signed points.",
        {"delta_points": _DELTA}, ["delta_points"]),
    _fn("volume_get", "Get your current speaking volume.", {}, []),
    _fn("time_now", "Get the current time.", {}, []),
    _fn("date_today", "Get today's date.", {}, []),
    _fn("weather_report", "Get weather data for home; compose the spoken answer from the data. Pick the period that matches the question: overview for a general ask, now for the next few hours, today/tomorrow for that day, week for the days ahead. Use focus when the question is mainly about rain, temperature, or wind.", {"period": {"type": "string", "enum": ["overview", "now", "today", "tomorrow", "week"]}, "focus": {"type": "string", "enum": ["overview", "rain", "temperature", "wind"]}}, []),
    _fn("tv_set_brightness", "Set the brightness of the TV screen that shows your avatar. 100 is full brightness; lower dims the screen, for example at night.", {"percent": _PCT}, ["percent"]),
    _fn("tv_adjust_brightness", "Change the TV screen brightness by signed points.", {"delta_points": _DELTA}, ["delta_points"]),
    _fn("tv_get_brightness", "Get the TV screen's current brightness.", {}, []),
    _fn("tv_set_volume", "Set the volume of the TV's own speakers. This is the television's volume, not your speaking volume and not the screen brightness.", {"percent": _PCT}, ["percent"]),
    _fn("tv_adjust_volume", "Change the TV's speaker volume by signed points.", {"delta_points": _DELTA}, ["delta_points"]),
    _fn("tv_get_volume", "Get the TV's current speaker volume.", {}, []),
]


class FreeChatClient:
    """Chat-completions client for free mode: same stdlib-urllib-in-
    a-thread pattern as the caged LlmClient, but tool-capable. The
    per-call socket timeout is whatever is left of the turn budget,
    so a wedged server can never hang a turn past its deadline."""

    def __init__(self, url: str, model: str) -> None:
        self._endpoint = url.rstrip("/") + "/v1/chat/completions"
        self._model = model

    async def chat(self, messages: list, tools, timeout_s: float) -> dict:
        body = {
            "model": self._model,
            "temperature": 0.3,
            "max_tokens": 300,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        raw = await asyncio.to_thread(self._post, body, max(1.0, timeout_s))
        try:
            return raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"unparseable chat reply: {exc}") from exc

    def _post(self, body: dict, timeout_s: float) -> dict:
        req = urllib.request.Request(
            self._endpoint,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LlmError(f"chat call failed: {exc}") from exc


_WS = re.compile(r"\s+")


def sanitize_spoken(text: str) -> str:
    """Pipeline necessity only: the TTS stage needs a non-empty
    string. Whitespace is collapsed; the model's words are NOT
    trimmed, capped, or corrected -- in the free-reign trial her
    output is the test data. Returns '' when nothing speakable
    remains (the caller treats that as a FREE_FAIL)."""
    if not text:
        return ""
    return _WS.sub(" ", text).strip()


def _as_int(value) -> int:
    return int(round(float(value)))


class FreeRouter:
    """session.on_command, free mode: same hook signature as the
    caged Router ((command, verify=None) -> spoken line), opposite
    philosophy. The model interprets and words everything; the
    tools layer is the only way anything actually happens."""

    def __init__(
        self,
        cfg,
        *,
        light,
        monitor,
        alarm_tool,
        media_volume,
        clock,
        weather,
        tv=None,
        tv_volume=None,
        client=None,
    ) -> None:
        self._cfg = cfg
        self._light = light
        self._monitor = monitor
        self._alarm = alarm_tool
        self._media_volume = media_volume
        self._clock = clock
        self._weather = weather
        self._tv = tv
        self._tv_volume = tv_volume
        self._client = client or FreeChatClient(cfg.llm_url, cfg.llm_model)
        self._history: list[list[dict]] = []
        self._last_turn_at: float | None = None

    def describe(self) -> str:
        return (
            f"FREE router: uncaged model via {self._cfg.llm_model} "
            f"@ {self._cfg.llm_url}, {len(TOOL_SCHEMAS)} tools, "
            f"deadline {FREE_DEADLINE_S:.0f}s"
        )

    # -- the hook ------------------------------------------------------

    async def handle(self, command: str, verify=None) -> object:
        normalized = normalize(command)
        if looks_garbled(normalized):
            _LOGGER.info("garbled command %r -> didn't catch", command)
            return DIDNT_CATCH
        if (
            self._last_turn_at is not None
            and time.monotonic() - self._last_turn_at > HISTORY_IDLE_CLEAR_S
            and self._history
        ):
            _LOGGER.info("free history cleared after idle gap")
            self._history.clear()
        started = time.monotonic()
        raw_final = None
        turn_slice: list = []
        try:
            raw_final, calls, turn_slice = await asyncio.wait_for(
                self._run_turn(command), timeout=FREE_DEADLINE_S
            )
        except asyncio.TimeoutError:
            _LOGGER.error(
                "FREE_FAIL timeout after %.1fs for %r raw=%r",
                FREE_DEADLINE_S, command, raw_final,
            )
            self._remember(command, "Sorry, something went wrong.")
            return "Sorry, something went wrong."
        except Exception as exc:
            _LOGGER.error(
                "FREE_FAIL %s for %r raw=%r", exc, command, raw_final
            )
            self._remember(command, "Sorry, something went wrong.")
            return "Sorry, something went wrong."
        text = sanitize_spoken(raw_final or "")
        if not text:
            _LOGGER.error(
                "FREE_FAIL empty/garbled final text for %r raw=%r",
                command, raw_final,
            )
            self._remember(command, NOT_UNDERSTOOD)
            return NOT_UNDERSTOOD
        self._remember_turn(turn_slice, text)
        _LOGGER.info(
            "free turn %r -> calls=%s final=%r (%.2fs, history=%d turns)",
            command, calls, text, time.monotonic() - started,
            len(self._history),
        )
        return text

    # -- rolling conversation context --------------------------------

    def _remember(self, command: str, spoken: str) -> None:
        """Record a turn whose model exchange failed (a fallback
        line was spoken) in its honest minimal form."""
        self._history.append([
            {"role": "user", "content": _stamped(command)},
            {"role": "assistant", "content": spoken},
        ])
        self._last_turn_at = time.monotonic()
        self._trim_history()

    def _remember_turn(self, turn_slice: list, spoken: str) -> None:
        turn = list(turn_slice) + [{"role": "assistant", "content": spoken}]
        self._history.append(turn)
        self._last_turn_at = time.monotonic()
        self._trim_history()

    def _trim_history(self) -> None:
        while len(self._history) > HISTORY_MAX_TURNS:
            self._history.pop(0)

        def _chars() -> int:
            return sum(
                len(str(m.get("content") or ""))
                for turn in self._history for m in turn
            )

        while len(self._history) > 1 and _chars() > HISTORY_MAX_CHARS:
            self._history.pop(0)

    async def _run_turn(self, command: str):
        deadline = time.monotonic() + FREE_DEADLINE_S
        history = [m for turn in self._history for m in turn]
        messages = [
            {"role": "system", "content": load_soul()},
            *history,
            {"role": "user", "content": _stamped(command)},
        ]
        slice_at = len(messages) - 1
        calls_made: list[str] = []
        total_calls = 0
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            last_round = round_no == MAX_TOOL_ROUNDS
            message = await self._client.chat(
                messages,
                None if last_round else TOOL_SCHEMAS,
                deadline - time.monotonic(),
            )
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return message.get("content") or "", calls_made, messages[slice_at:]
            messages.append(message)
            for call in tool_calls:
                total_calls += 1
                if total_calls > MAX_TOOL_CALLS:
                    feedback = {
                        "ok": False,
                        "result": "Tool budget for this turn is used up; "
                                  "answer with what you have.",
                        "state": {},
                    }
                else:
                    feedback = await self._execute(call)
                fn = (call.get("function") or {}).get("name") or "?"
                calls_made.append(fn)
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call-{total_calls}",
                    "content": json.dumps(feedback, default=str),
                })
        return "", calls_made, messages[slice_at:]


    # -- tool execution: the anti-bluff floor ---------------------------

    async def _execute(self, call: dict) -> dict:
        fn = call.get("function") or {}
        name = fn.get("name") or ""
        raw_args = fn.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (ValueError, TypeError):
            return {"ok": False, "result": f"Unparseable arguments: {raw_args!r}", "state": {}}
        try:
            return await self._dispatch(name, args)
        except Exception as exc:
            _LOGGER.warning("free tool %s failed: %s", name, exc)
            return {"ok": False, "result": f"The {name} tool failed: {exc}", "state": {}}

    @staticmethod
    def _wrap(result: ToolResult) -> dict:
        return {
            "ok": bool(result.ok),
            "result": result.confirmation,
            "state": result.state or {},
        }

    async def _dispatch(self, name: str, args: dict) -> dict:
        if name == "light_on":
            return self._wrap(await self._light.turn_on())
        if name == "light_off":
            return self._wrap(await self._light.turn_off())
        if name == "light_set_brightness":
            return self._wrap(await self._light.set_brightness(_as_int(args["percent"])))
        if name == "light_adjust":
            return self._wrap(await self._light.adjust_brightness(_as_int(args["delta_points"])))
        if name == "light_get_state":
            state = await self._light.get_state()
            if state is None:
                return {"ok": False, "result": "I couldn't reach the light.", "state": {}}
            line = "Light is on" if state.get("on") else "Light is off"
            if state.get("on") and isinstance(state.get("brightness"), int):
                line += f" at {state['brightness']} percent"
            return {"ok": True, "result": line + ".", "state": state}
        if name == "monitor_on":
            return self._wrap(await self._monitor.turn_on())
        if name == "monitor_off":
            return self._wrap(await self._monitor.turn_off())
        if name == "monitor_get_state":
            state = await self._monitor.get_state()
            if state is None:
                return {"ok": False, "result": "I couldn't confirm the monitor state.", "state": {}}
            return {"ok": True, "result": f"Monitor is {state}.", "state": {"monitor": state}}
        if name == "alarm_set":
            return self._wrap(await self._alarm.set_alarm(str(args["time"])))
        if name == "alarm_enable":
            return self._wrap(await self._alarm.set_enabled(True))
        if name == "alarm_disable":
            return self._wrap(await self._alarm.set_enabled(False))
        if name == "alarm_kill":
            return self._wrap(await self._alarm.kill())
        if name == "alarm_snooze":
            return self._wrap(await self._alarm.snooze())
        if name == "alarm_get_summary":
            summary = self._alarm.summary()
            line = (
                f"Alarm is set for {summary.get('time')}, "
                f"{'enabled' if summary.get('enabled') else 'disabled'}, "
                f"repeat {summary.get('repeat')}, "
                f"ring volume {_pct_line(summary.get('ring_volume'))}, "
                f"ringing: {bool(summary.get('ringing'))}."
            )
            return {"ok": True, "result": line, "state": summary}
        if name == "alarm_volume_set":
            return self._wrap(await self._alarm.set_ring_volume(_as_int(args["percent"]) / 100.0))
        if name == "alarm_volume_get":
            return self._wrap(await self._alarm.get_ring_volume())
        if name == "volume_set":
            return self._wrap(await self._media_volume.set_volume(_as_int(args["percent"]) / 100.0))
        if name == "volume_adjust":
            return self._wrap(await self._media_volume.adjust_volume(_as_int(args["delta_points"])))
        if name == "volume_get":
            return self._wrap(await self._media_volume.get_volume())
        if name == "time_now":
            return self._wrap(self._clock.time_result())
        if name == "date_today":
            return self._wrap(self._clock.date_result())
        if name == "weather_report":
            period = args.get("period") if isinstance(args.get("period"), str) else "overview"
            focus = args.get("focus") if isinstance(args.get("focus"), str) else "overview"
            return self._wrap(await self._weather.report(period, focus))
        if name == "tv_set_brightness":
            return self._wrap(await self._tv.set_brightness(_as_int(args["percent"])))
        if name == "tv_adjust_brightness":
            return self._wrap(await self._tv.adjust_brightness(_as_int(args["delta_points"])))
        if name == "tv_get_brightness":
            return self._wrap(await self._tv.get_brightness())
        if name == "tv_set_volume":
            return self._wrap(await self._tv_volume.set_volume(_as_int(args["percent"])))
        if name == "tv_adjust_volume":
            return self._wrap(await self._tv_volume.adjust_volume(_as_int(args["delta_points"])))
        if name == "tv_get_volume":
            return self._wrap(await self._tv_volume.get_volume())
        return {"ok": False, "result": f"Unknown tool: {name}", "state": {}}


def _pct_line(volume) -> str:
    if isinstance(volume, (int, float)):
        return f"{int(round(volume * 100))} percent"
    return "unknown"
