"""Lyra core configuration.

JSON (stdlib-only) so every core module can load it under any interpreter.
Secrets are never stored in the core config.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = CORE_DIR / "config.json"


@dataclass
class WyomingConfig:
    wake_uri: str = "tcp://127.0.0.1:10400"
    stt_uri: str = "tcp://127.0.0.1:10300"
    tts_uri: str = "tcp://127.0.0.1:10200"
    wake_name: str = "hey_lyra_stable_v1"
    language: str = "en"
    tts_voice: str = "af_heart"


@dataclass
class HttpConfig:
    # Bind loopback + this host's LAN address ONLY (dial-in
    # 2026-11-11, item C): the box fetches audio over the LAN, the
    # iOS Action Button kill URL rides home Wi-Fi, and local callers
    # use loopback -- while other interfaces
    # (e.g. the Tailscale address) no longer expose the endpoint.
    # A plain string is still accepted (single bind), e.g. in
    # benches; a list binds each address on the same port.
    bind: str | list[str] = field(
        default_factory=lambda: ["127.0.0.1", "192.168.12.144"]
    )
    port: int = 8099
    public_host: str = "192.168.12.144"


@dataclass
class AlarmConfig:
    # Fedora-native alarm (Phase 3). State + kill token live in files
    # the core owns; the token file is 0600 and its value never logged.
    state_path: str = "~/Documents/LyraOS/core/alarm_state.json"
    secrets_path: str = "~/Documents/LyraOS/core/alarm_secrets.json"
    tone_path: str = "~/Documents/LyraOS/data/assets/alarm_homepod_15min.opus"
    tone_url_path: str = "/alarm/tone.opus"
    kill_url_path: str = "/alarm/kill"
    light_host: str = "192.168.12.204"
    light_id: int = 0
    # Alarm button (2026-11-12): Shelly Plus Wall Dimmer with NO
    # load attached -- its taps kill the ring via on-device
    # webhooks to the kill endpoint, and its LED bar flashes
    # (brightness-only pulses) while a ring sounds. "" disables.
    button_host: str = "192.168.12.199"
    timezone: str = "America/New_York"
    hard_stop_s: float = 900.0
    snooze_s: float = 540.0
    catchup_window_s: float = 1800.0
    tick_s: float = 20.0


@dataclass
class RouterConfig:
    # Free router (2026-11-11): the model runs the turn uncaged
    # through the tools layer; see lyra_core.free_router. Backend
    # since 2026-11-16: Muse Spark 1.3 on the Meta Model API
    # (OpenAI-compatible chat completions). The local llama-server
    # (lyra-llm) was shut down and removed the same night at
    # Daniel's order -- there is no local fallback. The API key is
    # never stored here; config only says where free_router reads
    # it (Daniel's central APIKeys .env).
    llm_url: str = "https://api.meta.ai/v1"
    llm_model: str = "muse-spark-1.3"
    reasoning_effort: str = "low"
    api_key_path: str = "~/Documents/APIKeys/.env"
    api_key_var: str = "MUSE_MODEL_API_KEY"
    timezone: str = "America/New_York"
    # Home location exactly as the HA-era setup recorded it
    # (core.config in the 2026-11-10 HA baseline backup); Open-Meteo
    # is keyless but needs lat/lon.
    weather_latitude: float = 30.3639618
    weather_longitude: float = -81.7529823


@dataclass
class CoreConfig:
    # The core's one transport: "localmic" (2026-11-16) -- the
    # session's client is a PipeWire capture of Fedora's default
    # audio source (lyra_core.localmic). The speaker-box transports
    # (ESPHome API, satlink) were removed with the box the same day;
    # production_main refuses any other value.
    transport: str = "localmic"
    wyoming: WyomingConfig = field(default_factory=WyomingConfig)
    http: HttpConfig = field(default_factory=HttpConfig)
    alarm: AlarmConfig = field(default_factory=AlarmConfig)
    router: RouterConfig = field(default_factory=RouterConfig)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CoreConfig":
        """Load config.json if present; defaults otherwise. No secrets read."""
        cfg = cls()
        p = Path(path) if path is not None else DEFAULT_CONFIG_PATH
        if p.exists():
            raw = json.loads(p.read_text())
            if isinstance(raw.get("transport"), str):
                cfg.transport = raw["transport"]
            for section_name, section in (
                ("wyoming", cfg.wyoming),
                ("http", cfg.http),
                ("alarm", cfg.alarm),
                ("router", cfg.router),
            ):
                for key, value in raw.get(section_name, {}).items():
                    if hasattr(section, key):
                        setattr(section, key, value)
        return cfg
