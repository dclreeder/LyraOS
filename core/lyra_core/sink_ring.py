"""Default-sink alarm sounder (2026-11-19): the core plays the
alarm tone itself through Fedora's default sink.

Daniel took the TV out of the alarm's output path entirely --
no ADB wake, no TV volume save/set/restore, no browser page
looping the tone with acks, no speaker-box fallback. The Alarm
state machine owns the ring (schedule, kill, snooze, hard stop,
morning note); this class is just the noise-maker it drives:

- start(): spawn paplay on the tone file, aimed at the
  DEFAULT sink (no target is ever named: the OS is the
  router, per the standing rule). The tone plays at the
  sink's own volume -- no per-stream volume override, and
  this class never reads or writes any device or sink
  volume.
- The player is a child of the core process: a core restart
  ends any ring with it, and there is no device state to
  restore afterwards.
- A monitor task restarts the player if it exits mid-ring
  (tone EOF at the 15-minute mark, a sink hiccup), so the ring
  sounds until the Alarm ends it.

Everything is best-effort and logged; start() reports failure
(returns False) when the player cannot be launched or dies
immediately, so the Alarm can declare an honest miss instead
of entering a ring state that cannot sound. (paplay, not
pw-cat: on this WirePlumber setup pw-cat's --volume does not
reach the stream, while paplay's sets the sink-input volume
exactly -- verified 2026-11-19.)
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

_LOGGER = logging.getLogger("lyra_core.sink_ring")

# How long start() watches a fresh player before trusting it:
# an instant exit means the file or the sink path is broken.
_START_PROBE_S = 0.4
# Pause between player restarts inside a ring.
_RESPAWN_DELAY_S = 1.0


class SinkRinger:
    def __init__(self, tone_path: str) -> None:
        self._tone_path = Path(tone_path).expanduser()
        self._proc: asyncio.subprocess.Process | None = None
        self._monitor: asyncio.Task | None = None
        self._active = False

    async def start(self) -> bool:
        """Begin sounding on the default sink. False means the
        player could not start -- the caller must not enter a
        ring state on a ring nobody can hear."""
        if self._active:
            return True
        if shutil.which("paplay") is None:
            _LOGGER.error("sink ring: paplay not found; cannot sound")
            return False
        if not self._tone_path.is_file():
            _LOGGER.error(
                "sink ring: tone file missing at %s; cannot sound",
                self._tone_path,
            )
            return False
        self._active = True
        if not await self._spawn():
            self._active = False
            return False
        self._monitor = asyncio.create_task(
            self._watch(), name="lyra-alarm-sink-monitor"
        )
        _LOGGER.info("sink ring: sounding on the default sink")
        return True

    async def stop(self) -> None:
        self._active = False
        task, self._monitor = self._monitor, None
        if task is not None:
            task.cancel()
        await self._kill_player()
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _LOGGER.exception("sink ring: monitor ended oddly")
            # The monitor may have respawned in the same instant
            # the ring ended; no player outlives the ring.
            await self._kill_player()
        _LOGGER.info("sink ring: stopped")

    async def _kill_player(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        proc.terminate()
        for _ in range(30):
            if proc.returncode is not None:
                return
            await asyncio.sleep(0.1)
        proc.kill()

    async def _spawn(self) -> bool:
        try:
            self._proc = await asyncio.create_subprocess_exec(
                "paplay",
                str(self._tone_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except Exception as exc:
            _LOGGER.error("sink ring: player launch failed: %s", exc)
            self._proc = None
            return False
        await asyncio.sleep(_START_PROBE_S)
        if self._proc is not None and self._proc.returncode is None:
            return True
        _LOGGER.error(
            "sink ring: player exited immediately (rc=%s)",
            self._proc.returncode if self._proc is not None else None,
        )
        self._proc = None
        return False

    async def _watch(self) -> None:
        """Keep the player alive for the life of the ring."""
        while self._active:
            proc = self._proc
            if proc is None:
                if not await self._spawn():
                    await asyncio.sleep(2.0)
                continue
            await proc.wait()
            if not self._active:
                return
            _LOGGER.warning(
                "sink ring: player exited (rc=%s) mid-ring; "
                "restarting it",
                proc.returncode,
            )
            self._proc = None
            await asyncio.sleep(_RESPAWN_DELAY_S)
