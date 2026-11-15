"""ESPHome-API client for the Lyra Flex speaker box, on aioesphomeapi.

PHASE 1b STATUS: code-complete, verified by import + config load ONLY.
Nothing in this module connects on import or on construction; connect()
is called explicitly, and the first real connection happens in the
Phase 1 seat-swap window with HA's entry disabled (the box's voice
assistant admits exactly one subscriber — Phase 1a design §2).

API surface verified against aioesphomeapi 46.12.0 (the version HA uses
with this box's ESPHome 2026.10.2 firmware):
  APIClient(address, port, password=None, *, noise_psk=..., expected_name=...)
  await client.connect(login=True); await client.device_info()
  await client.list_entities_services()
  client.subscribe_voice_assistant(handle_start=..., handle_stop=...,
      handle_audio=...) -> unsubscribe callable (sync method)
  client.send_voice_assistant_event(event_type, data)  (sync, fire-and-forget)
  await client.send_voice_assistant_announcement_await_response(...)

Event choreography mirrors HA's pipeline driver: the firmware's streamed-
wake hook (voice_assistant.start_continuous) runs on subscription, so a
completed subscribe means the box is already streaming mic audio here.
STT VAD start/end events are emitted from the core's own endpointer in
the seat-swap wiring — they drive the box's LED phases and the XVF beam
unlock (Phase 1a design §1/§3). Error codes below are the ones the
firmware special-cases; a no-match turn must end as ERROR
"stt-no-text-recognized" (firmware ignores it: no error LED) rather than
a generic failure.

TODO(phase1-proof): the exact `data` payloads HA attaches to each event
type get diffed against a live HA-driven session's device DEBUG log in
the proof window (Phase 1a design §3.2); payloads here follow HA's known
keys (url for TTS_START, code/message for ERROR) and are marked where
they are best-effort.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aioesphomeapi import APIClient
from aioesphomeapi.model import (
    MediaPlayerCommand,
    MediaPlayerEntityState,
    MediaPlayerFormatPurpose,
    MediaPlayerInfo,
    MediaPlayerState,
    VoiceAssistantAudioSettings,
    VoiceAssistantEventType,
)

from .config import CoreConfig

_LOGGER = logging.getLogger("lyra_core.box")

# Error codes the Lyra Flex firmware special-cases (Phase 1a design §1):
# the first two are ignored by the firmware (no error phase); the third
# plays a local guidance file.
ERROR_STT_NO_TEXT = "stt-no-text-recognized"
ERROR_DUPLICATE_WAKE = "duplicate_wake_up_detected"
ERROR_CLOUD_AUTH = "cloud-auth-failed"

AudioCallback = Callable[[bytes], Awaitable[None]]
StartCallback = Callable[[str, str | None], Awaitable[None]]
StopCallback = Callable[[bool], Awaitable[None]]


class NotConnectedError(RuntimeError):
    pass


class BoxClient:
    """Owns the (single, seat-holding) API connection to the box."""

    def __init__(
        self,
        config: CoreConfig,
        *,
        on_audio: AudioCallback,
        on_start: StartCallback | None = None,
        on_stop: StopCallback | None = None,
    ) -> None:
        self._cfg = config.box
        self._full_config = config
        self._on_audio = on_audio
        self._on_start = on_start
        self._on_stop = on_stop
        self._client: APIClient | None = None
        self._unsubscribe_va: Callable[[], None] | None = None
        self.device_name: str | None = None
        self.entity_count: int | None = None
        # Media player (Phase 3, for the alarm): selected at connect
        # from the entity list; state tracked via subscribe_states for
        # the whole life of this connection (no unsubscribe handle —
        # the subscription dies with the connection, like HA's usage).
        self._media_key: int | None = None
        self.media_object_id: str | None = None
        self._media_volume: float | None = None
        self._media_state: MediaPlayerState | None = None

    # -- lifecycle -----------------------------------------------------------

    async def connect(self) -> None:
        """Connect (Noise), verify identity, subscribe to the voice
        assistant with API audio. Resolves the Noise PSK lazily — this is
        the only method that touches the ESPHome secrets file."""
        if self._client is not None:
            raise RuntimeError("already connected")
        client = APIClient(
            self._cfg.host,
            self._cfg.port,
            None,
            client_info="lyra-core",
            noise_psk=self._full_config.box_noise_psk(),
            expected_name=self._cfg.expected_name,
        )
        try:
            await client.connect(login=True)
            info = await client.device_info()
            entities, _services = await client.list_entities_services()
            self._select_media_player(entities)
            client.subscribe_states(self._on_entity_state)
            self._unsubscribe_va = client.subscribe_voice_assistant(
                handle_start=self._handle_start,
                handle_stop=self._handle_stop,
                handle_audio=self._handle_audio,
            )
        except BaseException:
            # Any failure (or cancellation) after the client exists must
            # not leak it: a partially initialized client left
            # undisconnected can hold the box's single voice-assistant
            # seat across the supervisor's retries. Best-effort cleanup,
            # then re-raise so the supervisor's backoff still sees the
            # original failure.
            if self._unsubscribe_va is not None:
                self._unsubscribe_va()
                self._unsubscribe_va = None
            try:
                await client.disconnect()
            except Exception:
                _LOGGER.warning(
                    "cleanup disconnect after failed connect raised",
                    exc_info=True,
                )
            raise
        self._client = client
        self.device_name = info.name
        self.entity_count = len(entities)
        _LOGGER.info(
            "connected to %s (%s), %d entities, VA subscribed",
            info.name, info.mac_address, len(entities),
        )

    async def disconnect(self) -> None:
        """Unsubscribe first, then disconnect — the seat is released
        before the connection drops, so a waiting controller (HA during
        rollback) can subscribe on its next connection cycle."""
        if self._unsubscribe_va is not None:
            self._unsubscribe_va()
            self._unsubscribe_va = None
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    @property
    def is_connected(self) -> bool:
        """Live-connection truth from aioesphomeapi (the supervisor's
        drop detector). `connected` only says a client object exists."""
        return self._client is not None and self._client.is_connected

    # -- aioesphomeapi callbacks ----------------------------------------------

    # -- media player (Phase 3: alarm playback + volume) ---------------------

    def _select_media_player(self, entities) -> None:
        players = [e for e in entities if isinstance(e, MediaPlayerInfo)]
        for e in players:
            fmts = ", ".join(
                f"{f.format}@{f.sample_rate}/{f.num_channels}ch"
                f"({f.purpose.name if f.purpose else '?'})"
                for f in e.supported_formats
            )
            _LOGGER.info(
                "media player candidate: %s key=%s formats=[%s]",
                e.object_id, e.key, fmts,
            )
        chosen = None
        for e in players:
            if any(
                f.purpose in (MediaPlayerFormatPurpose.DEFAULT, None)
                for f in e.supported_formats
            ):
                chosen = e
                break
        if chosen is None and players:
            chosen = players[0]
        if chosen is not None:
            self._media_key = chosen.key
            self.media_object_id = chosen.object_id
            _LOGGER.info(
                "media player selected: %s key=%s",
                chosen.object_id, chosen.key,
            )
        else:
            _LOGGER.warning("no media player entity on the box")

    def _on_entity_state(self, state) -> None:
        if (
            isinstance(state, MediaPlayerEntityState)
            and self._media_key is not None
            and state.key == self._media_key
        ):
            self._media_volume = state.volume
            self._media_state = state.state

    @property
    def media_volume(self) -> float | None:
        """Last media volume reported by the box (0..1), or None if
        no state has arrived on this connection yet."""
        return self._media_volume

    @property
    def media_state(self) -> MediaPlayerState | None:
        return self._media_state

    def _media_command(self, **kwargs) -> None:
        if self._client is None or not self._client.is_connected:
            raise NotConnectedError("box client is not connected")
        if self._media_key is None:
            raise NotConnectedError("box has no media player entity")
        self._client.media_player_command(self._media_key, **kwargs)

    def media_play(self, media_url: str) -> None:
        """Plain media playback of a URL (HA play_media parity)."""
        self._media_command(
            command=MediaPlayerCommand.PLAY,
            media_url=media_url,
            announcement=False,
        )

    def media_stop(self) -> None:
        self._media_command(command=MediaPlayerCommand.STOP)

    def media_set_volume(self, volume: float) -> None:
        """Volume-only command (HA's async_set_volume does the same)."""
        self._media_command(volume=max(0.0, min(1.0, volume)))

    async def _handle_start(
        self,
        conversation_id: str,
        flags: int,
        audio_settings: VoiceAssistantAudioSettings,
        wake_word_phrase: str | None,
    ) -> int | None:
        # API-audio mode: audio arrives via _handle_audio. The response
        # port selects the transport on the device: 0 = API audio (see
        # ESPHome api_connection.cpp on_voice_assistant_response).
        # Returning None here makes aioesphomeapi answer error=True,
        # the firmware calls failed_to_start(), and its start_continuous
        # hook retries forever with no audio streaming (found live at
        # the 2026-11-11 cutover).
        _LOGGER.debug(
            "VA start conversation=%s flags=%s wake=%r",
            conversation_id, flags, wake_word_phrase,
        )
        if self._on_start is not None:
            await self._on_start(conversation_id, wake_word_phrase)
        return 0

    async def _handle_stop(self, abort: bool) -> None:
        _LOGGER.debug("VA stop abort=%s", abort)
        if self._on_stop is not None:
            await self._on_stop(abort)

    async def _handle_audio(self, data: bytes, end_of_stream: bytes | None) -> None:
        await self._on_audio(data)

    # -- pipeline events back to the box ---------------------------------------

    def _send(self, event_type: VoiceAssistantEventType, data: dict | None = None) -> None:
        if self._client is None:
            raise NotConnectedError("box client is not connected")
        self._client.send_voice_assistant_event(event_type, data)

    def evt_run_start(self) -> None:
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START)

    def evt_run_end(self) -> None:
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_END)

    def evt_wake_ack(self) -> None:
        """Wake-gate earcon: this transport has no local ack tone
        (the satlink client plays one natively); the no-op keeps
        the session's call transport-agnostic."""
        pass

    def evt_stt_start(self) -> None:
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_STT_START)

    def evt_stt_vad_start(self) -> None:
        """Speech detected in the stream (LED phase + beam stays locked)."""
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_START)

    def evt_stt_vad_end(self) -> None:
        """Utterance ended (firmware releases the XVF beam lock here)."""
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_STT_VAD_END)

    def evt_stt_end(self, text: str) -> None:
        # TODO(phase1-proof): confirm HA's data keys against the device log.
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_STT_END, {"text": text})

    def evt_intent_start(self) -> None:
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_START)

    def evt_intent_end(self) -> None:
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_INTENT_END)

    def evt_tts_start(self, text: str) -> None:
        """TTS_START announces the spoken text (the firmware logs
        "No text in TTS_START event" and returns without it); the audio
        itself is delivered by evt_tts_end's URL."""
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START, {"text": text})

    def evt_tts_end(self, url: str) -> None:
        """TTS_END carries the HTTP URL (audio_http.py) the box fetches
        and plays as an announcement; without it the firmware logs
        "No url in TTS_END event" and returns."""
        self._send(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_END, {"url": url})

    def evt_error(self, code: str, message: str = "") -> None:
        """End a run with an error. For no-match turns use
        ERROR_STT_NO_TEXT — the firmware ignores it (no error LED)."""
        self._send(
            VoiceAssistantEventType.VOICE_ASSISTANT_ERROR,
            {"code": code, "message": message},
        )

    # -- announcements (assist_satellite.announce parity) ----------------------

    async def announce(self, media_url: str, text: str = "", timeout: float = 30.0):
        """Play media on the box's announcement pipeline and await finish."""
        if self._client is None:
            raise NotConnectedError("box client is not connected")
        return await self._client.send_voice_assistant_announcement_await_response(
            media_id=media_url, timeout=timeout, text=text
        )


def _self_check() -> None:
    """Import + config-load verification ONLY. Never connects."""
    import asyncio

    cfg = CoreConfig.load()

    async def _noop_audio(data: bytes) -> None:
        pass

    client = BoxClient(cfg, on_audio=_noop_audio)
    assert not client.connected
    try:
        client.evt_run_start()
    except NotConnectedError:
        pass
    else:  # pragma: no cover
        raise AssertionError("emitter must refuse while disconnected")
    print(
        "box_client self-check OK: config loaded "
        f"(box {cfg.box.host}:{cfg.box.port}, expected name "
        f"{cfg.box.expected_name!r}); emitter guard works; NOT connecting."
    )


if __name__ == "__main__":
    _self_check()
