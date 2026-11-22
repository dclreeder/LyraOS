"""Free-mode router (trial, 2026-11-11): a frontier model runs the turn.

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

Backend (2026-11-16): Muse Spark 1.3 via the Meta Model API
(OpenAI-compatible chat completions, reasoning effort low),
replacing the local Qwen3.5-9B on llama-server; the local model
was shut down and removed the same night at Daniel's order, so
there is no local fallback. The 12-turn rolling history limit
was removed at the same time: session history is sent in full
(Spark's context is ~1M tokens), bounded only by the idle clear
and a very large token safety valve (HISTORY_MAX_TOKENS).
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

# Turn deadline: 10s in the local-server era; 30s since the
# 2026-11-16 move to the Meta Model API, whose observed tail
# latency can pass 10s on a single tool round.
FREE_DEADLINE_S = 30.0
# History has no turn limit since 2026-11-16 (Daniel's order):
# the full session history goes to the model. This token
# estimate (chars/4, see _history_tokens) is only a safety valve
# against absurd growth, far under Spark's ~1M-token context.
HISTORY_MAX_TOKENS = 800_000
HISTORY_IDLE_CLEAR_S = 1800.0
# Per-round reply budget: Spark spends part of it on reasoning
# tokens, and on post-tool rounds it reasons hard (~800 tokens
# observed at low effort for a trivial follow-up), so the local
# era's 300 -- or even 800 -- truncates real replies to empty.
# 4000 gives the reasoning room and still bounds a runaway reply.
MAX_TOKENS = 4000
MAX_TOOL_ROUNDS = 3
MAX_TOOL_CALLS = 6

_TZ = ZoneInfo("America/New_York")


def _speaker_label(verify) -> str:
    """The turn's speaker label from its verify result, sanitized.
    Only the two positive identifications survive; everything else
    (no gate, RTC, a failure) is 'unknown'."""
    label = getattr(verify, "speaker", None)
    return label if label in ("daniel", "guest") else "unknown"


def _stamped(command: str, speaker: str = "unknown") -> str:
    """Prefix a user turn with the moment it was spoken, e.g.
    '[10:32 AM, Sun Nov 15] what time is it?'. Added 2026-11-15
    after the model answered a time question with zero tool calls
    and an invented 10:23 at 10:32: the stamp is fresh data
    attached to THIS turn, and because the turn slice is what
    history stores, every past turn keeps its own stamp — an old
    time reads as an old moment, not as now."""
    who = {"daniel": " · Daniel", "guest": " · Guest"}.get(speaker, "")
    return f"[{datetime.now(_TZ):%I:%M %p, %a %b %d}{who}] {command}"


_SOUL_PATH = Path(__file__).resolve().parent.parent / "SOUL.md"


# One line, deliberately content-free. A second full brief used to
# live here as the fallback; Daniel ordered it removed 2026-11-19
# ("not having all these extra not needed parts") because duplicate
# instruction text drifts -- it still described the deleted speaker
# box a week after the real SOUL.md moved on. If SOUL.md ever fails
# to load, she runs on this line and the failure is logged as an
# error, loudly, instead of being papered over by a stale stand-in.
_SOUL_FALLBACK_LINE = "You are Lyra, Daniel's voice assistant."


def load_soul() -> str:
    """Her standing instructions: SOUL.md at the project root,
    re-read on every turn so editing the file changes her without
    a restart. Missing, unreadable, or empty: log an error and
    fall back to the single identity line."""
    try:
        text = _SOUL_PATH.read_text(encoding="utf-8").strip()
    except OSError as exc:
        _LOGGER.error("SOUL.md failed to load (%s): %s", _SOUL_PATH, exc)
        return _SOUL_FALLBACK_LINE
    if not text:
        _LOGGER.error("SOUL.md is empty (%s)", _SOUL_PATH)
        return _SOUL_FALLBACK_LINE
    return text




def _load_model_api_key(cfg) -> str | None:
    """Read the Meta Model API key from Daniel's central APIKeys
    .env (path/variable from RouterConfig). The value lives only
    in this process's memory: never logged, never in config."""
    path = Path(
        getattr(cfg, "api_key_path", "~/Documents/APIKeys/.env")
    ).expanduser()
    var = getattr(cfg, "api_key_var", "MUSE_MODEL_API_KEY")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export "):]
        if line.startswith(var + "="):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value or None
    return None


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
    _fn("alarm_set", "Set the alarm time (24-hour HH:MM).",
        {"time": {"type": "string", "description": "HH:MM, 24-hour."}}, ["time"]),
    _fn("alarm_enable", "Enable the alarm.", {}, []),
    _fn("alarm_disable", "Disable the alarm.", {}, []),
    _fn("alarm_kill", "Kill the alarm: stops a ring, or cancels a pending snooze.", {}, []),
    _fn("alarm_snooze", "Snooze the alarm (only works while it is ringing).", {}, []),
    _fn("alarm_get_summary", "Get the alarm's time, enabled state, repeat and whether it is ringing.", {}, []),
    _fn("time_now", "Get the current time.", {}, []),
    _fn("date_today", "Get today's date.", {}, []),
    _fn("weather_report", "Get weather data for home; compose the spoken answer from the data. Pick the period that matches the question: overview for a general ask, now for the next few hours, today/tomorrow for that day, week for the days ahead. Use focus when the question is mainly about rain, temperature, or wind.", {"period": {"type": "string", "enum": ["overview", "now", "today", "tomorrow", "week"]}, "focus": {"type": "string", "enum": ["overview", "rain", "temperature", "wind"]}}, []),
    _fn("tv_set_brightness", "Set the brightness of the TV screen that shows your avatar. 100 is full brightness; lower dims the screen, for example at night.", {"percent": _PCT}, ["percent"]),
    _fn("tv_adjust_brightness", "Change the TV screen brightness by signed points.", {"delta_points": _DELTA}, ["delta_points"]),
    _fn("tv_get_brightness", "Get the TV screen's current brightness.", {}, []),
    _fn("tv_set_volume", "Set the volume of the TV's own speakers. This is the television's volume, not the screen brightness.", {"percent": _PCT}, ["percent"]),
    _fn("tv_adjust_volume", "Change the TV's speaker volume by signed points.", {"delta_points": _DELTA}, ["delta_points"]),
    _fn("tv_get_volume", "Get the TV's current speaker volume.", {}, []),
    _fn("jellyfin_find", "Search the Jellyfin media library for a movie or TV show by title. Returns the matching titles with type, year, and watched state.", {"query": {"type": "string", "description": "Title or part of a title to search for."}}, ["query"]),
    _fn("jellyfin_play", "Play a movie or TV show from Jellyfin on the TV. Pass the title as the user named it. For a show, this plays the next unwatched episode or resumes the episode in progress.", {"title": {"type": "string", "description": "The movie or show title."}}, ["title"]),
    _fn("jellyfin_control", "Control the Jellyfin playback on the TV.", {"action": {"type": "string", "enum": ["pause", "resume", "stop", "skip_forward", "skip_back"], "description": "pause, resume, stop, skip_forward (30 seconds ahead), or skip_back (15 seconds back)."}}, ["action"]),
    _fn("jellyfin_status", "Report what Jellyfin is doing: what is playing on the TV and how far in, or what is in progress to continue watching.", {}, []),
    _fn("jellyfin_fullscreen", "Put the Jellyfin player fullscreen on the TV screen. Use when the user asks for fullscreen or the picture is windowed.", {}, []),
    _fn("memory_lookup", "Look up what you remember about a person, a project, a place, or a topic -- things Daniel has told you or that came up before, beyond this conversation. Use it when an answer depends on something from the past rather than the live world.", {"query": {"type": "string", "description": "Who or what to look up, in plain words."}}, ["query"]),
    _fn("memory_note", "Write a note in your memory notebook, the moment it happens -- this is how things you learn in conversation reach your long-term memory. Use kind 'correction' the moment Daniel corrects a fact (something you believed, said, or had stored): text states the corrected fact, corrects names the wrong belief it replaces. A correction note outranks your stored memory immediately. Use kind 'proposal' when Daniel asks you to remember something, or tells you something plainly worth keeping. Filing a note is not the memory itself -- the curator weighs proposals tonight; corrections are applied as Daniel's word.", {"kind": {"type": "string", "enum": ["correction", "proposal"], "description": "correction when Daniel sets a fact right; proposal for something that should become memory."}, "text": {"type": "string", "description": "The note itself, in plain words: the corrected fact, or the thing to remember."}, "corrects": {"type": "string", "description": "Corrections only: the wrong belief this replaces, in plain words."}, "subject": {"type": "string", "description": "Optional: who or what the note is about."}}, ["kind", "text"]),
]


class FreeChatClient:
    """Chat-completions client for free mode: stdlib-urllib-in-
    a-thread, tool-capable, talking to the Meta Model API
    (OpenAI-compatible) with bearer auth. The per-call socket
    timeout is whatever is left of the turn budget, so a wedged
    server can never hang a turn past its deadline."""

    def __init__(self, url: str, model: str, *, api_key: str | None = None,
                 reasoning_effort: str = "minimal") -> None:
        self._endpoint = url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._reasoning_effort = reasoning_effort

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    async def chat(self, messages: list, tools, timeout_s: float) -> dict:
        body = {
            "model": self._model,
            "temperature": 0.3,
            "max_tokens": MAX_TOKENS,
            "reasoning_effort": self._reasoning_effort,
            "messages": messages,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        raw = await asyncio.to_thread(self._post, body, max(1.0, timeout_s))
        try:
            msg = raw["choices"][0]["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError(f"unparseable chat reply: {exc}") from exc
        # Normalize to the fields the turn loop round-trips back to
        # the API (the raw reply also carries e.g. a refusal field
        # that must not be echoed into the next request).
        message: dict = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            message["tool_calls"] = msg["tool_calls"]
        return message

    def _post(self, body: dict, timeout_s: float) -> dict:
        req = urllib.request.Request(
            self._endpoint,
            data=json.dumps(body).encode(),
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return json.loads(resp.read().decode())
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LlmError(f"chat call failed: {exc}") from exc

    async def chat_stream(self, messages: list, tools, timeout_s: float, on_piece) -> dict:
        """Streaming variant of chat(): content pieces go to the
        async on_piece callback as they arrive; the assembled
        message (same shape chat() returns) is the result. The
        urllib read runs in a thread and hands pieces over through
        a queue, mirroring _post's pattern."""
        body = {
            "model": self._model,
            "temperature": 0.3,
            "max_tokens": MAX_TOKENS,
            "reasoning_effort": self._reasoning_effort,
            "messages": messages,
            "stream": True,
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _pump() -> None:
            try:
                self._post_stream(
                    body, max(1.0, timeout_s),
                    lambda item: loop.call_soon_threadsafe(queue.put_nowait, item),
                )
            except Exception as exc:
                loop.call_soon_threadsafe(queue.put_nowait, ("error", exc))

        asyncio.ensure_future(asyncio.to_thread(_pump))
        content_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        pieces = 0
        while True:
            kind, payload = await queue.get()
            if kind == "done":
                break
            if kind == "error":
                raise LlmError(f"chat stream failed: {payload}")
            if kind == "content":
                pieces += 1
                content_parts.append(payload)
                if on_piece is not None:
                    await on_piece(payload)
            elif kind == "tool":
                idx, frag = payload
                slot = tool_calls.setdefault(
                    idx, {"id": "", "name": "", "arguments": ""})
                if frag.get("id"):
                    slot["id"] = frag["id"]
                fn = frag.get("function") or {}
                if fn.get("name"):
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["arguments"] += fn["arguments"]
        message: dict = {"role": "assistant", "content": "".join(content_parts)}
        if tool_calls:
            message["tool_calls"] = [
                {
                    "id": slot["id"] or f"call-{i}",
                    "type": "function",
                    "function": {
                        "name": slot["name"],
                        "arguments": slot["arguments"],
                    },
                }
                for i, slot in sorted(tool_calls.items())
            ]
        return message

    def _post_stream(self, body: dict, timeout_s: float, emit) -> None:
        req = urllib.request.Request(
            self._endpoint,
            data=json.dumps(body).encode(),
            headers=self._headers(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                for raw_line in resp:
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk["choices"][0].get("delta") or {}
                    except (ValueError, KeyError, IndexError, TypeError):
                        continue
                    if delta.get("content"):
                        emit(("content", delta["content"]))
                    for frag in delta.get("tool_calls") or []:
                        emit(("tool", (frag.get("index", 0), frag)))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise LlmError(f"chat stream failed: {exc}") from exc
        emit(("done", None))


_WS = re.compile(r"\s+")

_MD_UNDERSCORE = re.compile(r"(?<!\w)_+|_+(?!\w)")
_MD_LINE_LEAD = re.compile(r"^\s*(?:#{1,6}\s+|[-\u2022]\s+)")


def sanitize_spoken(text: str) -> str:
    """The one speech-preparation pass. Every synthesizer-bound
    emission goes through here: whole lines directly, and every
    sentence the splitter releases (feed, long-fragment cut, and
    flush). It returns the text cleaned for synthesis, or '' when
    nothing speakable remains (callers treat '' as a drop, never
    an error).

    - Markdown is stripped. The brain still emits *emphasis*,
      **bold**, *stage directions* and `code` ticks despite the
      SOUL's no-markdown rule, and the synthesizer reads the '*'
      character aloud as the word 'asterisk' (Daniel, live,
      2026-11-18). The wrapped words stay; only the markup
      characters go. Emphasis underscores come off at word
      boundaries only -- intra-word underscores (alarm_set) are
      part of the word and stay. Line-leading headers and
      bullets are stripped. Captions and page text keep her raw
      reply -- this runs only where text is bound for synthesis.
    - Whitespace runs collapse to single spaces.
    - A fragment carrying no letter or digit -- markup-only, or
      an emoji-only trailing fragment (the observed case,
      2026-11-18) -- comes back ''. Such fragments strip to
      nothing at the TTS stage and raise there; they must never
      be emitted as sentences or spoken lines in the first place.

    Pipeline necessity only: the TTS stage needs a non-empty
    string. The model's words are NOT trimmed, capped, or
    corrected -- in the free-reign trial her output is the test
    data."""
    if not text:
        return ""
    text = text.replace("*", "").replace("`", "").replace("\u2022", "")
    text = _MD_UNDERSCORE.sub("", text)
    text = _MD_LINE_LEAD.sub("", text)
    text = _WS.sub(" ", text).strip()
    if not any(ch.isalnum() for ch in text):
        return ""
    return text



def _as_int(value) -> int:
    return int(round(float(value)))


_SENTENCE_END = re.compile(r"[.!?\u2026]+[\"\)\]]*(?:\s+|$)")
_ABBREV_TAILS = (
    "p.m", "a.m", "e.g", "i.e", "mr", "mrs", "ms", "dr", "st", "vs",
    "u.s", "u.s.a", "approx", "no",
)


class _SentenceSplitter:
    """Splits streamed reply text into speakable sentences. A
    candidate is released only once the NEXT candidate exists (or
    the round ends tool-free and flush() is called), so a round
    that turns out to be tool calls never gets its text spoken --
    at most a genuine preamble sentence can precede a tool round,
    which this model does not produce in practice."""

    def __init__(self) -> None:
        self._buf = ""
        self.pending: list[str] = []

    def feed(self, piece: str) -> None:
        self._buf += piece
        start = 0
        while True:
            m = _SENTENCE_END.search(self._buf, start)
            if not m:
                break
            sent = self._buf[:m.end()].strip()
            tail = sent.rstrip('."\')\]').lower()
            if len(sent) < 4 or any(tail.endswith(a) for a in _ABBREV_TAILS):
                start = m.end()
                continue
            sent = sanitize_spoken(sent)
            if sent:
                self.pending.append(sent)
            self._buf = self._buf[m.end():]
            start = 0
        if len(self._buf) > 240:
            cut = self._buf.rfind(", ", 40)
            if cut > 0:
                frag = sanitize_spoken(self._buf[:cut + 1])
                if frag:
                    self.pending.append(frag)
                self._buf = self._buf[cut + 2:]

    def release_ready(self) -> list[str]:
        if len(self.pending) <= 1:
            return []
        out, self.pending = self.pending[:-1], self.pending[-1:]
        return out

    def flush(self) -> list[str]:
        out = list(self.pending)
        self.pending = []
        tail = sanitize_spoken(self._buf)
        self._buf = ""
        if tail:
            out.append(tail)
        return out


async def _stream_round(client, messages, tools, timeout_s, on_sentence):
    """One streamed completion round. Sentences are fed to
    on_sentence as they complete; if the round ends in tool calls,
    whatever is still buffered is dropped unspoken (the tool round's
    text is not a reply, same as the non-streaming path)."""
    splitter = _SentenceSplitter()
    seen = {"content": False, "released": False}

    async def _on_piece(piece: str) -> None:
        seen["content"] = True
        splitter.feed(piece)
        for sent in splitter.release_ready():
            seen["released"] = True
            await on_sentence(sent)

    try:
        message = await client.chat_stream(messages, tools, timeout_s, _on_piece)
    except LlmError:
        if seen["content"] or seen["released"]:
            raise
        # Nothing arrived at all: fall back to the plain call.
        return await client.chat(messages, tools, timeout_s)
    if message.get("tool_calls"):
        return message
    for sent in splitter.flush():
        await on_sentence(sent)
    return message


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
        alarm_tool,
        clock,
        weather,
        tv=None,
        tv_volume=None,
        jellyfin=None,
        inbox=None,
        notebook=None,
        memory=None,
        client=None,
    ) -> None:
        self._cfg = cfg
        self._light = light
        self._alarm = alarm_tool
        self._clock = clock
        self._weather = weather
        self._tv = tv
        self._tv_volume = tv_volume
        self._jellyfin = jellyfin
        if client is None:
            api_key = _load_model_api_key(cfg)
            if not api_key:
                raise LlmError(
                    "Meta Model API key not found: "
                    f"{getattr(cfg, 'api_key_var', 'MUSE_MODEL_API_KEY')} in "
                    f"{getattr(cfg, 'api_key_path', '~/Documents/APIKeys/.env')}"
                )
            client = FreeChatClient(
                cfg.llm_url,
                cfg.llm_model,
                api_key=api_key,
                reasoning_effort=getattr(cfg, "reasoning_effort", "minimal"),
            )
        self._client = client
        self._history: list[list[dict]] = []
        self._inbox = inbox
        self._notebook = notebook
        self._memory = memory
        self._surfaced: set = set()
        self._last_slice: list = []
        self._last_turn_at: float | None = None

    def describe(self) -> str:
        return (
            f"FREE router: uncaged model via {self._cfg.llm_model} "
            f"@ {self._cfg.llm_url}, {len(TOOL_SCHEMAS)} tools, "
            f"deadline {FREE_DEADLINE_S:.0f}s"
        )

    def _turn_deadline_s(self) -> float:
        return FREE_DEADLINE_S

    # -- the hook ------------------------------------------------------

    async def handle(self, command: str, verify=None, on_sentence=None, via: str = "box") -> object:
        reply = await self._handle_impl(command, verify=verify, on_sentence=on_sentence)
        self._log_turn(command, reply, via, _speaker_label(verify))
        return reply

    def _log_turn(self, command: str, reply: object, via: str,
                  speaker: str = "unknown") -> None:
        """Phase 0 inbox (memory build): one entry per completed
        turn, with the turn's tool calls and their results lifted
        from the turn slice. Written by code from what actually
        happened; never raises."""
        if self._inbox is None:
            return
        slice_, self._last_slice = self._last_slice, []
        tools: list = []
        pending: list = []
        for msg in slice_:
            if msg.get("role") == "assistant":
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        args = {"_raw": fn.get("arguments")}
                    pending.append({"name": fn.get("name") or "?", "args": args})
            elif msg.get("role") == "tool" and pending:
                entry = pending.pop(0)
                try:
                    feedback = json.loads(msg.get("content") or "{}")
                except (ValueError, TypeError):
                    feedback = {}
                entry["ok"] = bool(feedback.get("ok"))
                entry["result"] = str(feedback.get("result") or "")
                tools.append(entry)
        tools.extend(pending)
        self._inbox.log({
            "type": "turn", "via": via, "speaker": speaker, "user": command,
            "reply": str(reply), "tools": tools,
        })

    async def _memory_block(self, command: str) -> str:
        """Memory build, Phase 1: the in-play list plus whatever
        comes to mind for this turn, rendered quietly into her
        context -- a view over the store, never a summary. An
        empty store yields an empty string, which is exactly the
        pre-memory behavior. Memory can never break a turn: any
        failure returns ""."""
        if self._memory is None:
            return ""
        try:
            inplay = await asyncio.to_thread(self._memory.inplay)
            inplay_ids = {c.get("id") for c in inplay if c.get("id")}
            ambient = await asyncio.to_thread(
                self._memory.ambient, command,
                inplay_ids | self._surfaced)
        except Exception as exc:
            _LOGGER.warning("memory block failed: %s", exc)
            return ""
        def _line(c: dict) -> str:
            tag = " [inferred]" if c.get("inference") else ""
            return f"- {c.get('statement')} ({c.get('learned')}){tag}"
        sections = []
        if inplay:
            lines = "\n".join(_line(c) for c in inplay)
            sections.append(
                "Things currently in play between you and Daniel:\n"
                + lines)
        if ambient:
            lines = "\n".join(_line(c) for c in ambient)
            sections.append("Things that come to mind:\n" + lines)
            self._surfaced.update(
                c.get("id") for c in ambient if c.get("id"))
        return "\n\n".join(sections)

    def log_note(self, entry: dict) -> None:
        """Notebook (memory build): the running brain's notes --
        proposals and corrections -- written by code to the
        per-day notes stream the moment they happen. The caller
        is the memory_note tool (Phase 3). The notebook is what
        supersedes (PLAN.md): this write is the record, never
        best-effort prose riding in context."""
        if self._notebook is not None:
            self._notebook.log({"type": "note", **entry})

    async def _handle_impl(self, command: str, verify=None, on_sentence=None) -> object:
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
            self._surfaced.clear()
        started = time.monotonic()
        speaker = _speaker_label(verify)
        raw_final = None
        turn_slice: list = []
        self._last_slice = []
        try:
            raw_final, calls, turn_slice = await asyncio.wait_for(
                self._run_turn(command, on_sentence, speaker),
                timeout=self._turn_deadline_s(),
            )
        except asyncio.TimeoutError:
            _LOGGER.error(
                "FREE_FAIL timeout after %.1fs for %r raw=%r",
                self._turn_deadline_s(), command, raw_final,
            )
            self._remember(command, "Sorry, something went wrong.", speaker)
            return "Sorry, something went wrong."
        except Exception as exc:
            _LOGGER.error(
                "FREE_FAIL %s for %r raw=%r", exc, command, raw_final
            )
            self._remember(command, "Sorry, something went wrong.", speaker)
            return "Sorry, something went wrong."
        self._last_slice = turn_slice
        text = sanitize_spoken(raw_final or "")
        if not text:
            _LOGGER.error(
                "FREE_FAIL empty/garbled final text for %r raw=%r",
                command, raw_final,
            )
            self._remember(command, NOT_UNDERSTOOD, speaker)
            return NOT_UNDERSTOOD
        self._remember_turn(turn_slice, text)
        _LOGGER.info(
            "free turn %r -> calls=%s final=%r (%.2fs, history=%d turns)",
            command, calls, text, time.monotonic() - started,
            len(self._history),
        )
        return text

    # -- conversation context (no turn limit; see _trim_history) --------------------------------

    def _remember(self, command: str, spoken: str,
                  speaker: str = "unknown") -> None:
        """Record a turn whose model exchange failed (a fallback
        line was spoken) in its honest minimal form."""
        self._history.append([
            {"role": "user", "content": _stamped(command, speaker)},
            {"role": "assistant", "content": spoken},
        ])
        self._last_turn_at = time.monotonic()
        self._trim_history()

    def _remember_turn(self, turn_slice: list, spoken: str) -> None:
        turn = list(turn_slice) + [{"role": "assistant", "content": spoken}]
        self._history.append(turn)
        self._last_turn_at = time.monotonic()
        self._trim_history()

    def _history_tokens(self) -> int:
        """Rough token estimate (chars/4) over message content and
        tool-call arguments -- feeds only the safety valve below."""
        chars = 0
        for turn in self._history:
            for m in turn:
                chars += len(str(m.get("content") or ""))
                for call in m.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    chars += len(str(fn.get("name") or ""))
                    chars += len(str(fn.get("arguments") or ""))
        return chars // 4

    def _trim_history(self) -> None:
        """No turn limit since 2026-11-16: the full session history
        goes to the model. The only trim left is the safety valve:
        drop the oldest turns when the estimate passes
        HISTORY_MAX_TOKENS (~800K, under Spark's ~1M context)."""
        while len(self._history) > 1 and self._history_tokens() > HISTORY_MAX_TOKENS:
            self._history.pop(0)

    async def _run_turn(self, command: str, on_sentence=None,
                        speaker: str = "unknown"):
        client = self._client
        deadline = time.monotonic() + FREE_DEADLINE_S
        history = [m for turn in self._history for m in turn]
        system_content = load_soul()
        # Model plumbing appended to her soul at turn time (kept
        # OUT of SOUL.md itself): the current brain (Nemotron Nano
        # 9B v2) thinks by default and takes "/no_think" in the
        # system prompt as its reasoning-off control. Driven by
        # RouterConfig.system_prompt_suffix; empty = no suffix.
        suffix = getattr(self._cfg, "system_prompt_suffix", "")
        if suffix:
            system_content = f"{system_content}\n\n{suffix}"
        mem_block = await self._memory_block(command)
        if mem_block:
            system_content = f"{system_content}\n\n{mem_block}"
        messages = [
            {"role": "system", "content": system_content},
            *history,
            {"role": "user", "content": _stamped(command, speaker)},
        ]
        slice_at = len(messages) - 1
        calls_made: list[str] = []
        total_calls = 0
        for round_no in range(MAX_TOOL_ROUNDS + 1):
            last_round = round_no == MAX_TOOL_ROUNDS
            if on_sentence is None:
                message = await client.chat(
                    messages,
                    None if last_round else TOOL_SCHEMAS,
                    deadline - time.monotonic(),
                )
            else:
                message = await _stream_round(
                    client,
                    messages,
                    None if last_round else TOOL_SCHEMAS,
                    deadline - time.monotonic(),
                    on_sentence,
                )
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                if (message.get("content") or "").strip() or last_round:
                    return message.get("content") or "", calls_made, messages[slice_at:]
                # Spark quirk (2026-11-16): a round -- typically the
                # one after a tool result -- occasionally completes
                # with no content at all (its reasoning consumed the
                # whole budget or it simply stops empty). Nothing was
                # spoken for the round, so re-ask it once.
                _LOGGER.warning(
                    "free turn: empty completion on round %d; retrying", round_no)
                continue
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
                f"ringing: {bool(summary.get('ringing'))}."
            )
            return {"ok": True, "result": line, "state": summary}
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
        if name.startswith("jellyfin_"):
            if self._jellyfin is None:
                return {"ok": False, "result": "Jellyfin is not wired in this core.", "state": {}}
            if name == "jellyfin_find":
                return self._wrap(await self._jellyfin.find(str(args.get("query", ""))))
            if name == "jellyfin_play":
                return self._wrap(await self._jellyfin.play(str(args.get("title", ""))))
            if name == "jellyfin_control":
                return self._wrap(await self._jellyfin.control(str(args.get("action", ""))))
            if name == "jellyfin_status":
                return self._wrap(await self._jellyfin.status())
            if name == "jellyfin_fullscreen":
                return self._wrap(await self._jellyfin.fullscreen())
        if name == "memory_lookup":
            if self._memory is None:
                return {"ok": False,
                        "result": "Memory is not wired in this core.",
                        "state": {}}
            query = str(args.get("query", ""))
            found = await asyncio.to_thread(self._memory.lookup, query)
            lines = []
            for note in found["corrections"]:
                lines.append(
                    f"Daniel corrected this: {note.get('text')} "
                    f"(noted {str(note.get('ts') or '')[:10]}, "
                    "pending curation -- trust this over older memory)")
            for c in found["claims"]:
                line = (f"{c.get('statement')} "
                        f"(learned {c.get('learned')}, "
                        f"confidence {c.get('confidence')}")
                if c.get("quote"):
                    line += f"; his words: \"{c['quote']}\""
                line += ")"
                if c.get("valid_to"):
                    line += f" [window ended {c['valid_to']}]"
                if c.get("corrected"):
                    line += " [a correction about this is pending]"
                if c.get("inference"):
                    line += (" [inferred -- not something Daniel "
                             "stated; the quote is the basis]")
                lines.append(line)
            if not lines:
                return {"ok": True,
                        "result": f"Nothing in memory about {query}.",
                        "state": {}}
            return {"ok": True,
                    "result": "From memory:\n" + "\n".join(lines),
                    "state": {}}
        if name == "memory_note":
            if self._notebook is None:
                return {"ok": False,
                        "result": "Notebook is not wired in this core.",
                        "state": {}}
            kind = str(args.get("kind", ""))
            text = str(args.get("text", "")).strip()
            if kind not in ("correction", "proposal") or not text:
                return {"ok": False,
                        "result": "A note needs a kind (correction or "
                                  "proposal) and the note text.",
                        "state": {}}
            entry: dict = {"kind": kind, "text": text}
            if args.get("corrects"):
                entry["corrects"] = str(args["corrects"])
            if args.get("subject"):
                entry["subject"] = str(args["subject"])
            self.log_note(entry)
            if kind == "correction":
                return {"ok": True,
                        "result": "Correction filed in your notebook. "
                                  "It outranks the stored memory now; "
                                  "the curator formalizes it tonight.",
                        "state": {}}
            return {"ok": True,
                    "result": "Proposal filed in your notebook; the "
                              "curator will weigh it tonight.",
                    "state": {}}
        return {"ok": False, "result": f"Unknown tool: {name}", "state": {}}
