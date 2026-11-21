"""Mode flip check (suite: mode_flip).

Drives a mode change through the real dashboard endpoint
(POST :8098/api/mode -> the core's authenticated POST /mode) and
asserts the change lands in the pushed status state (the
first SSE snapshot on lyra-web's GET /status -- the same merged
state the TV page renders from), then flips back to the mode it
found and asserts
that too. No audio, no service restarts. Run via
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
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


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
    target = "sleep" if start != "sleep" else "wakegate"
    print(f"[mode] start={start} -> {target} -> {start}", flush=True)
    ok = True
    try:
        resp = set_mode(target)
        print(f"[mode] POST {target}: {resp}", flush=True)
        if not resp.get("ok"):
            print("[mode] FAIL: dashboard refused the flip")
            ok = False
        elif not wait_mode(target):
            print(f"[mode] FAIL: pushed state never became {target}")
            ok = False
        else:
            print(f"[mode] pushed state reached {target}")
    finally:
        try:
            resp = set_mode(start)
            print(f"[mode] POST back to {start}: {resp}", flush=True)
            if not resp.get("ok"):
                ok = False
        except Exception as exc:
            print(f"[mode] FAIL: restore POST failed: {exc}")
            ok = False
    if not wait_mode(start):
        print(f"[mode] FAIL: pushed state never returned to {start}")
        ok = False
    else:
        print(f"[mode] pushed state back at {start}")
    print("MODE FLIP BENCH:", "PASS" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
