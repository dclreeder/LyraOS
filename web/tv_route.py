#!/usr/bin/env python3
"""Route Lyra's voice by display state (Daniel's rule, 2026-11-15):
lyra-kiosk running (Lyra on screen) -> TV speaks (box everyday volume 0);
kiosk not running -> speaker box speaks (box everyday volume 20%).
No TV-power tracking: the service state is the signal. Acts only on
debounced changes. (Replaced the ADB TV-power poll the same day.)"""
import json, os, subprocess, time, urllib.request

SECRETS = "/home/dclreeder/Documents/LyraOS/core/alarm_secrets.json"
STATE = "/home/dclreeder/Documents/LyraOS/web/tv_route_state.json"
POLL_S = 5
NEED = 2          # consecutive readings to accept a change
ENV = {**os.environ, "XDG_RUNTIME_DIR": "/run/user/1000"}

def display_on():
    try:
        out = subprocess.run(
            ["systemctl", "--user", "is-active", "lyra-kiosk.service"],
            capture_output=True, text=True, timeout=4, env=ENV).stdout.strip()
        return out == "active"
    except Exception:
        return False

def set_box_volume(percent):
    tok = json.load(open(SECRETS))["kill_token"]
    body = {"tool": "media_volume", "action": "set", "args": {"percent": percent}}
    req = urllib.request.Request("http://127.0.0.1:8099/tool?t=" + tok,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return bool(json.loads(r.read()).get("ok"))

def main():
    current = None
    pending, count = None, 0
    while True:
        target = "tv" if display_on() else "box"
        if target != current:
            if pending == target:
                count += 1
            else:
                pending, count = target, 1
            if count >= NEED:
                try:
                    ok = set_box_volume(0 if target == "tv" else 20)
                except Exception:
                    ok = False
                if ok:
                    current = target
                    json.dump({"route": current, "signal": "kiosk",
                               "since": time.time()}, open(STATE, "w"))
                    print(f"route -> {current}", flush=True)
                pending, count = None, 0
        else:
            pending, count = None, 0
        time.sleep(POLL_S)

if __name__ == "__main__":
    main()
