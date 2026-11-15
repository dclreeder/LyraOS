#!/usr/bin/env python3
"""Wyoming TTS server: thin bridge to the Fedora's Kokoro service.

Synthesis requests arrive over Wyoming; the text is POSTed to the shared
Kokoro server at 127.0.0.1:8091 (/tts, JSON body {"text", "voice",
"speed"}, auth via the X-Lyra-Token header read at startup from the
Kokoro server's token.txt). The response's base64 WAV is decoded and its
PCM payload is streamed back as Wyoming audio events. This process loads
no model of its own; the single Kokoro instance stays in the
lyra-kokoro-tts service.
"""
import asyncio
import base64
import io
import logging
import os
import wave

import httpx

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, TtsProgram, TtsVoice
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.pipeline import RunPipeline
from wyoming.tts import Synthesize, SynthesizeChunk

_LOGGER = logging.getLogger("lyra-tts")
KOKORO_URL = "http://127.0.0.1:8091/tts"
TOKEN_FILE = os.path.expanduser("~/Documents/LyraOS/modules/tts/kokoro/server/token.txt")
DEFAULT_VOICE = "af_heart"
CHUNK_BYTES = 8192

with open(TOKEN_FILE) as _fh:
    _TOKEN = _fh.read().strip()

INFO = Info(
    tts=[
        TtsProgram(
            name="lyra-kokoro",
            attribution=Attribution(name="Lyra (local)", url=""),
            installed=True,
            description="Kokoro TTS via the Fedora kokoro_server (:8091)",
            version="1.0",
            voices=[
                TtsVoice(
                    name="af_heart",
                    attribution=Attribution(name="Kokoro", url=""),
                    installed=True,
                    description="af_heart (Kokoro default voice)",
                    version=None,
                    languages=["en"],
                )
            ],
            supports_synthesize_streaming=False,
        )
    ]
)


class TtsHandler(AsyncEventHandler):
    def __init__(self, reader, writer):
        super().__init__(reader, writer)
        self.text_parts = []

    async def handle_event(self, event: Event) -> bool:
        if Describe.is_type(event.type):
            await self.write_event(INFO.event())
            return True
        if Synthesize.is_type(event.type):
            synth = Synthesize.from_event(event)
            voice = DEFAULT_VOICE
            if synth.voice is not None and synth.voice.name:
                voice = synth.voice.name
            await self._synthesize(synth.text or "", voice)
            return True
        if RunPipeline.is_type(event.type):
            # A pipeline is starting; TTS is its final stage. Nothing to do.
            return True
        if SynthesizeChunk.is_type(event.type):
            chunk = SynthesizeChunk.from_event(event)
            if chunk.text:
                self.text_parts.append(chunk.text)
            return True
        return True

    async def _synthesize(self, text: str, voice: str) -> None:
        text = text.strip()
        if not text:
            return
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
                resp = await client.post(
                    KOKORO_URL,
                    json={"text": text, "voice": voice, "speed": 1.0},
                    headers={"X-Lyra-Token": _TOKEN},
                )
                resp.raise_for_status()
                wav_bytes = base64.b64decode(resp.json()["data"])
        except Exception:
            _LOGGER.exception("Kokoro request failed")
            return
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            rate = wf.getframerate()
            width = wf.getsampwidth()
            channels = wf.getnchannels()
            pcm = wf.readframes(wf.getnframes())
        _LOGGER.info(
            "Synthesized %d chars (voice=%s) -> %d B PCM @ %d Hz",
            len(text), voice, len(pcm), rate,
        )
        await self.write_event(
            AudioStart(rate=rate, width=width, channels=channels).event()
        )
        for i in range(0, len(pcm), CHUNK_BYTES):
            await self.write_event(
                AudioChunk(
                    rate=rate, width=width, channels=channels,
                    audio=pcm[i:i + CHUNK_BYTES],
                ).event()
            )
        await self.write_event(AudioStop().event())


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    server = AsyncServer.from_uri("tcp://0.0.0.0:10200")
    _LOGGER.info("Lyra TTS bridge listening on tcp://0.0.0.0:10200")
    await server.run(lambda r, w: TtsHandler(r, w))


if __name__ == "__main__":
    asyncio.run(main())
