"""Connection supervisor: reconnect loop around the session's
client (Phase 1c; transport-agnostic since the localmic cutover).

Behaviour contract:
- Connect attempts retry forever with capped exponential backoff
  (1 s doubling to 30 s); every successful connect re-attaches the
  client to the session.
- A dropped connection is detected via the client's own
  is_connected (polled); the session is detached/reset, and the
  dead client is still closed through its disconnect() before
  being discarded.
- The ONLY intentional exit path is stop() -> client.disconnect():
  the clean release that lets a future core instance attach on its
  next connection cycle. The supervisor never force-closes a
  capture.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from .localmic import LocalMicClient
from .config import CoreConfig
from .session import VoiceSession

_LOGGER = logging.getLogger("lyra_core.supervisor")

ClientFactory = Callable[[], LocalMicClient]


class CoreSupervisor:
    def __init__(
        self,
        config: CoreConfig,
        session: VoiceSession,
        client_factory: ClientFactory | None = None,
        *,
        backoff_initial_s: float = 1.0,
        backoff_max_s: float = 30.0,
        poll_s: float = 0.5,
    ) -> None:
        self._cfg = config
        self._session = session
        self._factory = client_factory or session.make_client
        self._backoff_initial = backoff_initial_s
        self._backoff_max = backoff_max_s
        self._poll_s = poll_s
        self._stop = asyncio.Event()
        self.connect_attempts = 0
        self.successful_connects = 0

    def stop(self) -> None:
        """Request the clean exit: the run loop will disconnect the
        current client (if any) before returning."""
        self._stop.set()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            client = self._factory()
            self.connect_attempts += 1
            try:
                await client.connect()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(
                    self._backoff_max,
                    self._backoff_initial * (2 ** (failures - 1)),
                )
                _LOGGER.warning(
                    "client connect failed (%s); retry in %.1fs", exc, delay
                )
                await self._sleep(delay)
                continue
            failures = 0
            self.successful_connects += 1
            self._session.attach_box(client)
            _LOGGER.info("client connected; supervising")
            # Wait for the stop request or the connection dropping.
            while not self._stop.is_set() and client.is_connected:
                await asyncio.sleep(self._poll_s)
            lost = not self._stop.is_set()
            self._session.detach_box()
            # The only exit path — used for the intentional stop AND for
            # closing out a dead client before it is discarded.
            try:
                await client.disconnect()
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
                    "client connection lost; reconnect in %.1fs", delay
                )
                await self._sleep(delay)
        _LOGGER.info("supervisor stopped")
