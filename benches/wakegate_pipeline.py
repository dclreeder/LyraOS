"""Phase 1b bench: prove the core's Wyoming loop on a recorded clip.

Stages: describe x2 (wake + TTS; STT is Parakeet direct since
2026-11-19) -> wake (known positive clip) -> STT (same audio) ->
TTS -> HTTP serve + fetch-back verification. Also observes the production
clip collector's side effects (spool -> filed clip + ledger line) so the
report states exactly what the bench run caused in the training pool.

Never drives a live device, never touches any service config;
the only writes are the collector's own (by design) and this output.
Run via benches/run_all.py (suite: wakegate_pipeline).
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sys
import time
import urllib.request
import wave
from pathlib import Path

# --- suite bootstrap: repo-relative import paths (core + this dir) ---
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.join(_HERE, "..", "core"), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end suite bootstrap ---
from lyra_core.audio_http import AudioHttpServer
from lyra_core.config import CoreConfig
from lyra_core.pipeline import PipelineDriver, PipelineError
from lyra_core.verify import finalize_spooled_detection

CLIP = Path(
    "~/Documents/LyraOS/modules/wakeword/data/clips/positives/"
    "wake_positive_20261111_061439_0.987.wav"
).expanduser()
POS_DIR = Path("~/Documents/LyraOS/modules/wakeword/data/clips/positives").expanduser()
LEDGER = Path("~/Documents/LyraOS/modules/wakeword/data/clips/clips_ledger.jsonl").expanduser()
SPOOL_DIR = Path("~/Documents/LyraOS/modules/wyoming/collector_spool").expanduser()
REPLY_TEXT = "Lyra core bench check complete."


def _newest(d: Path) -> tuple[str, float] | None:
    files = [p for p in d.iterdir() if p.is_file()]
    if not files:
        return None
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest.name, newest.stat().st_mtime


def _last_line(path: Path) -> str:
    try:
        lines = path.read_text().strip().splitlines()
        return lines[-1] if lines else ""
    except FileNotFoundError:
        return ""


def _read_pcm(path: Path) -> tuple[bytes, int, int, int]:
    with wave.open(str(path), "rb") as wf:
        return (
            wf.readframes(wf.getnframes()),
            wf.getframerate(),
            wf.getsampwidth(),
            wf.getnchannels(),
        )


def _fetch(url: str) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.status, resp.headers.get_content_type(), resp.read()


def _program_names(info: dict, key: str) -> list[str]:
    return [p.get("name", "?") for p in info.get(key, [])]


async def run() -> int:
    cfg = CoreConfig.load()
    # Suite: never bind the production HTTP port (the live core
    # holds it); the bench server runs on 8199 like the other
    # session benches.
    cfg = dataclasses.replace(cfg, http=dataclasses.replace(cfg.http, port=8199))
    driver = PipelineDriver(cfg)
    # The collector janitor hook, wired exactly as production
    # wires it (the STT bridge used to do this job).
    driver.on_transcribed = finalize_spooled_detection
    # Silence (house rule): the driver's TV mirror is a
    # fire-and-forget POST to lyra-web; record it in-process so no
    # bench speech ever reaches the TV page.
    mirrored: list[int] = []
    async def _record_mirror(wav_bytes: bytes) -> None:
        mirrored.append(len(wav_bytes))
    import lyra_core.pipeline as _pipeline_mod
    _pipeline_mod._mirror_wav_to_tv = _record_mirror
    report: dict = {"clip": str(CLIP), "reply_text": REPLY_TEXT}

    # Collector state before (the wake/STT pair files clips by design).
    before_clip = _newest(POS_DIR)
    before_ledger = _last_line(LEDGER)
    before_spool = sorted(p.name for p in SPOOL_DIR.iterdir()) if SPOOL_DIR.exists() else []

    # Stage 0: describe all three services.
    t0 = time.monotonic()
    infos = await driver.describe_all()
    report["describe"] = {
        "wake_programs": _program_names(infos["wake"], "wake"),
        "tts_programs": _program_names(infos["tts"], "tts"),
    }
    print(f"[describe] wake={report['describe']['wake_programs']} "
          f"tts={report['describe']['tts_programs']} "
          f"({time.monotonic() - t0:.2f}s)")

    server = AudioHttpServer(cfg.http.bind, cfg.http.port)
    server.start()
    try:
        status, _ctype, body = await asyncio.to_thread(
            _fetch, f"http://127.0.0.1:{server.port}/healthz"
        )
        print(f"[http] healthz -> {status} {body!r}")

        pcm, rate, width, channels = _read_pcm(CLIP)
        print(f"[clip] {CLIP.name}: {rate} Hz, {width * 8}-bit, {channels} ch, "
              f"{len(pcm) / (rate * width * channels):.2f}s")

        # Stage A1: wake detection on the positive clip.
        t0 = time.monotonic()
        try:
            detection = await driver.detect_wake(pcm, rate, width, channels)
        except PipelineError as exc:
            print(f"[wake] FAIL (pipeline error): {exc}")
            return 3
        report["detection"] = detection
        print(f"[wake] detection={detection} ({time.monotonic() - t0:.2f}s)")
        if not detection:
            print("[wake] FAIL: no Detection for a known positive clip")
            return 3

        # Stage A2: STT on the same audio.
        t0 = time.monotonic()
        try:
            transcript = await driver.transcribe_audio(pcm, rate, width, channels)
        except PipelineError as exc:
            print(f"[stt] FAIL (pipeline error): {exc}")
            return 4
        report["transcript"] = transcript
        print(f"[stt] transcript={transcript!r} ({time.monotonic() - t0:.2f}s)")
        # Regression contract: the transcript of the canonical pool
        # positive must equal its production ledger line EXACTLY.
        ledger_text = None
        for _line in LEDGER.read_text().splitlines():
            try:
                _entry = json.loads(_line)
            except json.JSONDecodeError:
                continue
            if _entry.get("path", "").endswith(CLIP.name):
                ledger_text = _entry.get("transcript")
        print(f"[regression] ledger transcript={ledger_text!r}")
        if transcript != ledger_text:
            print("[regression] FAIL: transcript differs from the ledger (not EXACT)")
            return 7
        print("[regression] transcript EXACT vs ledger")

        # Stage A3: TTS reply, served by the core's HTTP endpoint, fetched back.
        t0 = time.monotonic()
        try:
            wav_bytes, trate, twidth, tch = await driver.synthesize_speech(REPLY_TEXT)
        except PipelineError as exc:
            print(f"[tts] FAIL (pipeline error): {exc}")
            return 5
        audio_id = server.store.put(wav_bytes)
        url = server.url_for(audio_id, cfg.http.public_host)
        status, ctype, fetched = await asyncio.to_thread(_fetch, url)
        digest_stored = hashlib.sha256(wav_bytes).hexdigest()
        digest_fetched = hashlib.sha256(fetched).hexdigest()
        report["tts"] = {
            "rate": trate, "width": twidth, "channels": tch,
            "wav_bytes": len(wav_bytes),
        }
        report["http"] = {
            "url": url, "status": status, "content_type": ctype,
            "bytes_stored": len(wav_bytes), "bytes_fetched": len(fetched),
            "sha256_match": digest_stored == digest_fetched,
        }
        print(f"[tts] {len(wav_bytes)} B WAV @ {trate} Hz "
              f"({time.monotonic() - t0:.2f}s)")
        print(f"[http] GET {url} -> {status}, {ctype}, {len(fetched)} B, "
              f"sha256 match={digest_stored == digest_fetched}")
        if status != 200 or ctype != "audio/wav" or digest_stored != digest_fetched:
            print("[http] FAIL: fetch-back verification mismatch")
            return 6
    finally:
        server.stop()

    # Collector side effects: the driver's transcribe hook finalizes
    # the spooled detection asynchronously; give it a few seconds,
    # then observe (never interfere).
    await asyncio.sleep(8)
    after_clip = _newest(POS_DIR)
    after_ledger = _last_line(LEDGER)
    after_spool = sorted(p.name for p in SPOOL_DIR.iterdir()) if SPOOL_DIR.exists() else []
    report["collector"] = {
        "new_positive_clip": (
            after_clip[0] if after_clip and after_clip != before_clip else None
        ),
        "new_ledger_line": after_ledger if after_ledger != before_ledger else None,
        "spool_before": before_spool,
        "spool_after": after_spool,
    }
    print(f"[collector] new clip: {report['collector']['new_positive_clip']}")
    print(f"[collector] new ledger line: {report['collector']['new_ledger_line']}")
    print(f"[collector] spool before={before_spool} after={after_spool}")

    report["mirrored_wavs"] = mirrored
    print(f"[mirror] suppressed {len(mirrored)} TV mirror post(s) in-process")
    print("===BENCH_JSON===")
    print(json.dumps(report, indent=2))
    print("===BENCH_PASS===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
