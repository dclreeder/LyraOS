"""TTS event dialect bench (2026-11-11 fix): the ESPHome firmware's
voice_assistant.cpp reads data arg "text" on VOICE_ASSISTANT_TTS_START
and "url" on VOICE_ASSISTANT_TTS_END; the core used to send them the
other way round, so every reply bailed silently on the box (firmware
log: "No text in TTS_START event" / "No url in TTS_END event").

Part 1 -- BoxClient unit check against a fake _send: exact event types
and payload keys/values for evt_tts_start / evt_tts_end.

Part 2 -- session level, fake box + fake driver + fake verify gate:
a matched command must produce evt_tts_start(reply text) followed by
evt_tts_end(reply URL); the D5 garbled path speaks the D5 line the
same way; the no-match line rides the same TTS pair; the
verify-fail (silent drop) path must emit NO TTS events at all.

The real box is never contacted. Run:
  python -m lyra_core.tts_dialect_bench
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from .audio_http import AudioHttpServer
from .box_client import BoxClient, VoiceAssistantEventType
from .config import CoreConfig
from .router import DIDNT_CATCH, NOT_UNDERSTOOD
from .session import VoiceSession


# ---------------------------------------------------------------------------
# Part 1 -- BoxClient payload unit check
# ---------------------------------------------------------------------------

def part_1() -> None:
    print("== Part 1: BoxClient TTS payloads ==")
    cfg = CoreConfig.load()

    async def _noop_audio(data: bytes) -> None:
        pass

    box = BoxClient(cfg, on_audio=_noop_audio)
    sent: list = []
    box._send = lambda event_type, data=None: sent.append((event_type, data))  # type: ignore[method-assign]
    box.evt_tts_start("It's 17:05.")
    box.evt_tts_end("http://192.168.12.144:8099/audio/abc123.wav")
    assert sent == [
        (VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START, {"text": "It's 17:05."}),
        (
            VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END,
            {"url": "http://192.168.12.144:8099/audio/abc123.wav"},
        ),
    ], sent
    print(f"  sent: {sent}")
    print("Part 1: PASS")


# ---------------------------------------------------------------------------
# Part 2 -- session level with fakes
# ---------------------------------------------------------------------------

class FakeBox:
    def __init__(self) -> None:
        self.calls: list = []
        self.is_connected = True

    def _rec(self, name, detail=None):
        self.calls.append((name, detail or {}))

    def evt_run_start(self): self._rec("run_start")
    def evt_run_end(self): self._rec("run_end")
    def evt_stt_start(self): self._rec("stt_start")
    def evt_stt_vad_start(self): self._rec("stt_vad_start")
    def evt_stt_vad_end(self): self._rec("stt_vad_end")
    def evt_stt_end(self, text): self._rec("stt_end", {"text": text})
    def evt_intent_start(self): self._rec("intent_start")
    def evt_intent_end(self): self._rec("intent_end")
    def evt_tts_start(self, text): self._rec("tts_start", {"text": text})
    def evt_tts_end(self, url): self._rec("tts_end", {"url": url})
    def evt_error(self, code, message=""): self._rec("error", {"code": code})


class FakeDriver:
    def __init__(self) -> None:
        self.synthesized: list = []

    async def synthesize_speech(self, text, timeout=120.0):
        self.synthesized.append(text)
        return b"RIFF" + bytes(1000), 24000, 2, 1

    async def transcribe_audio(self, *a, **k):
        raise AssertionError("transcribe_audio must not run when a gate is attached")


class FakeGate:
    def __init__(self, **kw) -> None:
        self._result = SimpleNamespace(
            verdict=kw.get("verdict", "pass"),
            transcript=kw.get("transcript", ""),
            command=kw.get("command"),
            gate_score=0.99,
            dry_run=False,
            filed_path=None,
            audio_seconds=1.0,
        )

    async def verify(self, audio, spool_ts=None):
        return self._result


async def _run_turn(cfg, gate, hook):
    server = AudioHttpServer("127.0.0.1", 0)
    server.start()
    driver = FakeDriver()
    session = VoiceSession(cfg, driver, server, verify_gate=gate, on_command=hook)
    box = FakeBox()
    session.attach_box(box)  # type: ignore[arg-type]
    try:
        await session._process_utterance(b"\x00\x00" * 1600, no_speech=False)
    finally:
        server.stop()
    return session, driver, box


def _names(box) -> list:
    return [n for n, _d in box.calls]


async def part_2() -> None:
    print("== Part 2: session TTS dialect by outcome ==")
    cfg = CoreConfig.load()

    # A -- matched command: hook (router) returns the spoken confirmation.
    def hook_match(command, result):
        assert command == "what time is it?", command
        return "It's 17:05."

    session, driver, box = await _run_turn(
        cfg,
        FakeGate(transcript="Hey Lyra, what time is it?", command="what time is it?"),
        hook_match,
    )
    names = _names(box)
    assert driver.synthesized == ["It's 17:05."], driver.synthesized
    i_start = names.index("tts_start")
    i_end = names.index("tts_end")
    assert i_start < i_end < names.index("run_end"), names
    assert box.calls[i_start][1] == {"text": "It's 17:05."}, box.calls[i_start]
    assert box.calls[i_end][1] == {"url": session.last_tts_url}, box.calls[i_end]
    assert session.last_tts_url and session.last_tts_url.endswith(".wav")
    print(f"  A matched:  tts_start text -> tts_end url ({session.last_tts_url})")

    # B -- D5 garbled: PASS with an empty command; the router's D5 line
    # is the reply and must ride TTS_START as text like any other reply.
    def hook_d5(command, result):
        assert command == "", command
        return DIDNT_CATCH

    session, driver, box = await _run_turn(
        cfg,
        FakeGate(transcript="Hey Lyra, uh...", command=""),
        hook_d5,
    )
    names = _names(box)
    assert driver.synthesized == [DIDNT_CATCH], driver.synthesized
    i_start = names.index("tts_start")
    assert box.calls[i_start][1] == {"text": DIDNT_CATCH}, box.calls[i_start]
    assert box.calls[names.index("tts_end")][1] == {"url": session.last_tts_url}
    print("  B garbled:  D5 line spoken via tts_start(text) -> tts_end(url)")

    # C -- no-match: the router's no-match line is spoken exactly
    # like a confirmation (TTS_START text -> TTS_END url).
    def hook_no_match(command, result):
        assert command == "blorp the fribbit", command
        return NOT_UNDERSTOOD

    session, driver, box = await _run_turn(
        cfg,
        FakeGate(transcript="Hey Lyra, blorp the fribbit", command="blorp the fribbit"),
        hook_no_match,
    )
    names = _names(box)
    assert driver.synthesized == [NOT_UNDERSTOOD], driver.synthesized
    i_start = names.index("tts_start")
    assert box.calls[i_start][1] == {"text": NOT_UNDERSTOOD}, box.calls[i_start]
    assert box.calls[names.index("tts_end")][1] == {"url": session.last_tts_url}
    assert names[-1] == "run_end", names
    print("  C no-match: 'I don't understand.' spoken via tts_start(text) -> tts_end(url)")

    # D -- verify fail: silent drop; the hook must not even be called.
    called = []

    def hook_spy(command, result):
        called.append(command)
        return "must never be spoken"

    session, driver, box = await _run_turn(
        cfg,
        FakeGate(verdict="fail", transcript=".", command=None),
        hook_spy,
    )
    names = _names(box)
    assert called == [], called
    assert "tts_start" not in names and "tts_end" not in names, names
    assert driver.synthesized == [] and session.last_tts_url is None
    assert "error" in names and names[-1] == "run_end", names
    print("  D verify-fail: silent drop, zero TTS events")
    print("Part 2: PASS")


async def amain() -> None:
    part_1()
    await part_2()
    print("tts_dialect_bench OK: all assertions pass")


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
