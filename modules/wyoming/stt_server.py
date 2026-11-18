#!/usr/bin/env python3
"""Wyoming ASR server: thin bridge to the Parakeet transcription service.

Audio streams in over Wyoming (16 kHz / 16-bit / mono), is assembled
into a WAV in memory, and is POSTed to parakeet-server at
127.0.0.1:8096 (/v1/audio/transcriptions, multipart field 'file').
This process loads no model of its own; the single Parakeet instance
stays in the parakeet-server service (~/Documents/parakeet.cpp).
Backend history: whisper.cpp (:8093) until 2026-11-17, when Daniel
ordered Lyra swapped to Parakeet and Whisper pulled from the project.
"""
import asyncio
import io
import logging
import os
import sys
import wave

import httpx
import numpy as np

sys.path.insert(0, os.path.expanduser("~/Documents/LyraOS/modules/wyoming"))
import clip_collector as collector  # noqa: E402

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import AsrModel, AsrProgram, Attribution, Describe, Info
from wyoming.server import AsyncEventHandler, AsyncServer

_LOGGER = logging.getLogger("lyra-stt")
PARAKEET_URL = "http://127.0.0.1:8096/v1/audio/transcriptions"

INFO = Info(
    asr=[
        AsrProgram(
            name="lyra-parakeet",
            attribution=Attribution(name="Lyra (local)", url=""),
            installed=True,
            description="Parakeet TDT 0.6B v2 via the parakeet-server service",
            version="1.0",
            models=[
                AsrModel(
                    name="parakeet-tdt-0.6b-v2",
                    attribution=Attribution(name="NVIDIA Parakeet", url=""),
                    installed=True,
                    description="tdt-0.6b-v2 q8_0 (parakeet.cpp on the Fedora GPU)",
                    version=None,
                    languages=["en"],
                )
            ],
            supports_transcript_streaming=False,
        )
    ]
)


def to_wav(audio: bytes, rate: int, width: int, channels: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(audio)
    return buf.getvalue()


class SttHandler(AsyncEventHandler):
    def __init__(self, reader, writer):
        super().__init__(reader, writer)
        self.language = "en"
        self.audio = bytearray()
        self.rate, self.width, self.channels = 16000, 2, 1

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(INFO.event())
            return True
        if Transcribe.is_type(event.type):
            transcribe = Transcribe.from_event(event)
            if transcribe.language:
                self.language = transcribe.language
            self.audio.clear()
            return True
        if AudioStart.is_type(event.type):
            start = AudioStart.from_event(event)
            self.rate = start.rate
            self.width = start.width
            self.channels = start.channels
            return True
        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)
            self.audio.extend(chunk.audio)
            return True
        if AudioStop.is_type(event.type):
            await self._transcribe()
            return True
        return True

    async def _transcribe(self) -> None:
        if not self.audio:
            await self.write_event(
                Transcript(text="", language=self.language).event()
            )
            return
        wav_bytes = to_wav(bytes(self.audio), self.rate, self.width, self.channels)
        command_audio = np.frombuffer(bytes(self.audio), dtype=np.int16).copy()
        self.audio.clear()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(180.0)) as client:
                resp = await client.post(
                    PARAKEET_URL,
                    files={"file": ("audio.wav", wav_bytes, "audio/wav")},
                )
                resp.raise_for_status()
                text = (resp.json().get("text") or "").strip()
        except Exception:
            _LOGGER.exception("parakeet request failed")
            text = ""
        _LOGGER.info("Transcript (%s): %r", self.language, text)
        await self.write_event(Transcript(text=text, language=self.language).event())
        # Collector (2026-11-10): finalize the spooled wake detection
        # with this command audio attached. Fire-and-forget so the
        # voice path never waits on verification/filing.
        asyncio.create_task(self._collect(command_audio))

    async def _collect(self, command_audio: np.ndarray) -> None:
        try:
            got = collector.claim_latest_detection(max_age_s=120)
            if got is None:
                return
            meta, wake_wav = got
            try:
                wake_audio = collector.read_wav(wake_wav)
                combined = np.concatenate([wake_audio, command_audio])
                text = await collector.parakeet_transcribe(combined)
                verdict = collector.classify_wake_transcription(text)
                collector.file_clip(verdict, meta["gate_score"], text,
                                    meta["ts"], combined)
            finally:
                collector.discard_wake_wav(wake_wav)
        except Exception:
            _LOGGER.exception("collector finalize failed")


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = AsyncServer.from_uri("tcp://0.0.0.0:10300")
    _LOGGER.info("Lyra STT bridge listening on tcp://0.0.0.0:10300")
    await server.run(lambda r, w: SttHandler(r, w))


if __name__ == "__main__":
    asyncio.run(main())
