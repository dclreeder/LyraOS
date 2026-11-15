"""Stage 1 bench: the sat-link transport, proven host-side.

A fake box -- a TCP client speaking the sat7 wire protocol exactly
as sat_local.c does (hello on connect, 640 B AUDIO_UP frames, pings,
status replies, set_volume applied silently, barge_in recorded) --
connects to a real SatBoxClient bound to a BENCH port (127.0.0.1:
19876, never the production 9876) wired into a real VoiceSession.

Part A -- end-to-end turn: the recorded positive clip streams up as
AUDIO_UP; the production Wyoming wake server detects it; the session
captures, transcribes (production STT) and synthesizes a reply
(production Kokoro); the reply must come back DOWN as AUDIO_DOWN
PCM frames whose payload equals the reply WAV decoded to 24 kHz
mono (the thing ESPHome never managed). Also proves the core's
pongs answer the box's pings and the core's keepalive pings flow.

Part B -- media surface: volume round-trips (hello volume ->
set_volume -> get_status), the alarm tone streams from the local
opus via ffmpeg, media_stop lands a barge_in and frames cease
within the documented drain bound, and announce() completes with
success once its stream drains.

Part C -- supervisor integration: CoreSupervisor drives
SatBoxClient through connect -> drop -> reconnect -> clean stop.

Part D -- OTA push: the fake box grows the sat9 OTA contract
(ota_begin -> ota_ready, OTA_DATA chunks, ota_progress, ota_end ->
ota_ok / ota_error, ota_abort). A 200,000 B image must arrive
byte-identical in 8192 B chunks; the refusals (injected busy gate,
playback active, concurrent push, not connected) and the box's
own failure modes (refusal at begin, error at end, a begin that
is never answered, a link that dies mid-transfer) must all
surface as OtaErrors with the reason in the message.

Part E -- FirmwareTool: path hygiene (outside-root / missing /
empty / relative), the tool-layer busy gates (alarm ringing,
voice turn active), and a background update whose status walks
running -> done with the image byte-identical at the fake box.

The REAL box is never contacted (it is still on ESPHome) and the
production port 9876 is never bound. Run via ~/Documents/LyraOS/core/
satlink-bench.sh, on a /tmp copy of the tree.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import subprocess
import sys
import time
import wave
from pathlib import Path

from .audio_http import AudioHttpServer
from .config import CoreConfig
from .pipeline import PipelineDriver
from .satlink import (
    HEADER, MAGIC, TYPE_AUDIO_DOWN, TYPE_AUDIO_UP, TYPE_CTRL,
    TYPE_OTA_DATA, OtaError, SatBoxClient, build_frame, wav_to_pcm24,
)
from .session import CANNED_REPLY, VoiceSession
from .supervisor import CoreSupervisor
from .tools import FirmwareTool

BENCH_HOST = "127.0.0.1"
BENCH_PORT = 19876
HTTP_PORT = 18099
CLIP = Path(
    "~/Documents/LyraOS/modules/wakeword/data/clips/positives/"
    "wake_positive_20261111_061439_0.987.wav"
).expanduser()
POS_DIR = Path("~/Documents/LyraOS/modules/wakeword/data/clips/positives").expanduser()
LEDGER = Path("~/Documents/LyraOS/modules/wakeword/data/clips/clips_ledger.jsonl").expanduser()
SPOOL_DIR = Path("~/Documents/LyraOS/modules/wyoming/collector_spool").expanduser()
DEVICE_ID = "lyra-satgadget-01"


def _read_pcm(path: Path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as wf:
        return (wf.readframes(wf.getnframes()), wf.getframerate(),
                wf.getsampwidth(), wf.getnchannels())


def _newest(d: Path):
    files = [p for p in d.iterdir() if p.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest.name, newest.stat().st_mtime


def _last_line(path: Path) -> str:
    try:
        lines = path.read_text().strip().splitlines()
        return lines[-1] if lines else ""
    except FileNotFoundError:
        return ""


def _tone_pcm_prefix(seconds: float) -> bytes:
    """Reference decode of the alarm tone's head, same ffmpeg shape
    the SatBoxClient producer uses."""
    tone = Path("~/Documents/LyraOS/data/assets/alarm_homepod_15min.opus").expanduser()
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(tone),
         "-f", "s16le", "-ac", "1", "-ar", "24000", "-"],
        capture_output=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr[:200]
    return out.stdout[: int(seconds * 24000 * 2)]


# ---------------------------------------------------------------------------
# Fake box: sat_local.c's side of the wire
# ---------------------------------------------------------------------------

class FakeSatBox:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.volume = 0.6
        self.alarm_volume = 1.0
        self.seq = 0
        self.ctrl_received: list[dict] = []
        self.down_frames = 0
        self.down_pcm = bytearray()
        self.down_events: list[tuple[float, str]] = []  # (t, kind)
        self.pongs_received = 0
        self.pings_received = 0
        self.barge_ins = 0
        # OTA (sat9 contract): the image lands here chunk by chunk.
        # ota_mode: ok | refuse_begin | fail_end | slow_ready |
        # silent_ready | drop_mid.
        self.ota_mode = "ok"
        self.ota_begin: dict | None = None
        self.ota_image = bytearray()
        self.ota_chunks: list[int] = []
        self.ota_progress_sent = 0
        self.ota_end_seen = False
        self.ota_aborts = 0
        self._tasks: list[asyncio.Task] = []
        self._closed = False

    async def connect(self, timeout_s: float = 20.0) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                self.reader, self.writer = await asyncio.open_connection(
                    self.host, self.port
                )
                break
            except OSError:
                if time.monotonic() > deadline:
                    raise
                await asyncio.sleep(0.1)
        await self._send_ctrl({
            "cmd": "hello", "device_id": DEVICE_ID,
            "fw": "2026.11.11-sat7", "volume": self.volume,
            "alarm_volume": self.alarm_volume, "xvf_fw": "unknown",
        })
        self._tasks.append(asyncio.create_task(self._read_loop()))
        self._tasks.append(asyncio.create_task(self._ping_loop()))

    async def _send(self, ftype: int, payload: bytes) -> None:
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        assert self.writer is not None
        self.writer.write(build_frame(ftype, self.seq, payload))
        await self.writer.drain()

    async def _send_ctrl(self, msg: dict) -> None:
        await self._send(TYPE_CTRL, json.dumps(msg).encode())

    async def _ping_loop(self) -> None:
        # Firmware pings every 5 s; 2 s here keeps the bench snappy.
        try:
            while not self._closed:
                await asyncio.sleep(2.0)
                await self._send_ctrl({"cmd": "ping"})
        except (asyncio.CancelledError, ConnectionError, OSError):
            pass

    async def _read_loop(self) -> None:
        assert self.reader is not None
        try:
            while True:
                header = await self.reader.readexactly(HEADER.size)
                magic, ftype, _seq, _t, length = HEADER.unpack(header)
                assert magic == MAGIC, f"bad magic from core: {magic:#x}"
                payload = await self.reader.readexactly(length) if length else b""
                if ftype == TYPE_AUDIO_DOWN:
                    self.down_frames += 1
                    self.down_pcm.extend(payload)
                    self.down_events.append((time.monotonic(), "down"))
                elif ftype == TYPE_OTA_DATA:
                    self.ota_image.extend(payload)
                    self.ota_chunks.append(len(payload))
                    if self.ota_mode == "drop_mid" and len(self.ota_chunks) >= 3:
                        # The link dies mid-transfer: the core must
                        # report failure, not success or a hang.
                        assert self.writer is not None
                        self.writer.close()
                        continue
                    await self._send_ctrl({
                        "cmd": "ota_progress",
                        "received": len(self.ota_image),
                    })
                    self.ota_progress_sent += 1
                elif ftype == TYPE_CTRL:
                    msg = json.loads(payload.decode())
                    self.ctrl_received.append(msg)
                    kind = msg.get("cmd")
                    if kind == "ping":
                        self.pings_received += 1
                        await self._send_ctrl({"cmd": "pong"})
                    elif kind == "pong":
                        self.pongs_received += 1
                    elif kind == "get_status":
                        await self._send_ctrl({
                            "cmd": "status", "device_id": DEVICE_ID,
                            "uptime_s": 42, "volume": self.volume,
                            "alarm_volume": self.alarm_volume,
                            "call_state": "idle", "xvf_ok": True,
                        })
                    elif kind == "set_volume":
                        self.volume = float(msg["volume"])
                    elif kind == "barge_in":
                        self.barge_ins += 1
                        self.down_events.append((time.monotonic(), "barge_in"))
                    elif kind == "ota_begin":
                        self.ota_begin = msg
                        self.ota_image = bytearray()
                        self.ota_chunks = []
                        self.ota_end_seen = False
                        if self.ota_mode == "refuse_begin":
                            await self._send_ctrl(
                                {"cmd": "ota_error", "reason": "bench refusal"}
                            )
                        elif self.ota_mode == "silent_ready":
                            pass  # never answers: the core must time out
                        else:
                            if self.ota_mode == "slow_ready":
                                await asyncio.sleep(1.5)
                            await self._send_ctrl({"cmd": "ota_ready"})
                    elif kind == "ota_end":
                        self.ota_end_seen = True
                        digest = hashlib.sha256(bytes(self.ota_image)).hexdigest()
                        ok = (
                            self.ota_mode != "fail_end"
                            and self.ota_begin is not None
                            and len(self.ota_image) == self.ota_begin.get("size")
                            and digest == self.ota_begin.get("sha256")
                        )
                        if ok:
                            await self._send_ctrl({"cmd": "ota_ok"})
                        else:
                            await self._send_ctrl(
                                {"cmd": "ota_error",
                                 "reason": "sha mismatch (bench)"}
                            )
                    elif kind == "ota_abort":
                        self.ota_aborts += 1
                        await self._send_ctrl({"cmd": "ota_aborted"})
        except (asyncio.CancelledError, asyncio.IncompleteReadError,
                ConnectionError, OSError):
            pass

    async def send_audio_up(self, pcm: bytes, *, pace_s: float = 0.02) -> None:
        for off in range(0, len(pcm), 640):
            frame = pcm[off:off + 640]
            if len(frame) < 640:
                frame = frame + b"\x00" * (640 - len(frame))
            await self._send(TYPE_AUDIO_UP, frame)
            await asyncio.sleep(pace_s)

    async def close(self) -> None:
        self._closed = True
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self.writer is not None:
            try:
                self.writer.close()
            except Exception:
                pass
            self.writer = None

    def ctrl_kinds(self) -> list[str]:
        return [m.get("cmd", "?") for m in self.ctrl_received]


def _bench_cfg() -> CoreConfig:
    cfg = CoreConfig.load()
    cfg.transport = "satlink"
    cfg.satlink.bind_host = BENCH_HOST
    cfg.satlink.port = BENCH_PORT
    cfg.satlink.keepalive_s = 2.0
    cfg.http.bind = BENCH_HOST
    cfg.http.port = HTTP_PORT
    cfg.http.public_host = BENCH_HOST
    return cfg


async def _wait_until(pred, timeout_s: float, what: str) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for: {what}")


# ---------------------------------------------------------------------------
# Part A -- end-to-end turn over the wire
# ---------------------------------------------------------------------------

async def part_a(cfg: CoreConfig) -> dict:
    print("== Part A: end-to-end turn (fake box -> wake -> STT -> TTS -> PCM down) ==")
    timeline: list = []
    t0 = time.monotonic()

    def hook(kind: str, detail: dict) -> None:
        timeline.append((time.monotonic(), kind, detail))

    server = AudioHttpServer(cfg.http.bind, cfg.http.port)
    server.start()
    driver = PipelineDriver(cfg)
    session = VoiceSession(cfg, driver, server, on_event=hook)
    box = SatBoxClient(
        cfg,
        on_audio=session.handle_audio,
        on_start=session.handle_start,
        on_stop=session.handle_stop,
        audio_store=server.store,
    )
    fake = FakeSatBox(BENCH_HOST, BENCH_PORT)
    connect_task = asyncio.create_task(box.connect())
    await fake.connect()
    await asyncio.wait_for(connect_task, timeout=15)
    result: dict = {}
    try:
        assert box.is_connected, "box client not connected after hello"
        assert box.media_volume == 0.6, f"hello volume not cached: {box.media_volume}"
        result["hello_volume"] = box.media_volume
        print(f"[hello] device={box.device_name} volume={box.media_volume}")
        session.attach_box(box)  # type: ignore[arg-type]
        await session.start()
        await asyncio.sleep(0.5)  # wake link up before audio flows

        pcm, rate, width, channels = _read_pcm(CLIP)
        assert (rate, width, channels) == (16000, 2, 1), (rate, width, channels)
        print(f"[clip] {CLIP.name}: {len(pcm) / 32000:.2f}s of mic audio up")
        await fake.send_audio_up(pcm)
        await fake.send_audio_up(b"\x00\x00" * 16000 * 2)  # 2 s silence
        await _wait_until(
            lambda: any(
                d.get("name") == "run_end" for _t, k, d in timeline if k == "box_event"
            ) and session.state == "idle",
            60, "session run_end",
        )
        # The reply streams AFTER tts_end: wait for the down frames.
        await _wait_until(lambda: fake.down_frames > 0, 30, "reply AUDIO_DOWN frames")
        frames_at_check = fake.down_frames
        await asyncio.sleep(1.0)
        while fake.down_frames != frames_at_check:  # let the stream finish
            frames_at_check = fake.down_frames
            await asyncio.sleep(1.0)
        assert session.last_transcript, "empty transcript"
        result["transcript"] = session.last_transcript
        print(f"[stt] transcript={session.last_transcript!r}")

        kinds = [k for _t, k, _d in timeline]
        seq = [d.get("name") for _t, k, d in timeline if k == "box_event"]
        expected = ["run_start", "stt_start", "stt_vad_start", "stt_vad_end",
                    "stt_end", "intent_start", "intent_end", "tts_start",
                    "tts_end", "run_end"]
        assert "wake_forward" in kinds, "mic audio never forwarded to wake server"
        assert "detection" in kinds, "wake server never returned a Detection"
        assert kinds.index("wake_forward") < kinds.index("detection")
        assert seq == expected, f"box event order mismatch: {seq}"

        # The streamed PCM must BE the reply WAV, decoded.
        assert session.last_tts_url, "no TTS URL recorded"
        audio_id = session.last_tts_url.rsplit("/", 1)[-1][:-4]
        wav_bytes = server.store.get(audio_id)
        assert wav_bytes, "reply WAV missing from the store"
        expected_pcm = wav_to_pcm24(wav_bytes)
        got = bytes(fake.down_pcm)
        result["reply_wav_bytes"] = len(wav_bytes)
        result["reply_pcm_expected"] = len(expected_pcm)
        result["reply_pcm_received"] = len(got)
        print(f"[down] reply WAV {len(wav_bytes)} B -> PCM expected "
              f"{len(expected_pcm)} B, received {len(got)} B "
              f"in {fake.down_frames} frames")
        assert abs(len(got) - len(expected_pcm)) <= 960, \
            f"streamed PCM length off: got {len(got)}, expected {len(expected_pcm)}"
        common = min(len(got), len(expected_pcm)) - 960
        assert got[:common] == expected_pcm[:common], "streamed PCM payload mismatch"

        # Liveness both ways: the core ponged the box's pings and the
        # core's own keepalive pings reached the box.
        assert fake.pongs_received >= 1, "core never answered a box ping"
        assert fake.pings_received >= 1, "core keepalive ping never arrived"
        result["pongs_to_box"] = fake.pongs_received
        result["keepalive_pings_from_core"] = fake.pings_received
        print(f"[liveness] core->box pongs={fake.pongs_received} "
              f"pings={fake.pings_received}")
    finally:
        await session.stop()
        await box.disconnect()
        await fake.close()
        server.stop()
    print(f"Part A: PASS ({time.monotonic() - t0:.1f}s)")
    return result


# ---------------------------------------------------------------------------
# Part B -- media surface: volume, tone, stop, announce
# ---------------------------------------------------------------------------

async def part_b(cfg: CoreConfig) -> dict:
    print("== Part B: volume round-trip, alarm tone stream, stop, announce ==")
    server = AudioHttpServer(cfg.http.bind, cfg.http.port)
    server.start()
    driver = PipelineDriver(cfg)

    async def _noop_audio(data: bytes) -> None:
        pass

    box = SatBoxClient(cfg, on_audio=_noop_audio, audio_store=server.store)
    fake = FakeSatBox(BENCH_HOST, BENCH_PORT)
    connect_task = asyncio.create_task(box.connect())
    await fake.connect()
    await asyncio.wait_for(connect_task, timeout=15)
    result: dict = {}
    try:
        # Volume round-trip: hello cache -> set -> device applies ->
        # get_status reports the new value back.
        assert box.media_volume == 0.6
        box.media_set_volume(0.42)
        await _wait_until(lambda: fake.volume == 0.42, 5, "fake box applying set_volume")
        assert box.media_volume == 0.42, "optimistic volume cache not updated"
        await box._request_status(timeout=3.0)
        assert box.media_volume == 0.42, f"status round-trip: {box.media_volume}"
        result["volume_roundtrip"] = 0.42
        print("[volume] 0.6 (hello) -> set 0.42 -> status confirms 0.42")
        box.media_set_volume(0.6)
        await _wait_until(lambda: fake.volume == 0.6, 5, "volume restore")

        # Alarm tone: streams from the local opus via ffmpeg.
        expected_prefix = await asyncio.to_thread(_tone_pcm_prefix, 3.0)
        base_frames, base_bytes = fake.down_frames, len(fake.down_pcm)
        tone_url = f"http://{BENCH_HOST}:{HTTP_PORT}{cfg.alarm.tone_url_path}"
        box.media_play(tone_url)
        await _wait_until(
            lambda: fake.down_frames >= base_frames + 50, 15,
            "50 frames of alarm tone",
        )
        from aioesphomeapi.model import MediaPlayerState
        assert box.media_state == MediaPlayerState.PLAYING, box.media_state
        tone_pcm = bytes(fake.down_pcm[base_bytes:])
        common = min(len(tone_pcm), len(expected_prefix)) - 960
        assert common > 0 and tone_pcm[:common] == expected_prefix[:common], \
            "alarm tone PCM does not match the ffmpeg reference decode"
        result["tone_frames_before_stop"] = fake.down_frames - base_frames
        print(f"[tone] streaming; {result['tone_frames_before_stop']} frames "
              f"match the reference decode; media_state=PLAYING")

        # Stop: barge_in must land and frames must cease (allowing at
        # most the frame already on the wire).
        box.media_stop()
        await _wait_until(lambda: fake.barge_ins >= 1, 5, "barge_in at the box")
        frames_at_barge = fake.down_frames
        await asyncio.sleep(0.5)
        extra = fake.down_frames - frames_at_barge
        assert extra <= 1, f"{extra} frames arrived after barge_in (drain bound is ~1)"
        assert box.media_state == MediaPlayerState.IDLE, box.media_state
        result["frames_after_barge_in"] = extra
        print(f"[stop] barge_in received; frames after it: {extra}; media_state=IDLE")

        # Announce: completes with success once its stream drains.
        wav_bytes, rate, _w, _c = await driver.synthesize_speech(
            "Satlink bench announcement."
        )
        assert rate == 24000, rate
        audio_id = server.store.put(wav_bytes)
        url = server.url_for(audio_id, BENCH_HOST)
        base_frames, base_bytes = fake.down_frames, len(fake.down_pcm)
        res = await box.announce(url, text="Satlink bench announcement.", timeout=30.0)
        assert getattr(res, "success", None) is True, res
        expected_pcm = wav_to_pcm24(wav_bytes)
        got = bytes(fake.down_pcm[base_bytes:])
        assert abs(len(got) - len(expected_pcm)) <= 960
        common = min(len(got), len(expected_pcm)) - 960
        assert got[:common] == expected_pcm[:common], "announce PCM mismatch"
        result["announce_frames"] = fake.down_frames - base_frames
        print(f"[announce] success; {result['announce_frames']} frames streamed")
    finally:
        await box.disconnect()
        await fake.close()
        server.stop()
    print("Part B: PASS")
    return result


# ---------------------------------------------------------------------------
# Part C -- supervisor integration
# ---------------------------------------------------------------------------

async def part_c(cfg: CoreConfig) -> dict:
    print("== Part C: supervisor connect / drop / reconnect / stop ==")

    class _NullDriver:
        pass

    session = VoiceSession(cfg, _NullDriver(), None)  # type: ignore[arg-type]
    supervisor = CoreSupervisor(
        cfg, session,
        box_factory=lambda: SatBoxClient(  # type: ignore[arg-type]
            cfg, on_audio=session.handle_audio
        ),
        backoff_initial_s=0.05, backoff_max_s=0.2, poll_s=0.02,
    )
    run_task = asyncio.create_task(supervisor.run())
    fake1 = FakeSatBox(BENCH_HOST, BENCH_PORT)
    fake2 = FakeSatBox(BENCH_HOST, BENCH_PORT)
    try:
        await fake1.connect()
        await _wait_until(
            lambda: supervisor.successful_connects == 1, 15, "first supervised connect"
        )
        assert session._box is not None and session._box.is_connected
        await fake1.close()  # the box drops and stays gone (firmware
        # backoff): the supervisor must notice, cycle the client and
        # be listening again before the box returns.
        await _wait_until(
            lambda: supervisor.connect_attempts == 2, 15,
            "supervisor retry after drop",
        )
        await fake2.connect()
        await _wait_until(
            lambda: supervisor.successful_connects == 2, 20, "supervised reconnect"
        )
        supervisor.stop()
        await asyncio.wait_for(run_task, timeout=10)
    finally:
        await fake1.close()
        await fake2.close()
        if not run_task.done():
            run_task.cancel()
    assert supervisor.connect_attempts == 2, supervisor.connect_attempts
    result = {
        "attempts": supervisor.connect_attempts,
        "successful": supervisor.successful_connects,
    }
    print(f"[supervisor] attempts={result['attempts']} successful={result['successful']}")
    print("Part C: PASS")
    return result



# ---------------------------------------------------------------------------
# Part D -- OTA push: contract, byte-identity, refusals, box errors
# ---------------------------------------------------------------------------

OTA_IMG_BYTES = bytes((i * 31 + 7) % 256 for i in range(200_000))
OTA_IMG_SHA = hashlib.sha256(OTA_IMG_BYTES).hexdigest()
OTA_IMG_CHUNKS = [8192] * 24 + [len(OTA_IMG_BYTES) - 24 * 8192]


async def _expect_ota_error(coro, needle: str) -> str:
    try:
        await coro
    except OtaError as exc:
        assert needle in str(exc), f"OtaError {exc!r} lacks {needle!r}"
        return str(exc)
    raise AssertionError(f"ota_push unexpectedly succeeded (wanted {needle!r})")


async def part_d(cfg: CoreConfig) -> dict:
    print("== Part D: OTA push (byte-identical, chunking, refusals, box errors) ==")
    server = AudioHttpServer(cfg.http.bind, cfg.http.port)
    server.start()

    async def _noop_audio(data: bytes) -> None:
        pass

    img_path = Path("/tmp/lyra-ota-bench.bin")
    img_path.write_bytes(OTA_IMG_BYTES)
    gate: dict = {"reason": None}
    result: dict = {}
    box = SatBoxClient(
        cfg, on_audio=_noop_audio, audio_store=server.store,
        busy_check=lambda: gate["reason"],
    )
    fake = FakeSatBox(BENCH_HOST, BENCH_PORT)
    connect_task = asyncio.create_task(box.connect())
    await fake.connect()
    await asyncio.wait_for(connect_task, timeout=15)
    try:
        # Happy path: contract + byte identity + chunk shape.
        res = await box.ota_push(img_path)
        assert res.ok and res.size == len(OTA_IMG_BYTES), res
        assert res.sha256 == OTA_IMG_SHA
        assert res.chunks == len(OTA_IMG_CHUNKS), res.chunks
        assert bytes(fake.ota_image) == OTA_IMG_BYTES, "image differs at the box"
        assert hashlib.sha256(bytes(fake.ota_image)).hexdigest() == res.sha256
        assert fake.ota_chunks == OTA_IMG_CHUNKS, fake.ota_chunks
        assert fake.ota_end_seen and fake.ota_progress_sent >= 1
        result["happy"] = {"size": res.size, "chunks": res.chunks,
                           "sha256": res.sha256}
        print(f"[ota] happy: {res.size} B in {res.chunks} chunks "
              f"(8192 B x 24 + {OTA_IMG_CHUNKS[-1]} B tail), sha256 "
              f"{res.sha256[:16]}... byte-identical at the fake box")

        # Busy gate (the composition's alarm/turn check, injected).
        begins = lambda: sum(  # noqa: E731
            1 for m in fake.ctrl_received if m.get("cmd") == "ota_begin"
        )
        before = begins()
        gate["reason"] = "the alarm is ringing"
        msg = await _expect_ota_error(box.ota_push(img_path), "alarm is ringing")
        gate["reason"] = None
        assert begins() == before, "a refused push still sent ota_begin"
        result["busy_gate"] = msg
        print(f"[ota] busy gate refused before any byte: {msg!r}")

        # Playback-active gate (the client's own state).
        box._playing = True
        msg = await _expect_ota_error(box.ota_push(img_path), "playback is active")
        box._playing = False
        result["playing_gate"] = msg
        print(f"[ota] playback gate refused: {msg!r}")

        # A second concurrent push is refused; the first completes.
        fake.ota_mode = "slow_ready"
        first = asyncio.create_task(box.ota_push(img_path))
        await asyncio.sleep(0.3)
        assert box.ota_active
        msg = await _expect_ota_error(box.ota_push(img_path), "already in progress")
        res2 = await asyncio.wait_for(first, timeout=20)
        assert res2.ok
        fake.ota_mode = "ok"
        result["concurrent_gate"] = msg
        print(f"[ota] concurrent push refused ({msg!r}); the first completed")

        # The box refuses at ota_begin.
        fake.ota_mode = "refuse_begin"
        msg = await _expect_ota_error(box.ota_push(img_path), "bench refusal")
        fake.ota_mode = "ok"
        result["refuse_begin"] = msg
        print(f"[ota] box refusal at begin surfaced: {msg!r}")

        # The box errors at ota_end (its verification failed): the
        # bytes arrived fine, the verdict is what failed.
        fake.ota_mode = "fail_end"
        msg = await _expect_ota_error(box.ota_push(img_path), "sha mismatch")
        assert bytes(fake.ota_image) == OTA_IMG_BYTES
        fake.ota_mode = "ok"
        result["fail_end"] = msg
        print(f"[ota] box error at end surfaced: {msg!r}")

        # The box never answers ota_begin: the ready timeout fires.
        fake.ota_mode = "silent_ready"
        t0 = time.monotonic()
        msg = await _expect_ota_error(box.ota_push(img_path), "timed out")
        waited = time.monotonic() - t0
        fake.ota_mode = "ok"
        result["silent_ready"] = {"error": msg, "waited_s": round(waited, 1)}
        print(f"[ota] silent box -> timeout after {waited:.1f}s: {msg!r}")
    finally:
        await box.disconnect()
        await fake.close()

    # The link dies mid-transfer: failure, not a hang or a success.
    box2 = SatBoxClient(cfg, on_audio=_noop_audio, audio_store=server.store)
    fake2 = FakeSatBox(BENCH_HOST, BENCH_PORT)
    fake2.ota_mode = "drop_mid"
    connect_task = asyncio.create_task(box2.connect())
    await fake2.connect()
    await asyncio.wait_for(connect_task, timeout=15)
    try:
        msg = await _expect_ota_error(box2.ota_push(img_path), "disconnect")
        result["drop_mid"] = msg
        print(f"[ota] mid-transfer drop reported: {msg!r}")
    finally:
        await box2.disconnect()
        await fake2.close()
        server.stop()

    # Not connected at all.
    box3 = SatBoxClient(cfg, on_audio=_noop_audio, audio_store=server.store)
    msg = await _expect_ota_error(box3.ota_push(img_path), "not connected")
    result["not_connected"] = msg
    print(f"[ota] disconnected push refused: {msg!r}")
    img_path.unlink(missing_ok=True)
    print("Part D: PASS")
    return result


# ---------------------------------------------------------------------------
# Part E -- FirmwareTool: gates, background update, status reporting
# ---------------------------------------------------------------------------

async def part_e(cfg: CoreConfig) -> dict:
    print("== Part E: FirmwareTool gates + background update + status ==")
    server = AudioHttpServer(cfg.http.bind, cfg.http.port)
    server.start()

    async def _noop_audio(data: bytes) -> None:
        pass

    class _AlarmStub:
        def __init__(self) -> None:
            self.ringing = False

        def summary(self) -> dict:
            return {"ringing": self.ringing}

    class _SessionStub:
        state = "idle"

    root = Path("/tmp/lyra-ota-bench-root")
    root.mkdir(parents=True, exist_ok=True)
    img_path = root / "fw.bin"
    img_path.write_bytes(OTA_IMG_BYTES)
    empty_path = root / "empty.bin"
    empty_path.write_bytes(b"")

    box = SatBoxClient(cfg, on_audio=_noop_audio, audio_store=server.store)
    fake = FakeSatBox(BENCH_HOST, BENCH_PORT)
    connect_task = asyncio.create_task(box.connect())
    await fake.connect()
    await asyncio.wait_for(connect_task, timeout=15)
    alarm_stub = _AlarmStub()
    session_stub = _SessionStub()
    tool = FirmwareTool(
        box_getter=lambda: box,
        alarm=alarm_stub,
        session=session_stub,
        allowed_roots=[root],
    )
    result: dict = {}
    try:
        st = tool.update_status()
        assert st.ok and st.state["status"] == "idle", st
        result["initial_status"] = st.state["status"]

        # Path hygiene: outside the roots / missing / empty / relative.
        r = await tool.update("/etc/hostname")
        assert not r.ok and fake.ota_begin is None, r
        result["outside_root"] = r.confirmation
        r = await tool.update(str(root / "nope.bin"))
        assert not r.ok, r
        result["missing"] = r.confirmation
        r = await tool.update(str(empty_path))
        assert not r.ok, r
        result["empty"] = r.confirmation
        r = await tool.update("relative/path.bin")
        assert not r.ok, r
        result["relative"] = r.confirmation
        print("[tool] path hygiene refusals: outside-root, missing, empty, relative")

        # Busy gates at the tool layer; nothing reaches the box.
        alarm_stub.ringing = True
        r = await tool.update(str(img_path))
        assert not r.ok and "ringing" in r.confirmation, r
        alarm_stub.ringing = False
        result["ringing_gate"] = r.confirmation
        session_stub.state = "capturing"
        r = await tool.update(str(img_path))
        assert not r.ok and "voice turn" in r.confirmation, r
        session_stub.state = "idle"
        result["turn_gate"] = r.confirmation
        assert fake.ota_begin is None, "a gated update still reached the box"
        print("[tool] busy gates refused: alarm ringing, voice turn active")

        # Happy path: update returns at once; status walks to done.
        r = await tool.update(str(img_path))
        assert r.ok and r.state["status"] == "running", r
        await _wait_until(
            lambda: tool.update_status().state["status"] == "done",
            20, "tool update completion",
        )
        st = tool.update_status()
        assert st.ok and "sha256" in (st.state["detail"] or ""), st
        assert bytes(fake.ota_image) == OTA_IMG_BYTES
        result["done_detail"] = st.state["detail"]
        print(f"[tool] background update done: {st.state['detail']}")
    finally:
        await box.disconnect()
        await fake.close()
        server.stop()
        img_path.unlink(missing_ok=True)
        empty_path.unlink(missing_ok=True)
    print("Part E: PASS")
    return result

async def run() -> int:
    cfg = _bench_cfg()
    before_clip = _newest(POS_DIR)
    before_ledger = _last_line(LEDGER)
    before_spool = sorted(p.name for p in SPOOL_DIR.iterdir()) if SPOOL_DIR.exists() else []
    report: dict = {"reply_text": CANNED_REPLY}
    report["part_a"] = await part_a(cfg)
    report["part_b"] = await part_b(cfg)
    report["part_c"] = await part_c(cfg)
    report["part_d"] = await part_d(cfg)
    report["part_e"] = await part_e(cfg)
    # Production collector side effect (by design, as in the session
    # bench): the replayed genuine positive is filed; observe only.
    await asyncio.sleep(8)
    after_clip = _newest(POS_DIR)
    after_ledger = _last_line(LEDGER)
    after_spool = sorted(p.name for p in SPOOL_DIR.iterdir()) if SPOOL_DIR.exists() else []
    report["collector"] = {
        "new_positive_clip": (after_clip[0] if after_clip and after_clip != before_clip else None),
        "new_ledger_line": after_ledger if after_ledger != before_ledger else None,
        "spool_before": before_spool, "spool_after": after_spool,
    }
    print(f"[collector] new clip: {report['collector']['new_positive_clip']}")
    print("===SATLINK_BENCH_JSON===")
    print(json.dumps(report, indent=2))
    print("===SATLINK_BENCH_PASS===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
