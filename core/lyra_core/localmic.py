"""Local-mic transport: the core's box seat, filled by Fedora itself.

Daniel's direction (2026-11-16): act like the speaker box is gone.
This client fills the client seat VoiceSession and the alarm were
built against (the surface the old box clients exposed) -- but
there is no satellite hardware anywhere in the path:

- Ears: a PipeWire capture (parecord) of Fedora's DEFAULT audio
  source, requested with no device pin, so WirePlumber routes it
  to whatever the default source is and follows the default when
  it changes (a USB headset today; the ReSpeaker board, if it ever
  returns as a plain USB capture device, tomorrow -- it would
  just be another Fedora source). Audio reaches the session in
  the pipeline's contract format -- 16 kHz / 16-bit / mono -- in
  20 ms chunks, the shape the box's frames arrive in.
- Voice: nothing plays into a microphone. Her replies already
  leave through synthesize_speech's TV mirror, which follows
  Fedora's default sink, so every evt_* method is a no-op here,
  exactly as on the satlink wire.
- Media surface (alarm fallback): there is no
  box media player. media_play / announce play the given URL
  through Fedora's default sink with pw-cat -- that is what the
  alarm's box-fallback ring becomes on this transport.
  media_volume is None and media_set_volume a no-op: output
  volume lives in Fedora's sound settings, not in a device.

Lifecycle mirrors the old satlink client: connect() starts the
capture and returns once it is flowing; is_connected is the
supervisor's drop detector -- if the capture process dies, the
supervisor detaches and reconnects with its usual backoff.
"""
from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import urllib.request
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aioesphomeapi.model import MediaPlayerState

from .audio_http import AudioStore
from .config import CoreConfig

_LOGGER = logging.getLogger("lyra_core.localmic")


class NotConnectedError(RuntimeError):
    """The localmic client is not connected (capture not running)."""

AudioCallback = Callable[[bytes], Awaitable[None]]
StartCallback = Callable[[str, "str | None"], Awaitable[None]]
StopCallback = Callable[[bool], Awaitable[None]]

RATE, WIDTH, CHANNELS = 16000, 2, 1
CHUNK_BYTES = RATE * WIDTH * CHANNELS * 20 // 1000  # 20 ms per chunk

# No --device on purpose: parecord then records from Fedora's
# default source, and WirePlumber moves the stream when the
# default changes -- the OS is the router (Daniel's rule,
# 2026-11-16). --raw with NO file argument writes headerless
# s16le PCM to stdout (a "-" argument would be a literal file).
# --latency-msec is load-bearing (2026-11-18): without it the
# capture stream negotiates the PulseAudio-default buffering --
# node.latency 32000/16000 = 2 s in the PipeWire graph, measured
# -- so everything the session hears (wake detection, VAD,
# capture) trailed the room by up to ~2 s. Her replies hid it
# (the whole conversation shifts together); the kiosk status
# light exposed it, showing "listening" only after Daniel had
# already spoken. 50 ms keeps the feed effectively live; the
# pump re-slices to 20 ms chunks regardless of fragment size.
CAPTURE_CMD = (
    "parecord", "--raw", "--format=s16le", "--rate=16000",
    "--channels=1", "--latency-msec=50",
)
PLAY_CMD = ("pw-cat", "--playback")


@dataclass
class LocalAnnounceResult:
    success: bool


class LocalMicClient:
    """The session's client, whose 'device' is Fedora's default mic."""

    def __init__(
        self,
        config: CoreConfig,
        *,
        on_audio: AudioCallback,
        on_start: StartCallback | None = None,
        on_stop: StopCallback | None = None,
        audio_store: AudioStore | None = None,
        capture_cmd: tuple[str, ...] | None = None,
    ) -> None:
        self._full_config = config
        self._on_audio = on_audio
        self._on_start = on_start
        self._on_stop = on_stop
        self._store = audio_store
        # capture_cmd is a test seam (the bench feeds a paced file);
        # production always uses CAPTURE_CMD.
        self._capture_cmd = tuple(capture_cmd) if capture_cmd else CAPTURE_CMD
        self.device_name: str | None = "localmic (Fedora default source)"
        self.entity_count: int | None = None  # no entities here
        self._proc: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task | None = None
        self._media_proc: asyncio.subprocess.Process | None = None
        self._tasks: set = set()

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Start the capture; return once the process is alive."""
        if self._pump_task is not None:
            raise RuntimeError("already connected")
        self._pump_task = asyncio.create_task(
            self._pump(), name="lyra-localmic-pump")
        # A beat for a fast failure (no PipeWire, bad command) to
        # surface here, so the supervisor's backoff sees a failed
        # connect instead of an instant silent death.
        await asyncio.sleep(0.5)
        if not self.is_connected:
            raise NotConnectedError("localmic capture failed to start")
        _LOGGER.info("localmic connected (Fedora default source)")

    async def disconnect(self) -> None:
        if self._pump_task is not None:
            self._pump_task.cancel()
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
            self._pump_task = None
        self.media_stop()
        _LOGGER.info("localmic disconnected (capture stopped)")

    @property
    def connected(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    @property
    def is_connected(self) -> bool:
        """Live-capture truth (the supervisor's drop detector)."""
        return self.connected

    async def _pump(self) -> None:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *self._capture_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            _LOGGER.info("localmic capture up: %s",
                         " ".join(self._capture_cmd))
            buf = bytearray()
            stdout = self._proc.stdout
            assert stdout is not None
            while True:
                data = await stdout.read(4096)
                if not data:
                    break
                buf.extend(data)
                while len(buf) >= CHUNK_BYTES:
                    chunk = bytes(buf[:CHUNK_BYTES])
                    del buf[:CHUNK_BYTES]
                    await self._on_audio(chunk)
            _LOGGER.warning("localmic capture stream ended")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("localmic capture failed")
        finally:
            proc, self._proc = self._proc, None
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()

    def _schedule(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- media player surface (alarm fallback) ----------------------------------

    @property
    def media_volume(self) -> float | None:
        """No device volume on this transport: the OS owns it."""
        return None

    @property
    def media_state(self) -> MediaPlayerState:
        playing = (self._media_proc is not None
                   and self._media_proc.returncode is None)
        return MediaPlayerState.PLAYING if playing else MediaPlayerState.IDLE

    def media_set_volume(self, volume: float) -> None:
        # Nothing to set: output volume lives in Fedora's sound
        # settings on this transport, not in a device.
        pass

    def media_play(self, media_url: str) -> None:
        """Play the audio at media_url via Fedora's default sink.

        Sync like the old box client's media_play: connection problems
        raise immediately. This is the alarm's fallback ring on this
        transport -- through the OS default output, per the rule
        that the OS is the router."""
        if not self.is_connected:
            raise NotConnectedError("localmic is not connected")
        self.media_stop()
        self._schedule(self._play_url(media_url))

    def media_stop(self) -> None:
        proc = self._media_proc
        if proc is not None and proc.returncode is None:
            proc.kill()

    async def _play_url(self, url: str) -> bool:
        """Download url and play it via the default sink (pw-cat)."""
        path: str | None = None
        try:
            def _fetch() -> bytes:
                with urllib.request.urlopen(url, timeout=10) as resp:
                    return resp.read()
            data = await asyncio.to_thread(_fetch)
            if url.endswith(".wav"):
                suffix = ".wav"
            elif url.endswith(".opus"):
                suffix = ".opus"
            else:
                suffix = ".audio"
            with tempfile.NamedTemporaryFile(
                    suffix=suffix, delete=False) as fh:
                fh.write(data)
                path = fh.name
            self._media_proc = await asyncio.create_subprocess_exec(
                *PLAY_CMD, path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await self._media_proc.wait() == 0
        except Exception:
            _LOGGER.exception("localmic playback failed for %s", url)
            return False
        finally:
            self._media_proc = None
            if path is not None:
                try:
                    os.unlink(path)
                except OSError:
                    pass

    # -- announcements -----------------------------------------------------------

    async def announce(self, media_url: str, text: str = "",
                       timeout: float = 30.0):
        """Play the audio at media_url via the default sink and
        await its completion (the client contract the alarm's announce path expects)."""
        if not self.is_connected:
            raise NotConnectedError("localmic is not connected")
        try:
            ok = await asyncio.wait_for(
                self._play_url(media_url), timeout=timeout)
            return LocalAnnounceResult(success=bool(ok))
        except TimeoutError:
            _LOGGER.warning(
                "localmic announce timed out after %.0fs", timeout)
            self.media_stop()
            return LocalAnnounceResult(success=False)

    # -- VA event choreography -----------------------------------------------------
    # All no-ops: there is no device to signal. Her voice already
    # leaves through synthesize_speech's TV mirror (which follows
    # Fedora's default sink); the reply is never played into a
    # microphone. Same outward shape as the old satlink wire.

    def evt_run_start(self) -> None:
        pass

    def evt_run_end(self) -> None:
        pass

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
        pass

    def evt_error(self, code: str, message: str = "") -> None:
        pass
