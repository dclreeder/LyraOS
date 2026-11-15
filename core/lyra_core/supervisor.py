"""Connection supervisor: HA-parity reconnect/resubscribe loop around
the box client (Phase 1c).

Behaviour contract (mirrors how HA's ESPHome manager treats this box):
- Connect attempts retry forever with capped exponential backoff
  (1 s doubling to 30 s); every successful connect re-runs the full
  BoxClient.connect(), which re-subscribes to the voice assistant —
  the box's streamed-wake hook restarts continuous streaming on each
  subscription, exactly as it does for HA.
- A dropped connection is detected via aioesphomeapi's own
  APIClient.is_connected (polled), the session is detached/reset, and
  the dead client is still closed through BoxClient.disconnect()
  before being discarded.
- The ONLY intentional exit path is stop() -> BoxClient.disconnect():
  unsubscribe first, then disconnect — the clean seat release that lets
  HA (during rollback) or a future core instance subscribe on its next
  connection cycle. The supervisor never force-closes a socket.

Note: aioesphomeapi also ships ReconnectLogic (the class HA uses
internally); adopting it would mean splitting BoxClient.connect()
apart so ReconnectLogic can own the raw client lifecycle. This loop
keeps the 1b BoxClient intact and reproduces the same observable
behaviour; ReconnectLogic adoption is a production-hardening option
for Phase 5, not a bench requirement.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .box_client import BoxClient
from .config import CoreConfig
from .session import VoiceSession

_LOGGER = logging.getLogger("lyra_core.supervisor")

BoxFactory = Callable[[], BoxClient]


class CoreSupervisor:
    def __init__(
        self,
        config: CoreConfig,
        session: VoiceSession,
        box_factory: BoxFactory | None = None,
        *,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 30.0,
        poll_s: float = 0.5,
    ) -> None:
        self._cfg = config
        self._session = session
        self._factory = box_factory or session.make_box_client
        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._poll_s = poll_s
        self._stop = asyncio.Event()
        self.connect_attempts = 0
        self.successful_connects = 0

    def stop(self) -> None:
        """Request the clean exit: the run loop will unsubscribe and
        disconnect the current client (if any) before returning."""
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            box = self._factory()
            self.connect_attempts += 1
            try:
                await box.connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(
                    self._backoff_max,
                    self._backoff_initial * (2 ** (failures - 1)),
                )
                _LOGGER.warning(
                    "box connect failed (%s); retry in %.1fs", exc, delay
                )
                await self._sleep(delay)
                continue
            failures = 0
            self.successful_connects += 1
            self._session.attach_box(box)
            _LOGGER.info("box connected + VA subscribed; supervising")
            # Wait for the stop request or the connection dropping.
            while not self._stop.is_set() and box.is_connected:
                await asyncio.sleep(self._poll_s)
            lost = not self._stop.is_set()
            self._session.detach_box()
            # The only exit path — used for the intentional stop AND for
            # closing out a dead client before it is discarded.
            try:
                await box.disconnect()
            except Exception as exc:
                _LOGGER.warning("disconnect after %s raised: %s",
                                "loss" if lost else "stop", exc)
            if lost:
                failures += 1
                delay = min(
                    self._backoff_max,
                    self._backoff_initial * (2 ** (failures - 1)),
                )
                _LOGGER.warning(
                    "box connection lost; resubscribe in %.1fs", delay
                )
                await self._sleep(delay)
        _LOGGER.info("supervisor stopped (clean seat release)")
