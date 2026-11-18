"""Stack checklist (--preflight): the localmic-era core's
prerequisites, verified without starting the service.

Checks: config loads, the three Wyoming services describe OK, the
Parakeet/Kokoro engine ports answer, and the core's HTTP audio
endpoint binds, serves /healthz, and round-trips a stored WAV
byte-identically. (The speaker-box seat-swap procedure this file
was written for was retired with the box, 2026-11-16.)
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import urllib.request
import wave

from .audio_http import AudioHttpServer
from .config import CoreConfig
from .pipeline import PipelineDriver

# The shared engine services behind the Wyoming wrappers (single-instance
# rule): Parakeet and the Kokoro HTTP wrapper on the Fedora, plus the
# Nemotron streaming sidecar RTC uses. (A dead sidecar only degrades
# RTC to its batch fallback, but the checklist should still show it.)
ENGINE_PORTS = (("parakeet", "127.0.0.1", 8096), ("kokoro", "127.0.0.1", 8091),
                ("nemotron-stream", "127.0.0.1", 8097))

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

    # 1. Config loads.
    cfg = CoreConfig.load()
    check(True, "config loads",
          f"transport {cfg.transport!r}, "
          f"wake {cfg.wyoming.wake_uri}, stt {cfg.wyoming.stt_uri}, "
          f"tts {cfg.wyoming.tts_uri}, http :{cfg.http.port}")

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

    # 3. Engine ports answer (Parakeet / Kokoro behind the wrappers).
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
    if failures:
        print(f"PREFLIGHT: FAIL ({len(failures)} check(s) failed: {', '.join(failures)})")
        return 1
    print("PREFLIGHT: PASS — all checks green.")
    return 0
