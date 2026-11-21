"""Phase 4 core API: Lyra's hands on the core.

Authenticated routes on the core's HTTP endpoint (:8099),
sharing the alarm kill endpoint's token discipline: the token lives
in the core's secrets file (alarm_secrets.json, 0600), is handed to
this module by the production composition, and is never logged.

- POST /tool     {"tool": ..., "action": ..., "args": {...}}
  Executes through the SAME tool objects the phrase router uses
  (composed in production_main, never forked) and returns the
  ToolResult as JSON: {"ok": bool, "confirmation": str,
  "state": {...}}. Actions mirror the router's dispatch:
    light   state | on | off | dim {percent}
    alarm   summary | set {time "HH:MM"} | kill | snooze |
            enable | disable |
            note_get | note_set {text, date "YYYY-MM-DD"} |
            note_clear
    time    (the clock's time result)
    date    (the clock's date result)
    weather (the weather report)
    tv      get | set {percent} | adjust {delta_points}
            (the TV screen brightness of the lyra-web viewer)
  A read (state/summary) composes its confirmation from the live
  readback, like every other tool sentence (D2).

- POST /announce {"text": "..."}
  Speaks the text via the core's announce path (Kokoro af_heart
  -> TV mirror), injected by the composition. Statement-only: a "?" anywhere in the text is
  rejected 400 (the standing announcements rule), as are empty
  texts and texts over the length cap.

- POST /mode     {"mode": "wakegate" | "rtc" | "sleep"}
  Mode ownership (2026-11-19): the core owns mode state -- the
  composition's callback persists the choice in the core's own
  state file and runs the transition; the answer carries the
  applied mode. GET /mode returns {"ok": true, "mode": <applied>}.
  Unknown modes are rejected 400.

Auth: the token rides the `t` query parameter, exactly like the
alarm kill endpoint (GET /alarm/kill?t=...), and is compared with
hmac.compare_digest. Bad or missing token -> 403, before any body
validation. Every accepted call is logged (tool/action and the
outcome only: argument values and announcement text are never
written to the journal -- dial-in 2026-11-11, item M).

Handlers run in the HTTP server's worker threads; the actual tool
calls and the announce coroutine are bridged onto the core's event
loop with run_coroutine_threadsafe -- the alarm kill endpoint's
discipline. On a handler timeout the bridged future is CANCELLED
before the 504 is returned (dial-in item B): a call that has not
started never starts, one in flight is interrupted at its next
await, and a retry therefore cannot double-execute it.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import json
import logging
from collections.abc import Awaitable, Callable

from .audio_http import AudioHttpServer
from .tools import (
    AlarmTool,
    ClockTool,
    JellyfinTool,
    LightTool,
    ToolResult,
    TvTool,
    TvVolumeTool,
    WeatherTool,
)

_LOGGER = logging.getLogger("lyra_core.api")

TOOL_PATH = "/tool"
ANNOUNCE_PATH = "/announce"
RTC_OFFER_PATH = "/rtc/offer"
MODE_PATH = "/mode"
ANNOUNCE_MAX_CHARS = 200
_TOOL_TIMEOUT_S = 90.0
_ANNOUNCE_TIMEOUT_S = 75.0
_RTC_OFFER_TIMEOUT_S = 20.0
_MODE_TIMEOUT_S = 30.0

# The composition's announce: text -> None on success; raises
# AnnounceUnavailable when there is no way to speak.
AnnounceFn = Callable[[str], Awaitable[None]]

# RTC signaling callback: an SDP offer -> the SDP answer dict, or
# a dict with "error" (plus optional "status"). Raises on failure.
RtcOfferFn = Callable[[str, str], Awaitable[dict]]

# Mode callbacks (2026-11-19): mode_set persists + applies the
# requested mode on the core's event loop and returns the applied
# mode (raising ValueError for an unknown mode); mode_get returns
# the currently applied mode.
ModeSetFn = Callable[[str], Awaitable[str]]
ModeGetFn = Callable[[], str]


class AnnounceUnavailable(Exception):
    """Announce is unavailable (no speech output path)."""


class _BadRequest(Exception):
    """The request body failed validation (HTTP 400)."""


def _json(status: int, payload: dict) -> tuple[int, str, bytes]:
    return status, "application/json", (json.dumps(payload) + "\n").encode()


def _parse_json_body(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        raise _BadRequest("request body is not valid JSON")
    if not isinstance(payload, dict):
        raise _BadRequest("request body must be a JSON object")
    return payload


def _arg_percent(args: dict) -> int:
    value = args.get("percent")
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadRequest("args.percent (integer 0-100) is required")
    if not 0 <= value <= 100:
        raise _BadRequest("args.percent out of range 0-100")
    return value


class CoreApi:
    """POST /tool + POST /announce over the router's own tools,
    plus the /mode routes (mode ownership, 2026-11-19)."""

    def __init__(
        self,
        *,
        http: AudioHttpServer,
        token: str,
        light: LightTool,
        alarm_tool: AlarmTool,
        clock: ClockTool,
        weather: WeatherTool,
        announce: AnnounceFn,
        rtc_offer: RtcOfferFn | None = None,
        mode_set: ModeSetFn | None = None,
        mode_get: ModeGetFn | None = None,
        tv: TvTool | None = None,
        tv_volume: TvVolumeTool | None = None,
        jellyfin: JellyfinTool | None = None,
    ) -> None:
        self._http = http
        self._token = token
        self._light = light
        self._alarm = alarm_tool
        self._clock = clock
        self._weather = weather
        self._announce = announce
        self._rtc_offer = rtc_offer
        self._mode_set = mode_set
        self._mode_get = mode_get
        self._tv = tv
        self._tv_volume = tv_volume
        self._jellyfin = jellyfin
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Capture the core's event loop and register the routes."""
        self._loop = asyncio.get_running_loop()
        self._http.add_post_route(TOOL_PATH, self._serve_tool)
        self._http.add_post_route(ANNOUNCE_PATH, self._serve_announce)
        if self._rtc_offer is not None:
            self._http.add_post_route(RTC_OFFER_PATH, self._serve_rtc_offer)
            _LOGGER.info(
                "core API registered: POST %s (token auth)", RTC_OFFER_PATH
            )
        if self._mode_set is not None:
            self._http.add_post_route(MODE_PATH, self._serve_mode_post)
            _LOGGER.info(
                "core API registered: POST %s (token auth)", MODE_PATH
            )
        if self._mode_get is not None:
            self._http.add_route(MODE_PATH, self._serve_mode_get)
            _LOGGER.info(
                "core API registered: GET %s (token auth)", MODE_PATH
            )
        _LOGGER.info(
            "core API registered: POST %s, POST %s (token auth)",
            TOOL_PATH,
            ANNOUNCE_PATH,
        )

    # -- auth ------------------------------------------------------------

    def _authorized(self, query: dict) -> bool:
        supplied = (query.get("t") or [None])[0]
        return bool(supplied) and hmac.compare_digest(supplied, self._token)

    # -- /tool -------------------------------------------------------------

    def _serve_tool(self, query: dict, body: bytes) -> tuple[int, str, bytes]:
        if not self._authorized(query):
            _LOGGER.warning("core API /tool: rejected (bad token)")
            return _json(403, {"ok": False, "error": "forbidden"})
        try:
            payload = _parse_json_body(body)
            tool, action, args = self._validate_tool_payload(payload)
        except _BadRequest as exc:
            _LOGGER.warning("core API /tool: bad request: %s", exc)
            return _json(400, {"ok": False, "error": str(exc)})
        if self._loop is None:
            return _json(503, {"ok": False, "error": "core API not running"})
        _LOGGER.info("core API /tool: tool=%s action=%s", tool, action)
        future = asyncio.run_coroutine_threadsafe(
            self._run_tool(tool, action, args), self._loop
        )
        try:
            result = future.result(timeout=_TOOL_TIMEOUT_S)
        except _BadRequest as exc:
            _LOGGER.warning("core API /tool: bad request: %s", exc)
            return _json(400, {"ok": False, "error": str(exc)})
        except concurrent.futures.TimeoutError:
            # Cancel before answering: the call must not execute (or
            # finish executing) after we have told the caller it
            # timed out, and a retry must not double it (item B).
            future.cancel()
            _LOGGER.error(
                "core API /tool: %s %s timed out; future cancelled",
                tool, action,
            )
            return _json(504, {"ok": False, "error": "tool call timed out"})
        except Exception as exc:
            _LOGGER.error("core API /tool: %s %s failed: %s", tool, action, exc)
            return _json(500, {"ok": False, "error": "tool call failed"})
        _LOGGER.info(
            "core API /tool: %s %s -> ok=%s", tool, action, result.ok
        )
        return _json(
            200,
            {
                "ok": result.ok,
                "confirmation": result.confirmation,
                "state": result.state,
            },
        )

    @staticmethod
    def _validate_tool_payload(payload: dict) -> tuple[str, str | None, dict]:
        tool = payload.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            raise _BadRequest("missing 'tool'")
        action = payload.get("action")
        if action is not None and not isinstance(action, str):
            raise _BadRequest("'action' must be a string")
        args = payload.get("args") or {}
        if not isinstance(args, dict):
            raise _BadRequest("'args' must be an object")
        return (
            tool.strip().lower(),
            action.strip().lower() if action else None,
            args,
        )

    async def _run_tool(
        self, tool: str, action: str | None, args: dict
    ) -> ToolResult:
        if tool == "light":
            if action in (None, "state"):
                return await self._light_state()
            if action == "on":
                return await self._light.turn_on()
            if action == "off":
                return await self._light.turn_off()
            if action in ("dim", "set_brightness"):
                return await self._light.set_brightness(_arg_percent(args))
            raise _BadRequest(f"unknown light action: {action!r}")
        if tool == "alarm":
            if action in (None, "summary"):
                return self._alarm_summary()
            if action == "set":
                hhmm = args.get("time")
                if not isinstance(hhmm, str) or not hhmm.strip():
                    raise _BadRequest("args.time ('HH:MM') is required")
                return await self._alarm.set_alarm(hhmm.strip())
            if action == "kill":
                return await self._alarm.kill()
            if action == "snooze":
                return await self._alarm.snooze()
            if action == "enable":
                return await self._alarm.set_enabled(True)
            if action == "disable":
                return await self._alarm.set_enabled(False)
            if action == "note_get":
                return await self._alarm.get_morning_note()
            if action == "note_set":
                text = args.get("text")
                day = args.get("date")
                if not isinstance(text, str) or not text.strip():
                    raise _BadRequest("args.text is required")
                if not isinstance(day, str) or not day.strip():
                    raise _BadRequest("args.date ('YYYY-MM-DD') is required")
                return await self._alarm.set_morning_note(
                    text.strip(), day.strip()
                )
            if action == "note_clear":
                return await self._alarm.clear_morning_note()
            raise _BadRequest(f"unknown alarm action: {action!r}")
        if tool == "time":
            return self._clock.time_result()
        if tool == "date":
            return self._clock.date_result()
        if tool == "clock":
            if action == "date":
                return self._clock.date_result()
            return self._clock.time_result()
        if tool == "weather":
            return await self._weather.report()
        if tool == "tv":
            if self._tv is None:
                raise _BadRequest("tv control is not wired in this core")
            if action in (None, "get", "state"):
                return await self._tv.get_brightness()
            if action == "set":
                return await self._tv.set_brightness(_arg_percent(args))
            if action == "adjust":
                delta = args.get("delta_points", args.get("delta"))
                if isinstance(delta, bool) or not isinstance(delta, int):
                    raise _BadRequest("args.delta_points (integer) is required")
                return await self._tv.adjust_brightness(delta)
            raise _BadRequest(f"unknown tv action: {action!r}")
        if tool == "tv_volume":
            if self._tv_volume is None:
                raise _BadRequest("tv volume control is not wired in this core")
            if action in (None, "get", "state"):
                return await self._tv_volume.get_volume()
            if action == "set":
                return await self._tv_volume.set_volume(_arg_percent(args))
            if action == "adjust":
                delta = args.get("delta_points", args.get("delta"))
                if isinstance(delta, bool) or not isinstance(delta, int):
                    raise _BadRequest("args.delta_points (integer) is required")
                return await self._tv_volume.adjust_volume(delta)
            raise _BadRequest(f"unknown tv_volume action: {action!r}")
        if tool == "jellyfin":
            if self._jellyfin is None:
                raise _BadRequest("jellyfin is not wired in this core")
            if action in (None, "status"):
                return await self._jellyfin.status()
            if action == "find":
                query = args.get("query")
                if not isinstance(query, str) or not query.strip():
                    raise _BadRequest("args.query is required")
                return await self._jellyfin.find(query.strip())
            if action == "play":
                title = args.get("title") or args.get("query")
                if not isinstance(title, str) or not title.strip():
                    raise _BadRequest("args.title is required")
                return await self._jellyfin.play(title.strip())
            if action in ("pause", "resume", "stop", "skip_forward", "skip_back"):
                return await self._jellyfin.control(action)
            raise _BadRequest(f"unknown jellyfin action: {action!r}")
        raise _BadRequest(f"unknown tool: {tool!r}")

    async def _light_state(self) -> ToolResult:
        state = await self._light.get_state()
        if state is None:
            return ToolResult(False, "rejected: light unreachable", {})
        if state.get("on"):
            brightness = state.get("brightness")
            if isinstance(brightness, int):
                return ToolResult(
                    True,
                    f"light state: on=true brightness={brightness} percent",
                    {"on": True, "brightness": brightness},
                )
            return ToolResult(True, "light state: on=true", {"on": True})
        return ToolResult(True, "light state: on=false", {"on": False})

    def _alarm_summary(self) -> ToolResult:
        summary = self._alarm.summary()
        if summary.get("ringing"):
            text = "alarm state: ringing=true"
        elif summary.get("enabled"):
            text = f"alarm state: enabled=true time={summary.get('time')}"
        else:
            text = "alarm state: enabled=false"
        return ToolResult(True, text, summary)

    # -- /announce ----------------------------------------------------------

    def _serve_announce(self, query: dict, body: bytes) -> tuple[int, str, bytes]:
        if not self._authorized(query):
            _LOGGER.warning("core API /announce: rejected (bad token)")
            return _json(403, {"ok": False, "error": "forbidden"})
        try:
            payload = _parse_json_body(body)
            text = self._validate_announce_text(payload)
        except _BadRequest as exc:
            _LOGGER.warning("core API /announce: bad request: %s", exc)
            return _json(400, {"ok": False, "error": str(exc)})
        if self._loop is None:
            return _json(503, {"ok": False, "error": "core API not running"})
        _LOGGER.info("core API /announce: %d chars", len(text))
        future = asyncio.run_coroutine_threadsafe(self._announce(text), self._loop)
        try:
            future.result(timeout=_ANNOUNCE_TIMEOUT_S)
        except AnnounceUnavailable as exc:
            _LOGGER.warning("core API /announce: unavailable: %s", exc)
            return _json(503, {"ok": False, "error": str(exc)})
        except concurrent.futures.TimeoutError:
            # As with /tool: cancel before answering, so a timed-out
            # announce does not speak late (item B).
            future.cancel()
            _LOGGER.error("core API /announce: timed out; future cancelled")
            return _json(504, {"ok": False, "error": "announce timed out"})
        except Exception as exc:
            _LOGGER.error("core API /announce: failed: %s", exc)
            return _json(500, {"ok": False, "error": "announce failed"})
        _LOGGER.info("core API /announce: spoken")
        return _json(200, {"ok": True, "announced": text})

    # -- /rtc/offer ------------------------------------------------
    # WebRTC signaling for RTC mode (2026-11-16): the TV page (via
    # the lyra-web proxy) posts its SDP offer; the composition's
    # RTC server answers. Mode gating lives in the composition's
    # callback -- outside RTC mode it returns an error dict and
    # the offer is refused with its status.

    def _serve_rtc_offer(self, query: dict, body: bytes) -> tuple[int, str, bytes]:
        return self._serve_offer(query, body, self._rtc_offer, "/rtc/offer")

    def _serve_offer(self, query: dict, body: bytes,
                     offer_fn, path: str) -> tuple[int, str, bytes]:
        if not self._authorized(query):
            _LOGGER.warning("core API %s: rejected (bad token)", path)
            return _json(403, {"ok": False, "error": "forbidden"})
        _LOGGER.info("core API %s: offer received (%d bytes)", path, len(body))
        if offer_fn is None or self._loop is None:
            return _json(503, {"ok": False, "error": "offer not available"})
        try:
            payload = _parse_json_body(body)
        except _BadRequest as exc:
            return _json(400, {"ok": False, "error": str(exc)})
        sdp = payload.get("sdp")
        if not isinstance(sdp, str) or not sdp:
            return _json(400, {"ok": False, "error": "sdp required"})
        sdp_type = payload.get("type") or "offer"
        future = asyncio.run_coroutine_threadsafe(
            offer_fn(sdp, str(sdp_type)), self._loop)
        try:
            result = future.result(timeout=_RTC_OFFER_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            future.cancel()
            _LOGGER.error("core API %s: timed out; future cancelled", path)
            return _json(504, {"ok": False, "error": "offer timed out"})
        except Exception as exc:
            _LOGGER.error("core API %s: failed: %s", path, exc)
            return _json(500, {"ok": False, "error": "offer failed"})
        if isinstance(result, dict) and result.get("error"):
            return _json(int(result.get("status") or 409),
                         {"ok": False, "error": result["error"]})
        return _json(200, {"ok": True, **result})

    # -- /mode ------------------------------------------------------
    # Mode ownership (2026-11-19): the core owns mode state. POST
    # persists + applies the requested mode through the
    # composition's callback and answers with the applied mode;
    # GET reports the applied mode. Same token discipline as
    # every route here.

    def _serve_mode_get(self, query: dict) -> tuple[int, str, bytes]:
        if not self._authorized(query):
            _LOGGER.warning("core API /mode: rejected (bad token)")
            return _json(403, {"ok": False, "error": "forbidden"})
        if self._mode_get is None:
            return _json(503, {"ok": False, "error": "mode not available"})
        return _json(200, {"ok": True, "mode": self._mode_get()})

    def _serve_mode_post(self, query: dict, body: bytes) -> tuple[int, str, bytes]:
        if not self._authorized(query):
            _LOGGER.warning("core API /mode: rejected (bad token)")
            return _json(403, {"ok": False, "error": "forbidden"})
        try:
            payload = _parse_json_body(body)
        except _BadRequest as exc:
            return _json(400, {"ok": False, "error": str(exc)})
        mode = payload.get("mode")
        if not isinstance(mode, str) or not mode.strip():
            return _json(400, {"ok": False, "error": "missing 'mode'"})
        if self._mode_set is None or self._loop is None:
            return _json(503, {"ok": False, "error": "mode not available"})
        mode = mode.strip().lower()
        _LOGGER.info("core API /mode: set %s", mode)
        future = asyncio.run_coroutine_threadsafe(
            self._mode_set(mode), self._loop)
        try:
            applied = future.result(timeout=_MODE_TIMEOUT_S)
        except ValueError as exc:
            _LOGGER.warning("core API /mode: bad mode: %s", exc)
            return _json(400, {"ok": False, "error": str(exc)})
        except concurrent.futures.TimeoutError:
            # As with /tool: cancel before answering, so a timed-out
            # transition does not land after the caller gave up.
            future.cancel()
            _LOGGER.error("core API /mode: timed out; future cancelled")
            return _json(504, {"ok": False, "error": "mode change timed out"})
        except Exception as exc:
            _LOGGER.error("core API /mode: failed: %s", exc)
            return _json(500, {"ok": False, "error": "mode change failed"})
        _LOGGER.info("core API /mode: applied %s", applied)
        return _json(200, {"ok": True, "mode": applied})
