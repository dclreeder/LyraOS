"""Pre-window checklist (--preflight): everything the seat-swap proof
needs, verified WITHOUT connecting to the speaker box.

Checks: config loads (secrets file stat'ed only — the Noise PSK is
resolved at connect time and is never read here), the three Wyoming
services describe OK, the Whisper/Kokoro engine ports answer, and the
core's HTTP audio endpoint binds, serves /healthz, and round-trips a
stored WAV byte-identically. Ends by printing the exact window
procedure from the approved plan (Phase 1) and the Phase 1a design
notes, for the operator running the window.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import urllib.request
import wave
from pathlib import Path

from .audio_http import AudioHttpServer
from .config import CoreConfig
from .pipeline import PipelineDriver

# The shared engine services behind the Wyoming wrappers (single-instance
# rule): whisper.cpp and the Kokoro HTTP wrapper on the Fedora.
ENGINE_PORTS = (("whisper", "127.0.0.1", 8093), ("kokoro", "127.0.0.1", 8091))

WINDOW_PROCEDURE = """\
WINDOW PROCEDURE — seat-swap proof (approved plan, Phase 1; Phase 1a design SS2/SS3)
  0. Timing: evening window, >= 2 h clear of the 06:00 alarm; target <= 15 min total.
     No firmware build, no flash, at any point in the window.
  1. BEFORE anything is disabled: capture one live HA-driven session's device
     DEBUG log and diff box_client's pipeline-event data payloads against HA's
     (Phase 1a design SS3.2). Fix any drift first — while HA still owns the seat.
  2. Disable HA's Lyra Flex ESPHome entry. The box's voice assistant admits
     exactly ONE subscriber — first-come, never stolen — so the seat must be
     free before the core subscribes; a second subscribe is refused and logged
     on the device, and HA voice stays dead until its entry reloads.
  3. Start the core under its supervisor: it connects (Noise), verifies the
     device identity, and subscribes to the voice assistant. The box's
     streamed-wake hook fires on subscription and starts continuous mic
     streaming on its own — no controller-side start is needed.
  4. ONE spoken command, end to end. Verify: beam lock engages on the wake and
     releases on STT VAD end; LED phases track (listening -> thinking ->
     reply); the spoken reply plays. Nothing else is tested in this window.
  5. Rollback, IN THIS ORDER: the core unsubscribes, then disconnects FIRST
     (the only clean seat release). Never kill the core's socket or process
     to free the seat.
  6. Re-enable HA's Lyra Flex entry and verify the rollback: HA's satellite
     returns to idle and one HA-handled command works. HA re-subscribes only
     on a fresh connection cycle, so allow it a full reconnect.
"""


def _fetch(url: str) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.status, resp.headers.get_content_type(), resp.read()


async def _port_answers(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except Exception:
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return True


def _silent_wav(seconds: float = 0.1, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(rate * seconds))
    return buf.getvalue()


async def run_preflight() -> int:
    failures: list[str] = []

    def check(ok: bool, label: str, detail: str = "") -> None:
        print(f"[{'ok' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    # 1. Config loads; secrets file presence/permissions only.
    cfg = CoreConfig.load()
    check(True, "config loads",
          f"box {cfg.box.host}:{cfg.box.port} ({cfg.box.expected_name!r}), "
          f"wake {cfg.wyoming.wake_uri}, stt {cfg.wyoming.stt_uri}, "
          f"tts {cfg.wyoming.tts_uri}, http :{cfg.http.port}")
    secrets = Path(cfg.box.secrets_file).expanduser()
    if secrets.exists():
        mode = oct(os.stat(secrets).st_mode & 0o777)
        check(mode == "0o600", "box secrets file present (stat only, PSK not read)",
              f"{secrets} mode {mode}")
    else:
        check(False, "box secrets file present (stat only, PSK not read)",
              f"missing: {secrets}")

    driver = PipelineDriver(cfg)

    # 2. Wyoming services describe OK.
    try:
        infos = await driver.describe_all()
        names = {
            svc: [p.get("name", "?") for p in infos[svc].get(key, [])]
            for svc, key in (("wake", "wake"), ("stt", "asr"), ("tts", "tts"))
        }
        check(all(names.values()), "Wyoming services describe OK", str(names))
    except Exception as exc:
        check(False, "Wyoming services describe OK", str(exc))

    # 3. Engine ports answer (whisper.cpp / Kokoro behind the wrappers).
    for name, host, port in ENGINE_PORTS:
        ok = await _port_answers(host, port)
        check(ok, f"{name} engine port answers", f"{host}:{port}")

    # 4. HTTP audio endpoint: bind, healthz, store + fetch round-trip.
    server: AudioHttpServer | None = None
    try:
        server = AudioHttpServer(cfg.http.bind, cfg.http.port)
        server.start()
        status, _ctype, body = await asyncio.to_thread(
            _fetch, f"http://127.0.0.1:{server.port}/healthz"
        )
        check(status == 200 and body == b"ok\n", "HTTP endpoint serves /healthz",
              f"status {status}")
        wav_bytes = _silent_wav()
        audio_id = server.store.put(wav_bytes)
        url = server.url_for(audio_id, cfg.http.public_host)
        status, ctype, fetched = await asyncio.to_thread(_fetch, url)
        same = hashlib.sha256(fetched).hexdigest() == hashlib.sha256(wav_bytes).hexdigest()
        check(status == 200 and ctype == "audio/wav" and same,
              "HTTP endpoint round-trips stored audio", f"{url} -> {status}, sha256 match={same}")
    except Exception as exc:
        check(False, "HTTP endpoint serves", str(exc))
    finally:
        if server is not None:
            server.stop()

    print()
    print(WINDOW_PROCEDURE)
    if failures:
        print(f"PREFLIGHT: FAIL ({len(failures)} check(s) failed: {', '.join(failures)})")
        return 1
    print("PREFLIGHT: PASS — all checks green; the box was NOT contacted.")
    return 0
