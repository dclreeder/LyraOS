"""Minimal Wyoming protocol client (asyncio, stdlib only).

The core deliberately does not depend on the `wyoming` pip package: the
wire format is small and stable, and keeping it stdlib-only lets the whole
core run under the esphome venv (which carries aioesphomeapi) without a
second environment. Framing mirrors wyoming 1.11.0's event.py exactly:

  header line (JSON, \\n-terminated): {"type", "version",
      optional "data_length", optional "payload_length"}
  then data_length raw bytes: the event's data dict as JSON (if any)
  then payload_length raw bytes: binary payload (audio) (if any)

Event type strings and data shapes were verified against the production
servers and the wyoming 1.11.0 library on 2026-11-11 (Phase 1b).
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from urllib.parse import urlparse

PROTOCOL_VERSION = "1.11.0"


@dataclass
class WyomingEvent:
    type: str
    data: dict = field(default_factory=dict)
    payload: bytes | None = None


async def write_event(writer: asyncio.StreamWriter, event: WyomingEvent) -> None:
    header: dict = {"type": event.type, "version": PROTOCOL_VERSION}
    data_bytes: bytes | None = None
    if event.data:
        data_bytes = json.dumps(event.data, ensure_ascii=False).encode("utf-8")
        header["data_length"] = len(data_bytes)
    payload = event.payload or b""
    if payload:
        header["payload_length"] = len(payload)
    writer.write(json.dumps(header, ensure_ascii=False).encode("utf-8") + b"\n")
    if data_bytes:
        writer.write(data_bytes)
    if payload:
        writer.write(payload)
    await writer.drain()


async def read_event(reader: asyncio.StreamReader) -> WyomingEvent | None:
    line = await reader.readline()
    if not line:
        return None
    header = json.loads(line)
    data: dict = dict(header.get("data") or {})
    data_length = header.get("data_length")
    if data_length:
        data.update(json.loads(await reader.readexactly(data_length)))
    payload: bytes | None = None
    payload_length = header.get("payload_length")
    if payload_length:
        payload = await reader.readexactly(payload_length)
    return WyomingEvent(type=header["type"], data=data, payload=payload)


class WyomingConnection:
    """One TCP connection to a Wyoming service (one stage = one connection,
    the same shape HA's Wyoming client uses)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self._reader = reader
        self._writer = writer

    @classmethod
    async def connect(cls, uri: str) -> "WyomingConnection":
        parsed = urlparse(uri)
        if parsed.scheme != "tcp":
            raise ValueError(f"unsupported Wyoming URI scheme: {uri!r}")
        reader, writer = await asyncio.open_connection(parsed.hostname, parsed.port)
        return cls(reader, writer)

    async def send(self, event: WyomingEvent) -> None:
        await write_event(self._writer, event)

    async def receive(self) -> WyomingEvent | None:
        return await read_event(self._reader)

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass

    async def __aenter__(self) -> "WyomingConnection":
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()


# -- Event constructors (data shapes verified 2026-11-11) -------------------

def describe() -> WyomingEvent:
    return WyomingEvent("describe")


def detect(names: list[str]) -> WyomingEvent:
    return WyomingEvent("detect", {"names": list(names)})


def audio_start(rate: int, width: int, channels: int) -> WyomingEvent:
    return WyomingEvent(
        "audio-start",
        {"rate": rate, "width": width, "channels": channels, "timestamp": None},
    )


def audio_chunk(rate: int, width: int, channels: int, pcm: bytes) -> WyomingEvent:
    return WyomingEvent(
        "audio-chunk",
        {"rate": rate, "width": width, "channels": channels, "timestamp": None},
        payload=pcm,
    )


def audio_stop() -> WyomingEvent:
    return WyomingEvent("audio-stop", {"timestamp": None})


def synthesize(text: str, voice: str | None = None) -> WyomingEvent:
    data: dict = {"text": text}
    if voice:
        data["voice"] = {"name": voice}
    return WyomingEvent("synthesize", data)
