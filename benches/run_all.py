#!/usr/bin/env python3
"""LyraOS bench suite -- the single runner.

Usage:
    benches/run_all.py              # every bench, in order
    benches/run_all.py NAME [...]   # just the named benches

Each bench runs as its own subprocess under the core's venv
python with PYTHONPATH=core:benches. A bench's verdict is its
printed marker plus its exit code. KNOWN-FAIL is a first-class
verdict (currently only rtc_listening's Phase B -- see
benches/README.md): the suite stays green on an expected
known-fail, red on anything else unexpected.

The runner snapshots core/alarm_state.json's sha256 before and
after the run: no bench may touch the production alarm state.
Per-bench logs land in /tmp/lyra-bench-logs/<name>.log.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "benches"
CORE_DIR = ROOT / "core"
VENV_PY = Path.home() / "Documents/LyraOS/core/venv/bin/python"
ALARM_STATE = CORE_DIR / "alarm_state.json"
LOG_DIR = Path("/tmp/lyra-bench-logs")

# (name, module, timeout_s, pass marker, known-fail marker or None)
BENCHES = [
    ("alarm_fire", "alarm_fire", 180, "alarm bench OK", None),
    ("free_bench", "free_bench", 900, "FREE BENCH: PASS", None),
    ("wakegate_pipeline", "wakegate_pipeline", 300, "===BENCH_PASS===", None),
    ("verify_gate", "verify_gate", 420, "===PHASE2_BENCH_PASS===", None),
    ("wakegate_session", "wakegate_session", 420, "===SESSION_BENCH_PASS===", None),
    ("wakegate_listening", "wakegate_listening", 600, "WAKEGATE LISTENING BENCH: PASS", None),
    ("mode_flip", "mode_flip", 180, "MODE FLIP BENCH: PASS", None),
    ("rtc_loopback", "rtc_loopback", 600, "RTC LOOPBACK BENCH: PASS", None),
    ("rtc_listening", "rtc_listening", 600, "RTC LISTENING BENCH: PASS", "RTC LISTENING BENCH: KNOWN-FAIL"),
]


def alarm_sha() -> str:
    return hashlib.sha256(ALARM_STATE.read_bytes()).hexdigest()


def run_bench(spec) -> tuple[str, float, Path]:
    name, module, timeout, marker, known_marker = spec
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"{name}.log"
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{CORE_DIR}:{BENCH_DIR}"
    env.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            [str(VENV_PY), "-m", module],
            cwd=str(ROOT), env=env, capture_output=True, text=True,
            timeout=timeout,
        )
        out = proc.stdout + proc.stderr
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode(errors="replace") + "\n[runner] TIMEOUT\n"
        rc = -1
    dur = time.monotonic() - t0
    log_path.write_text(out)
    if known_marker and known_marker in out:
        verdict = "KNOWN-FAIL"
    elif marker in out and rc == 0:
        verdict = "PASS"
    else:
        verdict = "FAIL"
    return verdict, dur, log_path


def main() -> int:
    wanted = sys.argv[1:]
    specs = [b for b in BENCHES if not wanted or b[0] in wanted]
    if not specs:
        print(f"no benches match {wanted}; known: {[b[0] for b in BENCHES]}")
        return 2
    sha_before = alarm_sha()
    print(f"alarm_state.json sha256 before: {sha_before}")
    results = []
    for spec in specs:
        print(f"\n=== {spec[0]} ===", flush=True)
        verdict, dur, log_path = run_bench(spec)
        results.append((spec[0], verdict))
        print(f"--- {spec[0]}: {verdict} ({dur:.1f}s) log={log_path}", flush=True)
        if verdict == "FAIL":
            tail = log_path.read_text(errors="replace").splitlines()[-25:]
            print("    " + "\n    ".join(tail), flush=True)
    sha_after = alarm_sha()
    print("\n===== SUITE SUMMARY =====")
    for name, verdict in results:
        print(f"  {name:22s} {verdict}")
    print(f"alarm_state.json sha256 after:  {sha_after}")
    alarm_ok = sha_before == sha_after
    print(f"alarm state untouched: {alarm_ok}")
    failed = [n for n, v in results if v == "FAIL"]
    if failed or not alarm_ok:
        print(f"SUITE: FAIL ({', '.join(failed) if failed else 'alarm state changed'})")
        return 1
    print("SUITE: PASS" if all(v == "PASS" for _n, v in results) else "SUITE: PASS (with KNOWN-FAIL as documented)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
