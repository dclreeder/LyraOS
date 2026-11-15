"""Sat-link transport for the Lyra core (custom-firmware revival,
Stage 1, 2026-11-11).

The speaker box's custom firmware (sat7, revived from the sat6e tree)
is a plain TCP client speaking the sat protocol the archived Lyra Box
satellite firmware used; the core LISTENS and the box connects to it.
This module is the core-side endpoint: a duck-typed replacement for
lyra_core.box_client.BoxClient exposing the same surface the session,
supervisor, alarm and announce path use:

  connect() / disconnect() / is_connected / connected
  media_volume / media_state / media_play(url) / media_stop() /
      media_set_volume(v)
  announce(url, text, timeout) -> result object with a .success attr
  ota_push(image_path) -> OtaResult (OTA extra, NOT part of the
      BoxClient surface: the ESPHome client does not grow one)
  evt_run_start / evt_run_end / evt_stt_start / evt_stt_vad_start /
      evt_stt_vad_end / evt_stt_end(text) / evt_intent_start /
      evt_intent_end / evt_tts_start(text) / evt_tts_end(url) /
      evt_error(code, message)

Wire protocol (sat_local.c / sat_proto.h, as built in sat7):

  frame = [magic u16 0x4C53][type u8][seq u32][t_remote_us u64]
          [len u32][payload]; header big-endian, 19 bytes total.
  0x01 AUDIO_UP   box -> core: 640 B PCM16 LE mono @ 16 kHz (20 ms)
  0x02 AUDIO_DOWN core -> box: 960 B PCM16 LE mono @ 24 kHz (20 ms)
  0x03 CTRL       either way: one JSON object per frame. The
          discriminator key is "cmd" (sat_local.c handle_ctrl);
          there is no "t" key on this wire.
  0x04 OTA_DATA   core -> box: raw firmware image bytes, sent only
          inside an OTA push (ota_push below): 8192 B chunks
          between the ota_begin and ota_end CTRL messages.

  box -> core CTRL: hello {device_id, fw, volume, alarm_volume},
      status {... volume ...}, ping, pong, call_state, alerts_state.
  core -> box CTRL: ping, pong, hello (re-request), barge_in,
      set_volume {volume: fraction 0..1}, set_alarm_volume,
      get_status. start_call and the alarms_* commands are NEVER
      sent: the realtime cloud-call engine is compiled out of sat7
      and the alarm lives in the core.

Playback is STREAMED PCM: session replies, /announce audio and the
alarm tone are decoded to 24 kHz mono PCM on the Fedora and sent as
AUDIO_DOWN frames paced 20 ms apart -- no HTTP URLs on the wire, no
box-side decoding (the two things that failed under ESPHome).

Playback stop (alarm kill mid-ring, snooze) = a barge_in CTRL (the
box flushes its playback FIFO on receipt) plus the core ceasing to
send. Worst-case drain after a barge_in is the one frame currently
being written (~20 ms); without a barge_in, ceasing to send drains
at most the FIFO's ~10 queued frames (~200 ms). A new playback
stream replaces any active one (barge-in semantics), matching how
the ESPHome media player treated a new play command.

The ESPHome VA event choreography has NO equivalent on this wire:
the sat firmware has no run/TTS concept -- a turn is mic audio in
and PCM audio out, and it ends when the stream ends. Every evt_*
method is therefore a no-op except evt_tts_end(url), which starts
streaming the reply (evt_tts_start's text is NOT sent: the protocol
carries no TTS text event). One exception: evt_wake_ack streams a
short generated earcon the moment the session reports a wake
Detection. The box's LEDs stay on the firmware's stock SDK
patterns; the protocol has no LED control.

Volume lives on the device (NVS) per the standing hardware rule:
hello/status report the stored volume as a fraction and set_volume
writes it back to NVS. The core caches the last reported value and
the alarm's borrow/restore rides on media_volume / media_set_volume
unchanged.

Firmware updates are PUSHED over this same link (sat9+ firmware):
CTRL ota_begin {size, sha256} -> ota_ready from the box; the image
as OTA_DATA frames; CTRL ota_end -> the box verifies the byte
count and SHA-256, swaps slots and answers ota_ok, then reboots
into the new image (the supervisor's reconnect produces a fresh
hello, whose fw= the hello handler already logs). ota_push refuses
while disconnected, while another transfer runs, while playback is
active, or when the injected busy_check reports the box busy (the
alarm ringing, a voice turn in flight): an OTA ends in a reboot,
so it never lands mid-ring or mid-speech.

Liveness: the firmware drops the connection after 15 s with no
frames received, and it pings every 5 s. The core answers box pings
with pongs AND sends its own ping every keepalive_s, so the link
stays warm in both directions.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import math
import struct
import time
import urllib.request
import wave
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from aioesphomeapi.model import MediaPlayerState

from .audio_http import AudioStore
from .box_client import ERROR_STT_NO_TEXT, NotConnectedError  # noqa: F401
from .config import CoreConfig

_LOGGER = logging.getLogger("lyra_core.satlink")

MAGIC = 0x4C53
HEADER = struct.Struct(">HBIQI")  # magic, type, seq, t_remote_us, len
MAX_PAYLOAD = 65536
TYPE_AUDIO_UP = 0x01
TYPE_AUDIO_DOWN = 0x02
TYPE_CTRL = 0x03
TYPE_OTA_DATA = 0x04

OTA_CHUNK_BYTES = 8192
OTA_READY_TIMEOUT_S = 15.0
OTA_END_TIMEOUT_S = 30.0

MIC_RATE = 16000
MIC_FRAME_BYTES = 640  # 20 ms PCM16 mono @ 16 kHz
PLAY_RATE = 24000
PLAY_FRAME_BYTES = 960  # 20 ms PCM16 mono @ 24 kHz
FRAME_PERIOD_S = 0.020
PLAY_QUEUE_FRAMES = 400  # 8 s of buffered playback

AudioCallback = Callable[[bytes], Awaitable[None]]
StartCallback = Callable[[str, "str | None"], Awaitable[None]]
StopCallback = Callable[[bool], Awaitable[None]]
BusyCheck = Callable[[], "str | None"]


def build_frame(frame_type: int, seq: int, payload: bytes) -> bytes:
    return HEADER.pack(
        MAGIC, frame_type, seq & 0xFFFFFFFF, time.monotonic_ns() // 1000,
        len(payload),
    ) + payload


def wav_to_pcm24(wav_bytes: bytes) -> bytes:
    """Decode a WAV (any rate, mono/stereo, 16-bit) to raw PCM16 LE
    mono @ 24 kHz -- the AUDIO_DOWN payload format."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        rate, width, channels = wf.getframerate(), wf.getsampwidth(), wf.getnchannels()
        pcm = wf.readframes(wf.getnframes())
    return pcm_to_24k_mono(pcm, rate=rate, width=width, channels=channels)


def pcm_to_24k_mono(pcm: bytes, *, rate: int, width: int, channels: int) -> bytes:
    if width != 2:
        raise ValueError(f"only 16-bit PCM is supported (width={width})")
    if channels not in (1, 2):
        raise ValueError(f"unsupported channel count {channels}")
    samples = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    if channels == 2:
        samples = tuple(
            (samples[i] + samples[i + 1]) // 2
            for i in range(0, len(samples) - 1, 2)
        )
    if rate == PLAY_RATE:
        mono = samples
    elif rate <= 0:
        raise ValueError(f"invalid sample rate {rate}")
    else:
        # Linear-interpolation resample (safety net; Kokoro already
        # synthesizes at 24 kHz, so the happy path never resamples).
        out_len = int(len(samples) * PLAY_RATE / rate)
        mono_list = []
        for i in range(out_len):
            pos = i * rate / PLAY_RATE
            lo = int(pos)
            hi = min(lo + 1, len(samples) - 1)
            frac = pos - lo
            mono_list.append(int(samples[lo] * (1 - frac) + samples[hi] * frac))
        mono = tuple(mono_list)
    return struct.pack(f"<{len(mono)}h", *mono)


# Wake-ack chime (2026-11-12): played the moment the wake gate
# opens so a gated wake is audible instead of silent limbo.
# Generated, not an asset file -- one less thing to lose in a
# folder move. Two ascending notes (E6 -> A6), the classic
# "ready to listen" shape: Daniel's verdict on the first single
# 0.15 FS blip was "too subtle", so the chime peaks at 0.35 FS
# (the box's everyday volume still scales it on top of that),
# with Hann envelopes so it stays click-free.
WAKE_ACK_NOTES = ((1318.5, 0.0, 0.09), (1760.0, 0.07, 0.16))
WAKE_ACK_AMPLITUDE = 0.35
# After the box plays anything (a reply, the morning note), its
# own voice leaks into the mic and can re-fire the gate: the
# journal for 2026-11-12 17:02 shows the spoken reply "The
# current time is 17:02." doing exactly that, gate score 0.981,
# one second after it played. A wake ack inside this window
# after playback ends would just be Lyra chiming at her own
# echo, so it is suppressed (verify drops those turns anyway).
WAKE_ACK_ECHO_SUPPRESS_S = 2.0


def _make_wake_ack_pcm() -> bytes:
    total_s = max(start + dur for _f, start, dur in WAKE_ACK_NOTES)
    n = int(PLAY_RATE * total_s)
    samples = [0.0] * n
    for freq, start_s, dur_s in WAKE_ACK_NOTES:
        start = int(PLAY_RATE * start_s)
        count = int(PLAY_RATE * dur_s)
        for i in range(count):
            env = 0.5 - 0.5 * math.cos(
                2.0 * math.pi * i / max(1, count - 1)
            )
            samples[start + i] += (
                WAKE_ACK_AMPLITUDE * env
                * math.sin(2.0 * math.pi * freq * i / PLAY_RATE)
            )
    return struct.pack(
        f"<{n}h",
        *(max(-32768, min(32767, int(32767 * v))) for v in samples),
    )


WAKE_ACK_PCM = _make_wake_ack_pcm()


class _StreamReplaced(Exception):
    """A producer noticed a newer stream (or a stop) owns playback."""


@dataclass
class SatAnnounceResult:
    success: bool


class OtaError(Exception):
    """A firmware update was refused or failed; the message says why."""


@dataclass
class OtaResult:
    ok: bool
    size: int
    sha256: str
    chunks: int


class SatBoxClient:
    """Owns the sat-link listener and the one box connection on it.

    Duck-typed BoxClient: construct with the session's callbacks,
    await connect() (listens, then waits for the box to connect and
    say hello), and the supervisor polls is_connected exactly as it
    does for the ESPHome client.
    """

    def __init__(
        self,
        config: CoreConfig,
        *,
        on_audio: AudioCallback,
        on_start: StartCallback | None = None,
        on_stop: StopCallback | None = None,
        audio_store: AudioStore | None = None,
        busy_check: BusyCheck | None = None,
    ) -> None:
        self._cfg = config.satlink
        self._full_config = config
        self._on_audio = on_audio
        self._on_start = on_start
        self._on_stop = on_stop
        self._store = audio_store
        # Optional external busy gate for ota_push (the composition
        # wires the alarm-ringing / voice-turn check here).
        self._busy_check = busy_check
        self.device_name: str | None = None
        self.entity_count: int | None = None  # no entities on this wire
        # Connection state.
        self._server: asyncio.Server | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._tx_lock = asyncio.Lock()
        self._tx_seq = 0
        self._hello_event = asyncio.Event()
        self._status_event = asyncio.Event()
        self._hello: dict = {}
        # OTA push state: one transfer at a time; the box's terminal
        # OTA replies are routed to this queue by _handle_ctrl.
        self._ota_active = False
        self._ota_replies: asyncio.Queue = asyncio.Queue()
        # Device-reported volume (fraction 0..1) from hello/status;
        # None until the first report on this connection.
        self._media_volume: float | None = None
        # Playback engine.
        self._play_queue: asyncio.Queue = asyncio.Queue(maxsize=PLAY_QUEUE_FRAMES)
        self._play_gen = 0
        self._writer_task: asyncio.Task | None = None
        self._producer_task: asyncio.Task | None = None
        self._playing = False
        self._play_done: asyncio.Future | None = None
        # Monotonic time the last playback stream drained/stopped;
        # the wake ack stays quiet for a short window after it
        # (see WAKE_ACK_ECHO_SUPPRESS_S).
        self._last_playback_end: float = 0.0

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Start listening and wait for the box to connect + hello.

        The firmware reconnects with backoff capped at 30 s, so once
        the listener is up the box arrives promptly; log while we
        wait so a missing box is visible in the journal."""
        if self._server is not None:
            raise RuntimeError("already connected")
        self._server = await asyncio.start_server(
            self._on_client_connected, self._cfg.bind_host, self._cfg.port
        )
        _LOGGER.info(
            "satlink listening on %s:%d; waiting for the box",
            self._cfg.bind_host, self._cfg.port,
        )
        waited = 0
        while not self._hello_event.is_set():
            try:
                await asyncio.wait_for(self._hello_event.wait(), timeout=10.0)
            except TimeoutError:
                waited += 10
                _LOGGER.info("satlink: still waiting for the box (%ds)", waited)
        # Hello already carries the volume; a status refresh confirms
        # the round-trip on this connection (best effort).
        try:
            await self._request_status(timeout=2.0)
        except Exception as exc:
            _LOGGER.debug("initial get_status unanswered: %s", exc)
        self._writer_task = asyncio.create_task(
            self._playback_loop(), name="lyra-satlink-playback"
        )
        self._keepalive_task = asyncio.create_task(
            self._keepalive_loop(), name="lyra-satlink-keepalive"
        )
        _LOGGER.info(
            "satlink: box connected: %s", self.device_name or "unknown"
        )

    async def disconnect(self) -> None:
        """Stop playback, close the box connection, stop listening."""
        self._stop_playback_local()
        for task in (self._keepalive_task, self._writer_task, self._reader_task):
            if task is not None:
                task.cancel()
        self._keepalive_task = self._writer_task = self._reader_task = None
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
            self._writer = None
        self._reader = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None
        self._hello_event.clear()
        self._media_volume = None
        self._ota_active = False
        _LOGGER.info("satlink disconnected + listener closed")

    @property
    def connected(self) -> bool:
        return self._writer is not None

    @property
    def is_connected(self) -> bool:
        """Live-connection truth (the supervisor's drop detector)."""
        return self._writer is not None and not self._writer.is_closing()

    @property
    def ota_active(self) -> bool:
        """True while an ota_push transfer owns the wire."""
        return self._ota_active

    # -- connection plumbing ---------------------------------------------------

    def _on_client_connected(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = writer.get_extra_info("peername")
        if self._writer is not None and not self._writer.is_closing():
            # Single-client link (the archived sat_link semantics): a
            # new connection replaces a stale one.
            _LOGGER.warning("satlink: new box connection from %s replaces the old", peer)
            try:
                self._writer.close()
            except Exception:
                pass
            self._media_volume = None
            self.device_name = None
        self._reader, self._writer = reader, writer
        _LOGGER.info("satlink: box TCP connection from %s", peer)
        if self._reader_task is not None:
            self._reader_task.cancel()
        self._reader_task = asyncio.create_task(
            self._reader_loop(reader), name="lyra-satlink-reader"
        )

    async def _reader_loop(self, reader: asyncio.StreamReader) -> None:
        try:
            while True:
                header = await reader.readexactly(HEADER.size)
                magic, ftype, _seq, _t_us, length = HEADER.unpack(header)
                if magic != MAGIC:
                    _LOGGER.warning("satlink: bad magic 0x%04x; dropping link", magic)
                    break
                if length > MAX_PAYLOAD:
                    _LOGGER.warning("satlink: oversized frame %d; dropping link", length)
                    break
                payload = await reader.readexactly(length) if length else b""
                if ftype == TYPE_AUDIO_UP:
                    await self._on_audio(payload)
                elif ftype == TYPE_CTRL:
                    await self._handle_ctrl(payload)
                else:
                    _LOGGER.debug("satlink: ignoring frame type 0x%02x", ftype)
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, ConnectionError) as exc:
            _LOGGER.info("satlink: box connection ended (%s)", exc.__class__.__name__)
        except Exception:
            _LOGGER.exception("satlink: reader loop failed")
        finally:
            if self._reader is reader:
                self._mark_dead()

    def _mark_dead(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._hello_event.clear()
        self._stop_playback_local()

    async def _handle_ctrl(self, payload: bytes) -> None:
        try:
            msg = json.loads(payload.decode("utf-8"))
        except Exception:
            _LOGGER.warning("satlink: unparseable CTRL payload (%d B)", len(payload))
            return
        kind = msg.get("cmd")
        if kind == "hello":
            self._hello = msg
            self.device_name = msg.get("device_id")
            if isinstance(msg.get("volume"), (int, float)):
                self._media_volume = float(msg["volume"])
            expected = self._cfg.expected_device_id
            if expected and self.device_name != expected:
                _LOGGER.warning(
                    "satlink: hello from unexpected device %r (expected %r)",
                    self.device_name, expected,
                )
            _LOGGER.info(
                "satlink: hello device=%s fw=%s volume=%s alarm_volume=%s",
                self.device_name, msg.get("fw"),
                msg.get("volume"), msg.get("alarm_volume"),
            )
            self._hello_event.set()
        elif kind == "status":
            if isinstance(msg.get("volume"), (int, float)):
                self._media_volume = float(msg["volume"])
            self._status_event.set()
            _LOGGER.debug("satlink: status %s", msg)
        elif kind == "ping":
            asyncio.create_task(self._send_ctrl({"cmd": "pong"}))
        elif kind == "pong":
            pass  # liveness only
        elif kind == "ota_progress":
            _LOGGER.info(
                "satlink ota: box reports %s bytes received",
                msg.get("received"),
            )
        elif kind in ("ota_ready", "ota_ok", "ota_error", "ota_aborted"):
            self._ota_replies.put_nowait(msg)
        else:
            _LOGGER.debug("satlink: ctrl %s", msg)

    async def _keepalive_loop(self) -> None:
        """The firmware drops the link after 15 s of inbound silence;
        ping on an interval comfortably inside that."""
        try:
            while True:
                await asyncio.sleep(self._cfg.keepalive_s)
                if self.is_connected:
                    await self._send_ctrl({"cmd": "ping"})
        except asyncio.CancelledError:
            pass

    async def _send_frame(self, frame_type: int, payload: bytes) -> None:
        writer = self._writer
        if writer is None or writer.is_closing():
            raise NotConnectedError("satlink box is not connected")
        self._tx_seq = (self._tx_seq + 1) & 0xFFFFFFFF
        frame = build_frame(frame_type, self._tx_seq, payload)
        async with self._tx_lock:
            writer.write(frame)
            await writer.drain()

    async def _send_ctrl(self, msg: dict) -> None:
        await self._send_frame(TYPE_CTRL, json.dumps(msg).encode("utf-8"))

    async def _request_status(self, timeout: float = 2.0) -> dict | None:
        self._status_event.clear()
        await self._send_ctrl({"cmd": "get_status"})
        await asyncio.wait_for(self._status_event.wait(), timeout=timeout)
        return self._hello

    # -- media player surface (alarm + tools) ----------------------------------

    @property
    def media_volume(self) -> float | None:
        """Last volume reported by the box (fraction 0..1), or None
        before the first hello/status on this connection. Updated
        optimistically by media_set_volume: the firmware applies and
        persists the value but sends no reply."""
        return self._media_volume

    @property
    def media_state(self) -> MediaPlayerState:
        return MediaPlayerState.PLAYING if self._playing else MediaPlayerState.IDLE

    def media_set_volume(self, volume: float) -> None:
        if not self.is_connected:
            raise NotConnectedError("satlink box is not connected")
        v = max(0.0, min(1.0, float(volume)))
        self._media_volume = v
        self._schedule(self._send_ctrl({"cmd": "set_volume", "volume": v}))

    def media_play(self, media_url: str) -> None:
        """Start streaming the audio at media_url (alarm tone, reply).

        Sync like BoxClient.media_play: connection problems raise
        immediately; decode/stream errors surface in the playback
        task's log. Replaces any active stream (barge-in)."""
        if not self.is_connected:
            raise NotConnectedError("satlink box is not connected")
        self._schedule(self._play_url(media_url))

    def media_stop(self) -> None:
        """Stop playback now: drop queued frames locally and send
        barge_in so the box flushes its FIFO too."""
        if not self.is_connected:
            raise NotConnectedError("satlink box is not connected")
        self._stop_playback_local()
        self._schedule(self._send_ctrl({"cmd": "barge_in"}))

    # -- announcements -----------------------------------------------------------

    async def announce(self, media_url: str, text: str = "", timeout: float = 30.0):
        """Stream the audio at media_url and await its completion.

        There is no announce pipeline on this wire: the streamed PCM
        IS the announcement. success=False means the stream did not
        complete inside timeout (or the link dropped mid-stream)."""
        if not self.is_connected:
            raise NotConnectedError("satlink box is not connected")
        done = await self._play_url(media_url)
        try:
            await asyncio.wait_for(done, timeout=timeout)
            return SatAnnounceResult(success=True)
        except TimeoutError:
            _LOGGER.warning("satlink announce timed out after %.0fs", timeout)
            return SatAnnounceResult(success=False)

    # -- firmware update (OTA push) ------------------------------------------

    async def ota_push(self, image_path) -> OtaResult:
        """Push a firmware image to the box over the sat link.

        Contract (sat9+ firmware): CTRL ota_begin {size, sha256} ->
        the box answers ota_ready (or ota_error); the image follows
        as OTA_DATA frames of OTA_CHUNK_BYTES (last chunk partial);
        the box may report ota_progress along the way (logged by
        _handle_ctrl); CTRL ota_end -> the box verifies byte count
        + SHA-256 and answers ota_ok -- it then reboots into the
        new image, so the link drops and the supervisor reconnects
        to a fresh hello -- or ota_error, staying on the old image.
        The firmware abandons a partial transfer when the link
        drops, so any failure here leaves the box on its old image;
        every failure raises OtaError saying what happened.

        Refusals (all OtaError, before anything is sent): box not
        connected; a transfer already running; playback active on
        this client; the injected busy_check reporting the box busy
        (alarm ringing / voice turn in flight); image unreadable or
        empty. Frame writes go through _send_frame like all other
        traffic, so image chunks are serialized by the client's one
        _tx_lock and can never interleave mid-frame with audio or
        CTRL; _play_url additionally refuses to START a stream
        while a transfer owns the wire.
        """
        if self._ota_active:
            raise OtaError("a firmware update is already in progress")
        if not self.is_connected:
            raise OtaError("the speaker box is not connected")
        if self._playing:
            raise OtaError("playback is active; refusing to update mid-speech")
        if self._busy_check is not None:
            reason = self._busy_check()
            if reason:
                raise OtaError(reason)
        path = Path(image_path).expanduser()
        try:
            data = await asyncio.to_thread(path.read_bytes)
        except OSError as exc:
            raise OtaError(f"cannot read firmware image {path}: {exc}") from exc
        if not data:
            raise OtaError(f"firmware image {path} is empty")
        digest = hashlib.sha256(data).hexdigest()
        chunks = (len(data) + OTA_CHUNK_BYTES - 1) // OTA_CHUNK_BYTES
        self._ota_active = True
        # Drop any stale terminal replies from an earlier transfer.
        while not self._ota_replies.empty():
            self._ota_replies.get_nowait()
        try:
            # Nothing of ours should be sounding while the box takes
            # an image that ends in a reboot.
            self._stop_playback_local()
            _LOGGER.info(
                "satlink ota: begin: %s (%d bytes, %d chunks, sha256 %s)",
                path.name, len(data), chunks, digest,
            )
            await self._send_ctrl(
                {"cmd": "ota_begin", "size": len(data), "sha256": digest}
            )
            await self._wait_ota_reply("ota_ready", OTA_READY_TIMEOUT_S)
            for off in range(0, len(data), OTA_CHUNK_BYTES):
                if not self.is_connected:
                    raise OtaError("the box disconnected mid-transfer")
                await self._send_frame(
                    TYPE_OTA_DATA, data[off:off + OTA_CHUNK_BYTES]
                )
                # _send_frame drains per frame, so socket
                # backpressure is the pacing; this yield just keeps
                # the event loop fair for the reader + keepalive.
                await asyncio.sleep(0)
            await self._send_ctrl({"cmd": "ota_end"})
            await self._wait_ota_reply("ota_ok", OTA_END_TIMEOUT_S)
        except OtaError:
            self._ota_abort_quietly()
            raise
        except (NotConnectedError, ConnectionError) as exc:
            raise OtaError("the box disconnected during the update") from exc
        except asyncio.CancelledError:
            self._ota_abort_quietly()
            raise
        except Exception as exc:
            self._ota_abort_quietly()
            raise OtaError(f"firmware update failed: {exc}") from exc
        finally:
            self._ota_active = False
        _LOGGER.info(
            "satlink ota: image accepted (%d bytes in %d chunks); the "
            "box is rebooting into it -- expect a reconnect + hello",
            len(data), chunks,
        )
        return OtaResult(ok=True, size=len(data), sha256=digest, chunks=chunks)

    async def _wait_ota_reply(self, expected: str, timeout: float) -> dict:
        """The next terminal OTA reply, which must be `expected`.

        An ota_error (or any other terminal reply) becomes an
        OtaError carrying the box's reason; a link drop fails fast
        instead of burning the whole timeout on a dead connection."""
        deadline = time.monotonic() + timeout
        while True:
            if not self.is_connected:
                raise OtaError("the box disconnected during the update")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OtaError(
                    f"timed out waiting for the box's {expected} reply"
                )
            try:
                msg = await asyncio.wait_for(
                    self._ota_replies.get(), timeout=min(0.5, remaining)
                )
            except TimeoutError:
                continue
            kind = msg.get("cmd")
            if kind == expected:
                return msg
            if kind == "ota_error":
                raise OtaError(
                    "the box reported an OTA error while the core "
                    f"waited for {expected}: "
                    f"{msg.get('reason') or 'no reason given'}"
                )
            raise OtaError(
                f"unexpected {kind!r} reply while waiting for {expected}"
            )

    def _ota_abort_quietly(self) -> None:
        """Best-effort ota_abort after a failed push, so the box can
        drop the partial image at once. (The firmware also abandons
        the transfer on its own if the link drops, so a lost abort
        costs nothing.)"""
        if self.is_connected:
            self._schedule(self._send_ota_abort())

    async def _send_ota_abort(self) -> None:
        try:
            await self._send_ctrl({"cmd": "ota_abort"})
        except Exception:
            pass  # best effort by definition (see _ota_abort_quietly)

    # -- playback engine -----------------------------------------------------------

    def _schedule(self, coro) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _LOGGER.warning("satlink: no running loop; dropping %r", coro)
            coro.close()
            return
        loop.create_task(coro)

    def _stop_playback_local(self) -> None:
        """Generation bump + queue drain: the writer drops stale-gen
        frames, so this alone silences queued playback even before
        the box processes a barge_in."""
        if self._playing or not self._play_queue.empty() or (
            self._producer_task is not None
            and not self._producer_task.done()
        ):
            self._last_playback_end = time.monotonic()
        self._play_gen += 1
        while True:
            try:
                self._play_queue.get_nowait()
                self._play_queue.task_done()
            except asyncio.QueueEmpty:
                break
        if self._producer_task is not None:
            self._producer_task.cancel()
            self._producer_task = None
        if self._play_done is not None and not self._play_done.done():
            self._play_done.cancel()
        self._playing = False

    async def _play_url(self, url: str) -> asyncio.Future:
        """Resolve a core audio URL to a PCM source and stream it.

        Sources, in order: the audio store (session replies and
        /announce WAVs, via the /audio/<id>.wav URL shape), the
        alarm tone file (the tone URL is answered by decoding the
        local opus with ffmpeg), and a generic HTTP fetch fallback."""
        if self._ota_active:
            # A firmware transfer owns the wire: audio that would
            # stream now is dropped, never interleaved with image
            # chunks (and the box is about to reboot anyway).
            _LOGGER.warning("satlink: playback of %s dropped during OTA", url)
            dropped: asyncio.Future = asyncio.get_running_loop().create_future()
            dropped.set_result(False)
            return dropped
        self._stop_playback_local()
        if self.is_connected:
            # Flush whatever the box still holds from a prior stream.
            try:
                await self._send_ctrl({"cmd": "barge_in"})
            except NotConnectedError:
                pass
        gen = self._play_gen
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        self._play_done = done
        self._producer_task = asyncio.create_task(
            self._produce(url, gen, done), name="lyra-satlink-producer"
        )
        return done

    async def _play_pcm(self, pcm: bytes) -> asyncio.Future:
        """Stream raw 24 kHz PCM (the wake-ack earcon) under the same
        generation/barge-in discipline as _play_url."""
        if self._ota_active:
            _LOGGER.warning("satlink: wake-ack earcon dropped during OTA")
            dropped: asyncio.Future = asyncio.get_running_loop().create_future()
            dropped.set_result(False)
            return dropped
        self._stop_playback_local()
        if self.is_connected:
            try:
                await self._send_ctrl({"cmd": "barge_in"})
            except NotConnectedError:
                pass
        gen = self._play_gen
        done: asyncio.Future = asyncio.get_running_loop().create_future()
        self._play_done = done
        self._producer_task = asyncio.create_task(
            self._produce_pcm_source(gen, pcm, done),
            name="lyra-satlink-pcm-producer",
        )
        return done

    async def _produce_pcm_source(
        self, gen: int, pcm: bytes, done: asyncio.Future
    ) -> None:
        try:
            await self._produce_pcm(gen, pcm)
            await self._play_queue.join()
            if not done.done():
                done.set_result(True)
        except asyncio.CancelledError:
            if not done.done():
                done.cancel()
            raise
        except _StreamReplaced:
            if not done.done():
                done.cancel()
        except Exception as exc:
            _LOGGER.error("satlink PCM playback failed: %s", exc)
            if not done.done():
                done.set_result(False)

    async def _produce(self, url: str, gen: int, done: asyncio.Future) -> None:
        try:
            path = urlparse(url).path
            if path == self._full_config.alarm.tone_url_path:
                tone = Path(self._full_config.alarm.tone_path).expanduser()
                await self._produce_ffmpeg(gen, ["-i", str(tone)])
            elif "/audio/" in path and path.endswith(".wav"):
                audio_id = path.rsplit("/", 1)[-1][:-4]
                wav = self._store.get(audio_id) if self._store is not None else None
                if wav is None:
                    raise RuntimeError(f"audio store miss for {audio_id!r}")
                await self._produce_pcm(gen, wav_to_pcm24(wav))
            else:
                raw = await asyncio.to_thread(self._fetch_bytes, url)
                if raw[:4] == b"RIFF":
                    await self._produce_pcm(gen, wav_to_pcm24(raw))
                else:
                    await self._produce_ffmpeg(gen, ["-i", "pipe:0"], stdin_bytes=raw)
            # All frames queued: done when the writer has sent them.
            await self._play_queue.join()
            if not done.done():
                done.set_result(True)
        except asyncio.CancelledError:
            if not done.done():
                done.cancel()
            raise
        except _StreamReplaced:
            if not done.done():
                done.cancel()
        except Exception as exc:
            _LOGGER.error("satlink playback of %s failed: %s", url, exc)
            if not done.done():
                done.set_result(False)

    async def _produce_pcm(self, gen: int, pcm: bytes) -> None:
        for off in range(0, len(pcm), PLAY_FRAME_BYTES):
            if gen != self._play_gen:
                raise _StreamReplaced
            frame = pcm[off:off + PLAY_FRAME_BYTES]
            if len(frame) < PLAY_FRAME_BYTES:
                frame = frame + b"\x00" * (PLAY_FRAME_BYTES - len(frame))
            await self._play_queue.put(frame)

    async def _produce_ffmpeg(
        self, gen: int, args: list[str], *, stdin_bytes: bytes | None = None
    ) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-v", "error", *args,
            "-f", "s16le", "-ac", "1", "-ar", str(PLAY_RATE), "-",
            stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
            stdout=asyncio.subprocess.PIPE,
        )
        try:
            if stdin_bytes is not None and proc.stdin is not None:
                proc.stdin.write(stdin_bytes)
                proc.stdin.close()
            assert proc.stdout is not None
            while True:
                if gen != self._play_gen:
                    proc.kill()
                    raise _StreamReplaced
                try:
                    frame = await proc.stdout.readexactly(PLAY_FRAME_BYTES)
                except asyncio.IncompleteReadError as exc:
                    if exc.partial:
                        padded = exc.partial + b"\x00" * (
                            PLAY_FRAME_BYTES - len(exc.partial)
                        )
                        await self._play_queue.put(padded)
                    break
                await self._play_queue.put(frame)
        finally:
            # Kill first: a cancelled producer must not leave ffmpeg
            # decoding into a pipe nobody reads.
            if proc.returncode is None:
                proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                pass

    @staticmethod
    def _fetch_bytes(url: str) -> bytes:
        with urllib.request.urlopen(url, timeout=15) as resp:
            return resp.read()

    async def _playback_loop(self) -> None:
        """Drain the playback queue onto the wire, one 20 ms frame
        per 20 ms slot. Runs for the life of the connection."""
        next_slot: float | None = None
        try:
            while True:
                frame = await self._play_queue.get()
                try:
                    if not self.is_connected:
                        next_slot = None
                        self._playing = False
                        continue
                    now = time.monotonic()
                    if next_slot is None or next_slot < now - FRAME_PERIOD_S:
                        next_slot = now
                    await self._send_frame(TYPE_AUDIO_DOWN, frame)
                    self._playing = True
                    next_slot += FRAME_PERIOD_S
                    delay = next_slot - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                    if self._play_queue.empty():
                        self._playing = False
                        self._last_playback_end = time.monotonic()
                        next_slot = None
                finally:
                    self._play_queue.task_done()
        except asyncio.CancelledError:
            pass

    # -- pipeline events back to the box ---------------------------------------
    #
    # The sat protocol has no VA event channel: these are no-ops except
    # evt_tts_end, which carries the reply and therefore starts the
    # PCM stream. Signature parity with BoxClient keeps VoiceSession
    # transport-agnostic.

    def evt_run_start(self) -> None:
        pass

    def evt_run_end(self) -> None:
        pass

    def evt_wake_ack(self) -> None:
        """The wake gate just opened: play the ready chime --
        unless playback already owns the speaker (alarm ring, a
        reply still playing), an OTA transfer owns the wire, or
        the box finished playing something a moment ago (this
        gate fire is then almost certainly Lyra's own voice
        leaking back into the mic). The ack never barges."""
        if not self.is_connected or self._ota_active:
            return
        if self._playing or not self._play_queue.empty():
            return
        if self._producer_task is not None and not self._producer_task.done():
            return
        since_play = time.monotonic() - self._last_playback_end
        if since_play < WAKE_ACK_ECHO_SUPPRESS_S:
            _LOGGER.debug(
                "wake ack suppressed: %.2fs after playback end", since_play
            )
            return
        self._schedule(self._play_pcm(WAKE_ACK_PCM))

    def evt_stt_start(self) -> None:
        pass

    def evt_stt_vad_start(self) -> None:
        pass

    def evt_stt_vad_end(self) -> None:
        pass

    def evt_stt_end(self, text: str) -> None:
        pass

    def evt_intent_start(self) -> None:
        pass

    def evt_intent_end(self) -> None:
        pass

    def evt_tts_start(self, text: str) -> None:
        pass

    def evt_tts_end(self, url: str) -> None:
        """The reply: stream the WAV at url as AUDIO_DOWN PCM frames."""
        self._schedule(self._play_url(url))

    def evt_error(self, code: str, message: str = "") -> None:
        # No error channel on this wire (and stt-no-text-recognized
        # needs no firmware courtesy here): the turn simply ends
        # with no audio, the same outward shape the old sat_link
        # produced for dropped turns.
        pass


def _self_check() -> None:
    """Import + config-load verification ONLY. Never listens."""
    cfg = CoreConfig.load()
    assert cfg.transport in ("esphome", "satlink"), cfg.transport
    frame = build_frame(TYPE_CTRL, 7, b'{"cmd":"ping"}')
    magic, ftype, seq, _t, ln = HEADER.unpack(frame[:HEADER.size])
    assert (magic, ftype, seq, ln) == (MAGIC, TYPE_CTRL, 7, 14)
    pcm = wav_to_pcm24(_tiny_wav())
    assert len(pcm) == PLAY_RATE * 2 // 10  # 0.1 s of 24 kHz mono s16
    print(
        "satlink self-check OK: transport flag "
        f"{cfg.transport!r}; satlink bind {cfg.satlink.bind_host}:"
        f"{cfg.satlink.port}; frame codec + WAV decode verified; "
        "NOT listening."
    )


def _tiny_wav() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(b"\x00\x00" * 2400)
    return buf.getvalue()


if __name__ == "__main__":
    _self_check()
