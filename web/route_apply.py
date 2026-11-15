#!/usr/bin/env python3
"""Apply Lyra's display state (Daniel's design, 2026-11-15).

display_state.json is the single source of truth for the DISPLAY:
  display_on=true  -> kiosk running (her face is on the TV)
  display_on=false -> kiosk stopped

Speech does not route through here anymore: since 2026-11-15 the TV
is the only audio out and the box is ears only (the gating lives in
lyra-core's speech_route). This only enforces the kiosk side and
records the state. Runs at boot and whenever the state file changes
(lyra-route-watch.path). No polling, no volume tricks."""
import json, os, subprocess, time

HOME = "/home/dclreeder"
DISPLAY_STATE = f"{HOME}/Documents/LyraOS/web/display_state.json"
ROUTE_STATE = f"{HOME}/Documents/LyraOS/web/tv_route_state.json"
ENV = {**os.environ, "XDG_RUNTIME_DIR": "/run/user/1000"}


def display_on():
    try:
        return bool(json.load(open(DISPLAY_STATE)).get("display_on", True))
    except Exception:
        return True


def set_kiosk(on):
    subprocess.run(["systemctl", "--user", "start" if on else "stop",
                    "lyra-kiosk.service"],
                   capture_output=True, timeout=30, env=ENV)


def main():
    on = display_on()
    set_kiosk(on)
    json.dump({"route": "tv", "display_on": on, "signal": "state",
               "since": time.time()}, open(ROUTE_STATE, "w"))
    print(f"display_on={on} (route is always tv: TV is the only audio out)",
          flush=True)


if __name__ == "__main__":
    main()
