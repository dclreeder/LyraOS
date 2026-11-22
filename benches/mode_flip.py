"""Mode flip check (suite: mode_flip).

Drives a mode change through the real dashboard endpoint
(POST :8098/api/mode -> the core's authenticated POST /mode) and
asserts the change lands in the pushed status state (the
first SSE snapshot on lyra-web's GET /status -- the same merged
state the TV page renders from), then flips back and asserts
that too. Also asserts the sleep lock: since the memory build
(2026-11-22) sleep belongs to the nightly cycle alone, so a
manual POST to sleep must be refused and change nothing. (The
bench originally flipped INTO sleep; that leg is the refusal
check now.) No audio, no service restarts. Run via
benches/run_all.py.
"""
from __future__ import annotations

import json
import time
import urllib.request

DASH = "http://192.168.12.144:8098"
WEB = "http://127.0.0.1:8095"


def pushed_mode() -> str:
    req = urllib.request.Request(WEB + "/status")
    with urllib.request.urlopen(req, timeout=10) as resp:
        for raw in resp:
            line = raw.decode(errors="replace").strip()
            if line.startswith("data: "):
                return json.loads(line[len("data: "):])["mode"]
    raise RuntimeError("status stream ended without a snapshot")


def set_mode(mode: str) -> dict:
    body = json.dumps({"mode": mode}).encode()
    req = urllib.request.Request(
        DASH + "/api/mode", data=body,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {"ok": False, "http": exc.code}


def wait_mode(mode: str, timeout: float = 20.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pushed_mode() == mode:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    start = pushed_mode()
    print(f"[mode] start={start} -> rtc -> sleep (must refuse) -> wakegate", flush=True)
    ok = True
    try:
        if start != "wakegate":
            set_mode("wakegate")
            if not wait_mode("wakegate"):
                print("[mode] FAIL: could not settle at wakegate first")
                ok = False
        resp = set_mode("rtc")
        print(f"[mode] POST rtc: {resp}", flush=True)
        if not resp.get("ok"):
            print("[mode] FAIL: dashboard refused the flip to rtc")
            ok = False
        elif not wait_mode("rtc"):
            print("[mode] FAIL: pushed state never became rtc")
            ok = False
        else:
            print("[mode] pushed state reached rtc")
        resp = set_mode("sleep")
        print(f"[mode] POST sleep: {resp}", flush=True)
        if resp.get("ok"):
            print("[mode] FAIL: manual sleep was accepted; the cycle owns sleep")
            ok = False
        elif pushed_mode() != "rtc":
            print("[mode] FAIL: refused sleep still changed the mode")
            ok = False
        else:
            print("[mode] manual sleep refused, mode unchanged -- as designed")
    finally:
        try:
            resp = set_mode("wakegate")
            print(f"[mode] POST back to wakegate: {resp}", flush=True)
            if not resp.get("ok"):
                ok = False
        except Exception as exc:
            print(f"[mode] FAIL: restore POST failed: {exc}")
            ok = False
    if not wait_mode("wakegate"):
        print("[mode] FAIL: pushed state never returned to wakegate")
        ok = False
    else:
        print("[mode] pushed state back at wakegate")
    print("MODE FLIP BENCH:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
