"""Client for the Nemotron streaming-ASR sidecar (RTC mode).

The sidecar (modules/streamstt, lyra-nemotron-stream.service) wraps
parakeet.cpp's cache-aware streaming session for
nvidia/nemotron-3.5-asr-streaming-0.6b: 16 kHz PCM in over a
localhost socket, finalized text out as it forms. It exists
because parakeet.cpp's streaming surface is its C API, not the
HTTP server the batch TDT path uses (:8096).

Protocol (see nemotron_stream_server.c): framed input messages
(1-byte type + 4-byte big-endian length) -- 'A' audio, 'F' flush,
'R' reset, 'P' ping -- and newline-JSON replies: ready / text /
final / reset / pong / error. Control tokens (the model's
trailing "<en-US>" locale tag) are stripped server-side, so text
arriving here is router-safe.

This client tracks the state the RTC turn-end rule reads:

- text: finalized text accumulated for the current utterance.
- last_word_end: stream-clock seconds of the newest finalized
  word's end (the model has seen all audio up to audio_fed, so a
  word is final only once later audio has been fed past it).
- audio_fed: seconds of audio sent since the last flush/reset
  (counted locally; same clock as last_word_end).
- last_text_at: monotonic time of the last text update.

Failure model: any io error or EOF marks the client failed and
fires on_failure once; feed() never raises. The RTC session arms
its legacy Endpointer+TDT path when that happens, so a dead
streamer can never deafen the session.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Callable

_LOGGER = logging.getLogger("lyra_core.stream_stt")

STREAM_STT_HOST = "127.0.0.1"
STREAM_STT_PORT = 8097  # lyra-nemotron-stream.service

_FRAME_HDR = struct.Struct(">cI")
_SAMPLES_PER_SEC = 16000


class StreamSttClient:
    """One sidecar connection = one stream. Not thread-safe; use
    from the event loop that created it."""

    def __init__(self, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._text = ""
        self._last_word_end: float | None = None
        self._audio_fed = 0.0
        self._last_text_at = 0.0
        self._failed = False
        self._ready = asyncio.Event()
        self._final_waiter: asyncio.Future | None = None
        self.on_failure: Callable[[], None] | None = None
        self._reader_task = asyncio.ensure_future(self._read_loop())

    @classmethod
    async def connect(cls, timeout: float = 3.0) -> "StreamSttClient | None":
        """Connect and wait for the sidecar's ready line; None if
        the sidecar is down or wedged (the caller falls back)."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(STREAM_STT_HOST, STREAM_STT_PORT),
                timeout)
        except Exception:
            return None
        client = cls(reader, writer)
        try:
            await asyncio.wait_for(client._ready.wait(), timeout)
        except Exception:
            client._fail()
            await client.close()
            return None
        if client._failed:
            await client.close()
            return None
        return client

    # -- state the turn-end rule reads ------------------------------

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def text(self) -> str:
        return self._text

    @property
    def last_word_end(self) -> float | None:
        return self._last_word_end

    @property
    def audio_fed(self) -> float:
        return self._audio_fed

    @property
    def last_text_at(self) -> float:
        return self._last_text_at

    # -- commands -----------------------------------------------------

    async def feed(self, pcm: bytes) -> None:
        """Send one chunk of s16le 16 kHz mono PCM. Never raises:
        an io failure marks the client failed instead."""
        if self._failed or not pcm:
            return
        self._audio_fed += (len(pcm) // 2) / _SAMPLES_PER_SEC
        try:
            self._writer.write(_FRAME_HDR.pack(b"A", len(pcm)) + pcm)
            await self._writer.drain()
        except Exception:
            self._fail()

    async def flush(self, timeout: float = 5.0) -> str | None:
        """Finalize the current utterance; returns its full text
        (possibly ''), or None if the sidecar failed/timed out --
        in which case the client is marked failed. On success the
        per-utterance state resets for the next utterance."""
        if self._failed:
            return None
        loop = asyncio.get_event_loop()
        self._final_waiter = loop.create_future()
        try:
            self._writer.write(_FRAME_HDR.pack(b"F", 0))
            await self._writer.drain()
            text = await asyncio.wait_for(self._final_waiter, timeout)
        except Exception:
            self._fail()
            return None
        finally:
            self._final_waiter = None
        self._clear_utterance()
        return text

    async def reset(self) -> None:
        """Discard the current utterance without finalizing it."""
        if self._failed:
            return
        try:
            self._writer.write(_FRAME_HDR.pack(b"R", 0))
            await self._writer.drain()
        except Exception:
            self._fail()
            return
        self._clear_utterance()

    async def close(self) -> None:
        self._reader_task.cancel()
        try:
            self._writer.close()
            await self._writer.wait_closed()
        except Exception:
            pass

    # -- internals ----------------------------------------------------

    def _clear_utterance(self) -> None:
        self._text = ""
        self._last_word_end = None
        self._audio_fed = 0.0
        self._last_text_at = 0.0

    def _fail(self) -> None:
        if self._failed:
            return
        self._failed = True
        waiter, self._final_waiter = self._final_waiter, None
        if waiter is not None and not waiter.done():
            waiter.set_exception(ConnectionError("stream sidecar lost"))
        callback, self.on_failure = self.on_failure, None
        if callback is not None:
            try:
                callback()
            except Exception:
                _LOGGER.exception("stream on_failure callback failed")

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except Exception:
                    _LOGGER.warning("stream sidecar: unparseable line %r", line[:120])
                    continue
                kind = msg.get("type")
                if kind == "ready":
                    self._ready.set()
                elif kind == "text":
                    delta = msg.get("text") or ""
                    if delta:
                        self._text += delta
                        self._last_text_at = time.monotonic()
                    end = msg.get("last_word_end")
                    if isinstance(end, (int, float)):
                        self._last_word_end = float(end)
                elif kind == "final":
                    waiter, self._final_waiter = self._final_waiter, None
                    if waiter is not None and not waiter.done():
                        waiter.set_result(msg.get("text") or "")
                elif kind == "error":
                    _LOGGER.warning("stream sidecar error: %s", msg.get("message"))
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("stream sidecar read loop failed")
        finally:
            self._ready.set()  # never leave connect() hanging
            self._fail()
