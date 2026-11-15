"""Phase 2 bench: verify-then-fire, proven at mock-session level.

The training pipeline in-path: the session's verify gate transcribes
the COMBINED wake+command clip via whisper.cpp :8093 DIRECTLY (the
production collector's own whisper_transcribe — not the Wyoming STT
spool/claim path), classifies it with the production collector's
classifier in-process, and the verdict gates the turn. The gate runs
DRY-RUN: classification is real, filing is computed by the real
collector.file_clip with its write primitives swapped for recorders —
the production pool and ledger must be byte-identical before/after
(asserted here by a full clips-tree hash manifest).

Part 0 — pure functions: the wake-strip normalizer is asserted
         string-equal to the production normalizer over a battery;
         strip_wake_phrase and classify sanity on known transcripts.
Part A — the 2026-11-11 00:35 negative clip (TV audio, production
         verdict FAIL): detection -> combined transcription -> FAIL
         -> silent end. Asserts: zero Wyoming STT calls, zero TTS
         calls, zero reply URLs, empty audio store, on_command never
         called, box trace = run_start/stt_start/vad/error/run_end.
         NOTE: the marginal 0.885 TV trigger does NOT re-fire when the
         filed clip is replayed through the gate (probe-verified with
         PipelineDriver.detect_wake; the 0.987 positive re-fires
         reliably). A part therefore injects the Detection at the
         production stream position (2.5 s) if the gate has not fired
         by then, and reports detection_source; the verify path under
         test is identical either way.
Part B — the "Hey Lyra, what time is it?" pool positive (production
         verdict PASS): PASS -> would-file positive -> command handed
         onward == "what time is it?" -> full 1c box-event sequence,
         one TTS call, URL fetch-back 200.

The REAL box is never contacted; HA is untouched; the Wyoming wake
server is only *used* (it spools each replayed Detection as in
production — spool entries created by a part are removed by that part,
well inside the janitor's 120 s window). Run via ~/Documents/LyraOS/core/phase2-bench.sh.
"""
from __future__ import annotations

import array
import asyncio
import hashlib
import json
import math
import sys
import time
from pathlib import Path

from .audio_http import AudioHttpServer
from .config import CoreConfig
from .endpointer import VAD_END, VAD_START, Endpointer
from .pipeline import PipelineDriver
from .session import VoiceSession
from .session_bench import FakeBox, _fetch, _read_pcm
from .verify import VerifyGate, collector, strip_wake_phrase, _normalize_with_map

CLIPS = Path("~/Documents/LyraOS/modules/wakeword/data/clips").expanduser()
LEDGER = CLIPS / "clips_ledger.jsonl"
SPOOL_DIR = Path("~/Documents/LyraOS/modules/wyoming/collector_spool").expanduser()
NEG_CLIP = CLIPS / "negatives" / "wake_negative_20261111_003531_0.885.wav"
POS_CLIP = CLIPS / "positives" / "wake_positive_20261111_061439_0.987.wav"

CHUNK = 3200  # 100 ms at 16 kHz / 16-bit / mono
SILENCE_1S = b"\x00\x00" * 16000


class CountingDriver(PipelineDriver):
    """Counts Wyoming STT/TTS usage: in verify mode the STT count must
    stay zero (the gate transcribes via :8093 directly)."""

    def __init__(self, cfg: CoreConfig) -> None:
        super().__init__(cfg)
        self.stt_calls = 0
        self.tts_calls = 0

    async def transcribe_audio(self, *args, **kwargs):
        self.stt_calls += 1
        return await super().transcribe_audio(*args, **kwargs)

    async def synthesize_speech(self, *args, **kwargs):
        self.tts_calls += 1
        return await super().synthesize_speech(*args, **kwargs)


class BenchEndpointer(Endpointer):
    """Harness stand-in used ONLY for Part A (the TV clip). The
    production endpointer cannot endpoint that audio — measured frame
    RMS 640-847 across the whole clip vs its 969 start threshold (see
    phase2-bench.md) — so this double declares VAD_START on the first
    armed chunk and VAD_END after ~700 ms of near-silence, mirroring
    the segment the production STT path captured for this clip. The
    verify gate under test is untouched; Part B uses the real
    endpointer."""

    def __init__(self) -> None:
        super().__init__()
        self._dbl_started = False
        self._dbl_quiet_chunks = 0

    def begin_capture(self) -> None:
        super().begin_capture()
        self._dbl_started = False
        self._dbl_quiet_chunks = 0

    def feed(self, pcm: bytes) -> list[str]:
        if not self._armed:
            return super().feed(pcm)
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
        if not samples:
            return []
        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        if not self._dbl_started:
            self._dbl_started = True
            self._dbl_quiet_chunks = 0
            return [VAD_START]
        if rms < 60.0:
            self._dbl_quiet_chunks += 1
            if self._dbl_quiet_chunks >= 7:  # ~700 ms of 100 ms chunks
                return [VAD_END]
        else:
            self._dbl_quiet_chunks = 0
        return []


def tree_manifest(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def spool_names() -> set[str]:
    if not SPOOL_DIR.exists():
        return set()
    return {p.name for p in SPOOL_DIR.iterdir() if p.is_file()}


def cleanup_spool(before: set[str]) -> list[str]:
    removed = []
    for name in sorted(spool_names() - before):
        (SPOOL_DIR / name).unlink()
        removed.append(name)
    return removed


def production_ledger_line(clip_name: str) -> dict | None:
    for line in LEDGER.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("path", "").endswith(clip_name):
            return entry
    return None


# ---------------------------------------------------------------------------
# Part 0 — pure functions
# ---------------------------------------------------------------------------

def part_0() -> dict:
    print("== Part 0: pure-function battery ==")
    battery = [
        "Hey Lyra, what time is it?",
        "- I remember going to have a function in that one thing.",
        "", "!!!", "a  b", "  Hey   Lyra!!  What's the weather?  ",
        "Héllo Lyra", "OK Lyra, play some music", "heylyra",
        "Turn OFF the Light, please...", "123 hey lyra 456",
        "Hey Laura, turn it up", "\tHey\nLyra\r\nstop",
    ]
    for text in battery:
        mine, _ = _normalize_with_map(text)
        prod = collector._normalize_wake_text(text)
        assert mine == prod, (text, mine, prod)
    print(f"  normalize-with-map == production normalizer on {len(battery)} inputs")
    strip_cases = {
        "Hey Lyra, what time is it?": "what time is it?",
        "hey lyra turn on the light": "turn on the light",
        "Hey Larry, set a timer for five minutes": "set a timer for five minutes",
        "Hey Lyra": "",
        "No wake phrase here at all.": "",
        "heylyra what time is it": "what time is it",
    }
    for text, expected in strip_cases.items():
        got = strip_wake_phrase(text)
        assert got == expected, (text, got, expected)
    print(f"  strip_wake_phrase correct on {len(strip_cases)} cases")
    assert collector.classify_wake_transcription(
        "- I remember going to have a function in that one thing.") == "fail"
    assert collector.classify_wake_transcription(
        "Hey Lyra, what time is it?") == "pass"
    assert collector.classify_wake_transcription(
        "OK Lyra, play some music") == "maybe"
    print("  classify sanity: TV line=fail, positive=pass, bare 'Lyra'=maybe")
    print("Part 0: PASS")
    return {"battery": len(battery), "strip_cases": len(strip_cases)}


# ---------------------------------------------------------------------------
# Parts A/B — one mock-session run each
# ---------------------------------------------------------------------------

async def run_part(cfg: CoreConfig, clip: Path, label: str, *,
                   inject_detection_at_s: float | None = None,
                   endpointer=None) -> dict:
    print(f"== {label}: {clip.name} ==")
    timeline: list = []
    t0 = time.monotonic()

    def hook(kind: str, detail: dict) -> None:
        timeline.append((time.monotonic(), kind, detail))

    spool_before = spool_names()
    server = AudioHttpServer(cfg.http.bind, cfg.http.port)
    server.start()
    driver = CountingDriver(cfg)
    gate = VerifyGate(dry_run=True)
    commands: list[str] = []

    def on_command(command: str) -> None:
        commands.append(command)
        return None  # canned reply stands (Phase 3 owns real replies)

    session = VoiceSession(
        cfg, driver, server,
        on_event=hook, verify_gate=gate, on_command=on_command,
        endpointer=endpointer,
    )
    box = FakeBox(session.handle_audio, timeline)
    session.attach_box(box)  # type: ignore[arg-type]
    await session.start()
    result: dict = {"clip": clip.name, "label": label}
    try:
        await asyncio.sleep(0.5)  # wake link up before audio flows
        pcm, rate, width, channels = _read_pcm(clip)
        print(f"[clip] {clip.name}: {len(pcm) / (rate * width * channels):.2f}s")
        # Feed in 0.5 s slices; inject the Detection at the production
        # stream position if the gate has not fired by then.
        slice_bytes = CHUNK * 5
        fed = 0
        injected = False
        while fed < len(pcm):
            await box.feed(pcm[fed:fed + slice_bytes])
            fed += slice_bytes
            fed_s = fed / (rate * width * channels)
            if (inject_detection_at_s is not None and not injected
                    and fed_s >= inject_detection_at_s
                    and session.state == "idle"):
                session._on_detection({})
                injected = True
                print(f"[bench] detection injected at {fed_s:.1f}s "
                      "(gate did not re-fire on replay)")
        result["detection_source"] = "injected" if injected else "live"
        result["endpointer"] = (
            type(endpointer).__name__ if endpointer is not None
            else "Endpointer (production)"
        )
        # Silence tail so the endpointer hangover can expire; stop the
        # moment processing begins (no post-run audio is forwarded).
        fed_silence = 0.0
        while session.state != "processing" and fed_silence < 4.0:
            await box.feed(SILENCE_1S[:CHUNK * 5])  # 0.5 s slices
            fed_silence += 0.5
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            names = [d.get("name") for _t, k, d in timeline if k == "box_event"]
            if "run_end" in names and session.state == "idle":
                break
            await asyncio.sleep(0.2)
        else:
            kinds_seen = sorted({k for _t, k, _d in timeline})
            raise AssertionError(
                f"run never completed: state={session.state} kinds={kinds_seen}")
        if session.last_tts_url:
            f_status, f_ctype, f_body = await asyncio.to_thread(
                _fetch, session.last_tts_url
            )
            result["tts_fetch"] = {
                "status": f_status, "content_type": f_ctype, "bytes": len(f_body),
            }
            print(f"[http] GET {session.last_tts_url} -> {f_status}, "
                  f"{f_ctype}, {len(f_body)} B")
    finally:
        await session.stop()
        removed = cleanup_spool(spool_before)
        server.stop()
    result["spool_cleaned"] = removed
    print(f"[spool] removed bench entries: {removed}")
    print("-- event timeline (t+ seconds) --")
    for ts, kind, detail in timeline:
        print(f"  t+{ts - t0:6.2f}s  {kind:14s} {detail}")
    verify_events = [d for _t, k, d in timeline if k == "verify"]
    result["timeline"] = [
        {"t": round(ts - t0, 2), "kind": k, "detail": d} for ts, k, d in timeline
    ]
    result["box_seq"] = [d.get("name") for _t, k, d in timeline if k == "box_event"]
    result["kinds"] = [k for _t, k, _d in timeline]
    result["verify"] = verify_events[0] if verify_events else None
    result["last_verify"] = (
        {
            "verdict": session.last_verify.verdict,
            "transcript": session.last_verify.transcript,
            "command": session.last_verify.command,
            "gate_score": session.last_verify.gate_score,
            "dry_run": session.last_verify.dry_run,
            "filed_path": session.last_verify.filed_path,
            "would_ledger": session.last_verify.would_ledger,
            "audio_seconds": round(session.last_verify.audio_seconds, 2),
        }
        if session.last_verify is not None else None
    )
    result["commands"] = commands
    result["stt_calls"] = driver.stt_calls
    result["tts_calls"] = driver.tts_calls
    result["last_tts_url"] = session.last_tts_url
    result["store_items"] = len(server.store._items)
    result["last_transcript"] = session.last_transcript
    print(f"[verify] {result['last_verify']}")
    print(f"[driver] wyoming stt_calls={driver.stt_calls} tts_calls={driver.tts_calls}")
    return result


def assert_part_a(r: dict) -> None:
    assert "detection" in r["kinds"], "wake server never returned a Detection"
    assert r["verify"] is not None, "verify stage never ran"
    assert r["verify"]["verdict"] == "fail", r["verify"]
    assert "function" in r["verify"]["transcript"], r["verify"]["transcript"]
    assert "silent_drop" in r["kinds"], "no silent_drop event"
    assert r["stt_calls"] == 0, "Wyoming STT was used in verify mode"
    assert r["tts_calls"] == 0, "TTS was called on a FAIL verdict"
    assert r["last_tts_url"] is None, "a reply URL was emitted on a FAIL verdict"
    assert "tts_url" not in r["kinds"], "tts_url event on a FAIL verdict"
    assert r["store_items"] == 0, "audio store not empty after silent drop"
    assert r["commands"] == [], "on_command fired on a FAIL verdict"
    assert r["box_seq"] == ["run_start", "stt_start", "stt_vad_start",
                             "stt_vad_end", "error", "run_end"], r["box_seq"]
    lv = r["last_verify"]
    assert lv["dry_run"] is True
    assert "/negatives/" in lv["filed_path"], lv["filed_path"]
    assert lv["would_ledger"] and lv["would_ledger"]["verdict"] == "fail"
    assert lv["would_ledger"]["path"].startswith("negatives/")
    print("Part A assertions: PASS (FAIL verdict, fully silent, would-file negative)")


def assert_part_b(r: dict) -> None:
    assert "detection" in r["kinds"], "wake server never returned a Detection"
    assert r["verify"] is not None, "verify stage never ran"
    assert r["verify"]["verdict"] == "pass", r["verify"]
    assert r["verify"]["transcript"] == "Hey Lyra, what time is it?", r["verify"]
    assert r["commands"] == ["what time is it?"], r["commands"]
    assert r["stt_calls"] == 0, "Wyoming STT was used in verify mode"
    assert r["tts_calls"] == 1, "expected exactly one TTS call on PASS"
    assert r["box_seq"] == ["run_start", "stt_start", "stt_vad_start",
                             "stt_vad_end", "stt_end", "intent_start",
                             "intent_end", "tts_start", "tts_end",
                             "run_end"], r["box_seq"]
    lv = r["last_verify"]
    assert lv["dry_run"] is True
    assert "/positives/" in lv["filed_path"], lv["filed_path"]
    assert lv["would_ledger"] and lv["would_ledger"]["verdict"] == "pass"
    assert lv["would_ledger"]["path"].startswith("positives/")
    fetched = r.get("tts_fetch")
    assert fetched and fetched["status"] == 200 and fetched["content_type"] == "audio/wav", fetched
    print("Part B assertions: PASS (PASS verdict, command onward, would-file positive)")


# ---------------------------------------------------------------------------

async def run() -> int:
    cfg = CoreConfig.load()
    report: dict = {}
    print("[manifest] hashing clips tree (before)...")
    before = tree_manifest(CLIPS)
    report["manifest_before_files"] = len(before)
    report["ledger_sha_before"] = before.get("clips_ledger.jsonl")
    print(f"[manifest] {len(before)} files; ledger sha256={report['ledger_sha_before']}")

    report["part_0"] = part_0()
    part_a = await run_part(cfg, NEG_CLIP, "Part A (negative)",
                            inject_detection_at_s=2.5,
                            endpointer=BenchEndpointer())
    assert_part_a(part_a)
    report["part_a"] = part_a
    part_b = await run_part(cfg, POS_CLIP, "Part B (positive)",
                            inject_detection_at_s=2.5)
    assert_part_b(part_b)
    report["part_b"] = part_b

    print("[manifest] hashing clips tree (after)...")
    after = tree_manifest(CLIPS)
    report["manifest_after_files"] = len(after)
    report["ledger_sha_after"] = after.get("clips_ledger.jsonl")
    diff = {
        k for k in set(before) | set(after) if before.get(k) != after.get(k)
    }
    report["manifest_diff"] = sorted(diff)
    assert not diff, f"PRODUCTION POOL CHANGED during dry-run bench: {sorted(diff)}"
    print(f"[manifest] identical ({len(after)} files, ledger sha256={report['ledger_sha_after']})")
    report["spool_final"] = sorted(spool_names())

    for key, clip in (("part_a", NEG_CLIP.name), ("part_b", POS_CLIP.name)):
        prod = production_ledger_line(clip)
        bench = report[key]["last_verify"]
        report[f"{key}_production"] = prod
        match = bool(prod) and prod["verdict"] == bench["verdict"]
        report[f"{key}_verdict_match"] = match
        print(f"[compare] {clip}: production verdict={prod and prod['verdict']} "
              f"bench verdict={bench['verdict']} match={match}")
        print(f"[compare] production transcript={prod and prod['transcript']!r}")
        print(f"[compare] bench      transcript={bench['transcript']!r}")
    print("===PHASE2_BENCH_JSON===")
    print(json.dumps(report, indent=2, default=str))
    print("===PHASE2_BENCH_PASS===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
