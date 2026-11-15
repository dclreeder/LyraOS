"""Pipeline driver: the core playing HA's client role against the three
production Wyoming services (wake :10400, STT :10300, TTS :10200).

Phase 1b scope: recorded-clip round trips only — describe, wake detection,
transcription, synthesis. No box involvement; the box-facing choreography
(pipeline events back to the device) lives in box_client.py and is wired
to this driver in the seat-swap phase.
"""
from __future__ import annotations

import asyncio
import io
import json
import re
import unicodedata
import wave

from .config import CoreConfig
from .wyoming_client import (
    WyomingConnection,
    WyomingEvent,
    audio_chunk,
    audio_start,
    audio_stop,
    describe,
    detect,
    synthesize,
    transcribe,
)


_DISPLAY_STATE_PATH = "/home/dclreeder/Documents/LyraOS/web/display_state.json"


def speech_route() -> str:
    """Where spoken lines go. Since 2026-11-15 (Daniel's call) the TV
    is the ONLY audio out: the speaker box is out of the output
    equation entirely and serves as ears only, so this is "tv"
    unconditionally -- the display state no longer routes speech.
    (Before that, this read display_state.json fresh on every line and
    routed between the TV and the box; restore that body to bring the
    box back as an output.)"""
    return "tv"


def silent_wav_like(wav_bytes: bytes) -> bytes:
    """A silent WAV with the same format and duration as the given one.
    On the TV route the box is handed this as its TTS audio: its run
    completes a normal play phase -- no empty-run beeps -- and the
    core's playback echo-guard covers the window while the TV speaks
    the real line, so her own voice is not heard as a new command."""
    src = wave.open(io.BytesIO(wav_bytes), "rb")
    params = src.getparams()
    frames = src.getnframes()
    src.close()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setparams(params)
        wf.writeframes(b"\x00" * frames * params.sampwidth * params.nchannels)
    return buf.getvalue()


async def _mirror_wav_to_tv(wav_bytes: bytes) -> None:
    """Fire-and-forget: hand each spoken line's WAV to the TV web viewer
    (lyra-web :8095/mirror) so the on-screen model mouths along, muted.
    Any failure is swallowed: the TV mirror must never affect speech."""
    try:
        import urllib.request
        def _post() -> None:
            req = urllib.request.Request(
                "http://127.0.0.1:8095/mirror", data=wav_bytes,
                headers={"Content-Type": "audio/wav"})
            urllib.request.urlopen(req, timeout=2).read()
        await asyncio.to_thread(_post)
    except Exception:
        pass


class PipelineError(RuntimeError):
    """A Wyoming service answered with an error event or closed early."""


# ---------------------------------------------------------------------------
# Speech-text hygiene: a speaker cannot say a pictograph (2026-11-11).
#
# synthesize_speech below is the ONE funnel every spoken line passes
# through -- session replies (free-router text and tool confirmations
# alike), the /announce composition, the cutover line -- so glyph
# stripping lives here and nowhere else. It is a pipeline necessity,
# not model steering: the free router's words reach this point
# untouched, but Kokoro pronounces an emoji's NAME aloud (Daniel heard
# "smiling face with smiling eyes" for U+1F60A), and no wording change
# upstream can make a pictograph speakable. Lines without glyphs are
# returned byte-identical (fast path), so ordinary text -- punctuation,
# math symbols, any language's letters -- is never rewritten.
# ---------------------------------------------------------------------------

# Emoji sequence machinery: zero-width joiner, variation selector-16,
# combining enclosing keycap. Meaningless (and unspeakable) on their
# own once the pictograph they decorate is gone.
_GLYPH_JOINERS = frozenset({"\u200d", "\ufe0f", "\u20e3"})
_WHITESPACE_RUN = re.compile(r"\s+")
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([.,!?;:%])")


def _is_unspeakable_glyph(ch: str) -> bool:
    if ch in _GLYPH_JOINERS:
        return True
    if "\U0001f3fb" <= ch <= "\U0001f3ff":  # emoji skin-tone modifiers
        return True
    # Category So (Symbol, other): the pictographs, emoticons, dingbats,
    # transport/map symbols, and regional-indicator flag letters. TTS
    # reads every one of them as its Unicode name.
    return unicodedata.category(ch) == "So"


_MD_HEADING = re.compile(r"^#{1,6}\s+", re.M)
_MD_BULLET = re.compile(r"^\s*(?:[-*\u2022]|\d+[.)])\s+", re.M)


def _strip_markdown(text: str) -> str:
    """Remove markdown the model sometimes emits (2026-11-12: a
    weather answer arrived as a bolded bullet list and was recited
    verbatim). Bold/code markers drop out, heading and bullet/number
    prefixes come off line starts; the words stay."""
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    return text.replace("**", "").replace("__", "").replace("`", "")


# Commonly spoken units/abbreviations, expanded to how people say them.
# Applied to EVERY spoken line in the funnel (global, not per-tool).
_SPEAK_AS_SPOKEN = [
    (re.compile(r"\bmph\b", re.IGNORECASE), "miles per hour"),
    (re.compile(r"\b(?:kph|km/h)\b", re.IGNORECASE), "kilometers per hour"),
    (re.compile(r"(\d)\s*sq\.?\s*ft\b"), r"\1 square feet"),
    (re.compile(r"(\d)\s*\u00b0F\b"), r"\1 degrees Fahrenheit"),
    (re.compile(r"(\d)\s*\u00b0C\b"), r"\1 degrees Celsius"),
    (re.compile(r"\bdegrees\s+F\b"), "degrees Fahrenheit"),
    (re.compile(r"(\d)\s*F\b"), r"\1 degrees Fahrenheit"),
    (re.compile(r"\blbs?\b", re.IGNORECASE), "pounds"),
    (re.compile(r"\boz\b", re.IGNORECASE), "ounces"),
    (re.compile(r"(\d)\s*ft\b"), r"\1 feet"),
    (re.compile(r"(\d)\s*km\b"), r"\1 kilometers"),
    (re.compile(r"(\d)\s*mi\b"), r"\1 miles"),
]


def speakable_text(text: str) -> str:
    """Return `text` made speakable: markdown stripped, then emoji/pictographs stripped for synthesis.

    Only glyph-bearing lines are rewritten: the glyphs are dropped,
    the spacing their removal leaves behind is tidied (space runs
    collapsed, no space before punctuation), and the result is
    stripped. A line that was ONLY glyphs comes back empty; the
    caller decides what an empty line means.
    """
    text = _strip_markdown(text)
    for _pat, _repl in _SPEAK_AS_SPOKEN:
        text = _pat.sub(_repl, text)
    if not any(_is_unspeakable_glyph(ch) for ch in text):
        return text
    cleaned = "".join(ch for ch in text if not _is_unspeakable_glyph(ch))
    cleaned = _SPACE_BEFORE_PUNCT.sub(r"\1", cleaned)
    cleaned = _WHITESPACE_RUN.sub(" ", cleaned)
    return cleaned.strip()


async def _next_of_type(
    conn: WyomingConnection, want: str, timeout: float
) -> WyomingEvent:
    """Read events until one of the wanted type arrives."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise PipelineError(f"timed out waiting for {want!r} event")
        event = await asyncio.wait_for(conn.receive(), timeout=remaining)
        if event is None:
            raise PipelineError(f"connection closed waiting for {want!r}")
        if event.type == "error":
            raise PipelineError(f"service error event: {event.data}")
        if event.type == want:
            return event


class PipelineDriver:
    def __init__(self, config: CoreConfig):
        self._cfg = config.wyoming

    async def describe(self, uri: str) -> dict:
        async with await WyomingConnection.connect(uri) as conn:
            await conn.send(describe())
            info = await _next_of_type(conn, "info", timeout=15)
            return info.data

    async def describe_all(self) -> dict[str, dict]:
        return {
            "wake": await self.describe(self._cfg.wake_uri),
            "stt": await self.describe(self._cfg.stt_uri),
            "tts": await self.describe(self._cfg.tts_uri),
        }

    async def detect_wake(
        self,
        pcm: bytes,
        rate: int = 16000,
        width: int = 2,
        channels: int = 1,
        grace_s: float = 15.0,
    ) -> dict | None:
        """Stream a full clip to the wake server at ~real-time pace.

        Returns the Detection event's data dict, or None if the gate never
        fired. The whole clip is always streamed (a real satellite's mic
        stream doesn't stop at the detection), then we wait out the grace
        window for the Detection the gate emits mid-stream.
        """
        cfg = self._cfg
        chunk_samples = 1600  # 100 ms at 16 kHz
        chunk_bytes = chunk_samples * width * channels
        async with await WyomingConnection.connect(cfg.wake_uri) as conn:
            await conn.send(detect([cfg.wake_name]))
            await conn.send(audio_start(rate, width, channels))
            for offset in range(0, len(pcm), chunk_bytes):
                await conn.send(
                    audio_chunk(rate, width, channels, pcm[offset:offset + chunk_bytes])
                )
                await asyncio.sleep(0.09)
            await conn.send(audio_stop())
            try:
                event = await _next_of_type(conn, "detection", timeout=grace_s)
                return event.data
            except PipelineError:
                return None

    async def transcribe_audio(
        self,
        pcm: bytes,
        rate: int = 16000,
        width: int = 2,
        channels: int = 1,
        timeout: float = 180.0,
    ) -> str:
        cfg = self._cfg
        chunk_bytes = 8192 * width * channels
        async with await WyomingConnection.connect(cfg.stt_uri) as conn:
            await conn.send(transcribe(cfg.language))
            await conn.send(audio_start(rate, width, channels))
            for offset in range(0, len(pcm), chunk_bytes):
                await conn.send(
                    audio_chunk(rate, width, channels, pcm[offset:offset + chunk_bytes])
                )
            await conn.send(audio_stop())
            event = await _next_of_type(conn, "transcript", timeout=timeout)
            return (event.data.get("text") or "").strip()

    async def synthesize_speech(
        self, text: str, timeout: float = 120.0
    ) -> tuple[bytes, int, int, int]:
        """Synthesize text via the TTS service; returns (wav_bytes, rate,
        width, channels) with the PCM reassembled into a WAV container."""
        cfg = self._cfg
        text = speakable_text(text)
        if not text:
            raise PipelineError("speech text empty after glyph stripping")
        async with await WyomingConnection.connect(cfg.tts_uri) as conn:
            await conn.send(synthesize(text, voice=cfg.tts_voice))
            start = await _next_of_type(conn, "audio-start", timeout=timeout)
            rate = int(start.data.get("rate") or 22050)
            width = int(start.data.get("width") or 2)
            channels = int(start.data.get("channels") or 1)
            pcm = bytearray()
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise PipelineError("timed out collecting TTS audio")
                event = await asyncio.wait_for(conn.receive(), timeout=remaining)
                if event is None:
                    raise PipelineError("TTS connection closed mid-audio")
                if event.type == "audio-chunk" and event.payload:
                    pcm.extend(event.payload)
                elif event.type == "audio-stop":
                    break
                elif event.type == "error":
                    raise PipelineError(f"TTS error event: {event.data}")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(width)
            wf.setframerate(rate)
            wf.writeframes(bytes(pcm))
        if speech_route() == "tv":
            try:
                asyncio.get_running_loop().create_task(
                    _mirror_wav_to_tv(buf.getvalue()))
            except Exception:
                pass
        return buf.getvalue(), rate, width, channels
