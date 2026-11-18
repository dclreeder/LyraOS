"""Phase 4 core API: Lyra's hands on the core.

Two authenticated POST routes on the core's HTTP endpoint (:8099),
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
            enable | disable | ring_volume_get |
            ring_volume_set {percent | volume} |
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

- POST /call/offer  (WebRTC signaling for the phone call's
  streaming transport, 2026-11-18: the call page is a peer of the
  composition's call RTC server; NOT mode-gated -- a call is not
  a mode)
- POST /call/start  (raw WAV utterance in the body; returns at once
  with a call id once the utterance is transcribed)
- GET /call/poll  (the call turn's reply sentences, synthesized and
  delivered one by one as they are generated)
  One dashboard-call turn (2026-11-15): the composition's call
  callback runs streaming Nemotron (Parakeet TDT
  fallback) -> the free router -> Kokoro and returns
  {"ok": true, "transcript": ..., "reply": ..., "wav_b64": ...}.
  The reply WAV also reaches the TV through synthesize_speech's
  mirror, like every spoken line. This route carries a larger
  per-route body cap (whole utterances, not JSON).

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
import base64
import concurrent.futures
import hmac
import json
import logging
from collections.abc import Awaitable, Callable

from .audio_http import AudioHttpServer
from .tools import (
    AlarmTool,
    ClockTool,
    LightTool,
    ToolResult,
    TvTool,
    TvVolumeTool,
    WeatherTool,
)

_LOGGER = logging.getLogger("lyra_core.api")

TOOL_PATH = "/tool"
ANNOUNCE_PATH = "/announce"
CALL_START_PATH = "/call/start"
CALL_OFFER_PATH = "/call/offer"
RTC_OFFER_PATH = "/rtc/offer"
CALL_POLL_PATH = "/call/poll"
ANNOUNCE_MAX_CHARS = 200
_TOOL_TIMEOUT_S = 90.0
_ANNOUNCE_TIMEOUT_S = 75.0
_CALL_START_TIMEOUT_S = 60.0
_RTC_OFFER_TIMEOUT_S = 20.0
_CALL_MAX_BODY_BYTES = 8 * 1024 * 1024

# The composition's announce: text -> None on success; raises
# AnnounceUnavailable when there is no way to speak.
AnnounceFn = Callable[[str], Awaitable[None]]

# The composition's call turn: utterance WAV bytes ->
# (reply WAV bytes, transcript, reply text). Raises on failure.
CallStartFn = Callable[[bytes], Awaitable[dict]]
CallPollFn = Callable[[str, int], "dict | None"]

# RTC signaling callback: an SDP offer -> the SDP answer dict, or
# a dict with "error" (plus optional "status"). Raises on failure.
RtcOfferFn = Callable[[str, str], Awaitable[dict]]


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
    """POST /tool + POST /announce over the router's own tools."""

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
        call_start: CallStartFn | None = None,
        rtc_offer: RtcOfferFn | None = None,
        call_offer: RtcOfferFn | None = None,
        call_poll: CallPollFn | None = None,
        tv: TvTool | None = None,
        tv_volume: TvVolumeTool | None = None,
    ) -> None:
        self._http = http
        self._token = token
        self._light = light
        self._alarm = alarm_tool
        self._clock = clock
        self._weather = weather
        self._announce = announce
        self._call_start = call_start
        self._rtc_offer = rtc_offer
        self._call_offer = call_offer
        self._call_poll = call_poll
        self._tv = tv
        self._tv_volume = tv_volume
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        """Capture the core's event loop and register the routes."""
        self._loop = asyncio.get_running_loop()
        self._http.add_post_route(TOOL_PATH, self._serve_tool)
        self._http.add_post_route(ANNOUNCE_PATH, self._serve_announce)
        if self._call_start is not None and self._call_poll is not None:
            self._http.add_post_route(
                CALL_START_PATH,
                self._serve_call_start,
                max_body_bytes=_CALL_MAX_BODY_BYTES,
            )
            self._http.add_route(CALL_POLL_PATH, self._serve_call_poll)
            _LOGGER.info(
                "core API registered: POST %s, GET %s (token auth)",
                CALL_START_PATH,
                CALL_POLL_PATH,
            )
        if self._rtc_offer is not None:
            self._http.add_post_route(RTC_OFFER_PATH, self._serve_rtc_offer)
            _LOGGER.info(
                "core API registered: POST %s (token auth)", RTC_OFFER_PATH
            )
        if self._call_offer is not None:
            self._http.add_post_route(
                CALL_OFFER_PATH, self._serve_call_offer)
            _LOGGER.info(
                "core API registered: POST %s (token auth)",
                CALL_OFFER_PATH
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
            if action == "ring_volume_get":
                return await self._alarm.get_ring_volume()
            if action == "ring_volume_set":
                return await self._alarm.set_ring_volume(
                    self._volume_arg(args)
                )
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
        raise _BadRequest(f"unknown tool: {tool!r}")

    @staticmethod
    def _volume_arg(args: dict) -> float:
        """A volume argument for ring_volume_set: args.percent
        (int 0-100) or args.volume (0.0-1.0)."""
        if "percent" in args:
            return _arg_percent(args) / 100.0
        value = args.get("volume")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _BadRequest(
                "args.percent (0-100) or args.volume (0.0-1.0) is required"
            )
        if not 0.0 <= float(value) <= 1.0:
            raise _BadRequest("args.volume out of range 0.0-1.0")
        return float(value)

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

    # -- /call/start + /call/poll ---------------------------------------
    # Streamed call turns (2026-11-15): /call/start transcribes the
    # utterance and returns a call id immediately; the turn itself
    # runs in the background and its reply is synthesized sentence by
    # sentence. /call/poll hands the caller the sentences it has not
    # seen yet, so the caller starts hearing her after roughly one
    # sentence instead of the whole reply (the old /call/turn shape).

    def _serve_call_start(self, query: dict, body: bytes) -> tuple[int, str, bytes]:
        if not self._authorized(query):
            _LOGGER.warning("core API /call/start: rejected (bad token)")
            return _json(403, {"ok": False, "error": "forbidden"})
        if self._call_start is None or self._loop is None:
            return _json(503, {"ok": False, "error": "call not available"})
        if len(body) < 44 or not body.startswith(b"RIFF"):
            return _json(400, {"ok": False, "error": "body must be a WAV utterance"})
        _LOGGER.info("core API /call/start: %d bytes in", len(body))
        future = asyncio.run_coroutine_threadsafe(self._call_start(body), self._loop)
        try:
            result = future.result(timeout=_CALL_START_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            future.cancel()
            _LOGGER.error("core API /call/start: timed out; future cancelled")
            return _json(504, {"ok": False, "error": "call start timed out"})
        except Exception as exc:
            _LOGGER.error("core API /call/start: failed: %s", exc)
            return _json(500, {"ok": False, "error": "call start failed"})
        if result.get("busy"):
            return _json(409, {"ok": False, "error": "a call turn is already in progress"})
        return _json(200, {"ok": True, **result})

    def _serve_call_poll(self, query: dict) -> tuple[int, str, bytes]:
        if not self._authorized(query):
            _LOGGER.warning("core API /call/poll: rejected (bad token)")
            return _json(403, {"ok": False, "error": "forbidden"})
        if self._call_poll is None:
            return _json(503, {"ok": False, "error": "call not available"})
        call_id = (query.get("call") or [None])[0]
        if not call_id:
            return _json(400, {"ok": False, "error": "call id required"})
        try:
            after = int((query.get("after") or ["0"])[0])
        except (TypeError, ValueError):
            return _json(400, {"ok": False, "error": "bad after"})
        result = self._call_poll(call_id, max(0, after))
        if result is None:
            return _json(404, {"ok": False, "error": "unknown call"})
        return _json(200, {"ok": True, **result})

    # -- /rtc/offer ------------------------------------------------
    # WebRTC signaling for RTC mode (2026-11-16): the TV page (via
    # the lyra-web proxy) posts its SDP offer; the composition's
    # RTC server answers. Mode gating lives in the composition's
    # callback -- outside RTC mode it returns an error dict and
    # the offer is refused with its status.

    def _serve_rtc_offer(self, query: dict, body: bytes) -> tuple[int, str, bytes]:
        return self._serve_offer(query, body, self._rtc_offer, "/rtc/offer")

    def _serve_call_offer(self, query: dict, body: bytes) -> tuple[int, str, bytes]:
        return self._serve_offer(query, body, self._call_offer, "/call/offer")

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
