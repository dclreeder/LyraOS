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
    # Wake-word detection is NOT a service at all: it runs in the
    # core process (lyra_core.wake, hey_lyra_stable_v1) since
    # 2026-11-19 -- no wake URI remains. What is left in this
    # config is TTS (Wyoming), Parakeet (HTTP) and the names.
    # Speech-to-text is NOT a Wyoming service either: the core POSTs
    # WAVs directly to Parakeet TDT 0.6B v2 on parakeet-server
    # :8096 (home: ~/Documents/parakeet.cpp). The Wyoming STT
    # bridge that used to sit between them was removed 2026-11-19.
    # (Whisper was removed from the project 2026-11-17 at Daniel's
    # order.)
    parakeet_url: str = "http://127.0.0.1:8096/v1/audio/transcriptions"
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
    # through the tools layer; see lyra_core.free_router. Backend:
    # LOCAL llama-server (lyra-llm.service) on 127.0.0.1:8094
    # serving Nemotron Nano 9B v2 Q8_0 (alias nemotron-nano-9b-v2,
    # bartowski GGUF) on the RX 9060 XT via Vulkan. Her brain
    # 2026-11-17 -> 2026-11-18, RESTORED the same evening
    # (2026-11-18) at Daniel's order after the 4B latency test
    # concluded (~29.8 tok/s decode / 840-1,120 prompt eval).
    # Reasoning off via the "/no_think" system-prompt suffix
    # (system_prompt_suffix below) -- this model's control.
    # ROLLBACK to Nemotron 3 Nano 4B Q8_0 (her brain for the
    # 2026-11-18 evening latency test: prompt eval 2,195 tok/s
    # cold / decode 63.7-64.1 tok/s fully resident -- it proved
    # the parked 30B-A3B's slowness was CPU-expert offload, not
    # its ~3B active size; Daniel's live verdict: "obviously
    # dumber by a lot" but "speedy"): its GGUF is still on disk
    # (data/models/nvidia_Nemotron-3-Nano-4B-Q8_0.gguf, sha256
    # 4e023262fd2f...). Restore lyra-llm.service ExecStart to
    # the 4B file with --alias nemotron-3-nano-4b AND
    # --chat-template-kwargs '{"enable_thinking":false}' (that
    # generation's reasoning control; "/no_think" does NOT work
    # on it), llm_model back to "nemotron-3-nano-4b",
    # system_prompt_suffix back to "", restart lyra-llm +
    # lyra-core.
    # Qwen3.5-9B rollback (her brain 2026-11-12 to 2026-11-16,
    # and briefly 2026-11-17): its GGUF is still on disk
    # (data/models/Qwen3.5-9B-Q8_0.gguf). Restore
    # lyra-llm.service ExecStart to the Qwen file with
    # --alias qwen3.5-9b and --chat-template-kwargs
    # '{"enable_thinking":false}' (drop --jinja), llm_model
    # back to "qwen3.5-9b", system_prompt_suffix back to "",
    # restart lyra-llm + lyra-core.
    # History, same day 2026-11-17: Gemma 4 12B (worked, ~31-33
    # tok/s, some ~22 s turns), then Hermes 3 Llama 3.1 8B
    # lorablated (ZERO tool calls, fabricated actions; swapped in
    # anyway at Daniel's explicit override, then replaced hours
    # later). BOTH of those GGUFs were DELETED at Daniel's order
    # -- rolling back to either means re-downloading it. The
    # no-download fallback is the cloud backend from before Gemma
    # (2026-11-16): Muse Ember 1.0, Meta Model API. ROLLBACK to
    # Ember = restore these lines to:
    #   llm_url "https://api.meta.ai/v1", llm_model "muse-ember-1.0",
    #   api_key_path "~/Documents/APIKeys/.env",
    #   api_key_var "MUSE_MODEL_API_KEY" (reasoning_effort as-is)
    # and restart lyra-core. The local server ignores the
    # reasoning_effort field (verified 2026-11-17) and needs no
    # real key; the loader still requires a non-empty one, so it
    # reads a dummy from the project-local llama_api_key.env
    # (NOT a secret -- see that file).
    llm_url: str = "http://127.0.0.1:8094/v1"
    llm_model: str = "nemotron-nano-9b-v2"
    # Appended to the loaded SOUL.md at turn time by
    # free_router (never written into SOUL.md itself):
    # model plumbing tied to the current brain. "/no_think" is
    # the 9B v2's reasoning-off control. The 4B rollback sets
    # this back to "" -- its reasoning is controlled
    # server-side instead (see above).
    system_prompt_suffix: str = "/no_think"
    reasoning_effort: str = "minimal"
    api_key_path: str = "~/Documents/LyraOS/core/llama_api_key.env"
    api_key_var: str = "LLAMA_API_KEY"
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
