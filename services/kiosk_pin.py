#!/usr/bin/python3
"""Pin Lyra's kiosk window to the monitor (DP-2) at launch.

Run once by lyra-kiosk.service's ExecStartPost. NOT a background
sorter: it runs at kiosk start, puts her window right, verifies
from the window's own reported output + fullscreen state, and
exits. Nothing stays resident.

Why it exists (2026-11-21): KWin in this build does not honor
output priorities or kwinrulesrc screen forcing for new windows,
so a freshly launched kiosk lands on the TV (the priority-1
output) instead of her monitor. A one-shot verified move at
launch is the mechanism that has proven reliable here -- the
same pattern the core's Jellyfin tool uses.

ASYNC BY DESIGN: the driver forks on startup and the parent
exits immediately, so ExecStartPost completes in milliseconds.
The first (synchronous) version hung in the start-post context
-- no reports ever arrived there -- and systemd's start-post
timeout killed the whole kiosk service three times in a loop
before it was pulled. A pin must never be able to take the
kiosk down: it also exits 0 on every outcome, and the unit
carries the '-' prefix besides. Pass --sync to run in the
foreground (manual tests).

Behavior:
- Window already on DP-2 fullscreen: report and exit.
- Window elsewhere, DP-2 present: move it, set fullscreen,
  then poll the settled state with a read-only script,
  re-asserting the move twice in case the fresh Firefox applies
  its own placement after the first set.
- DP-2 absent: leave the window where it is (the TV is her
  fallback screen) and exit.

The D-Bus report listener is this same file re-invoked with
--serve, on a per-process bus name; it runs on the system
python (PyGObject), which is why the unit calls
/usr/bin/python3. One KWin script is loaded at a time -- with
two loaded, Scripting.start() produced no reports at all
(2026-11-21). The window scan is try/catch per window: a
window caught mid-teardown must not silence the report.
"""

import os
import subprocess
import sys
import time
from pathlib import Path

TARGET_OUTPUT = "DP-2"
WINDOW_CLASS = "lyra-kiosk"
DEADLINE_S = 40

os.environ.setdefault("XDG_RUNTIME_DIR", "/run/user/1000")
os.environ.setdefault(
    "DBUS_SESSION_BUS_ADDRESS", "unix:path=/run/user/1000/bus")

LISTENER_SRC = r'''
import sys
from pathlib import Path
import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib

report_path = Path(sys.argv[1])
ready_path = Path(sys.argv[2])
bus_name = sys.argv[3]
XML = """<node><interface name="__BUSNAME__">
<method name="Report"><arg type="s" direction="in"/></method>
</interface></node>""".replace("__BUSNAME__", bus_name)

def on_method(conn, sender, path, iface, method, params, invocation):
    if method == "Report":
        report_path.write_text(params.unpack()[0])
        invocation.return_value(None)

def on_acquired(conn, name):
    conn.register_object(
        "/Pin", Gio.DBusNodeInfo.new_for_xml(XML).interfaces[0],
        on_method, None, None)
    ready_path.write_text("ready")

Gio.bus_own_name(
    Gio.BusType.SESSION, bus_name,
    Gio.BusNameOwnerFlags.NONE, None, on_acquired, None)
loop = GLib.MainLoop()
# Backstop only; the driver terminates this process itself. It
# must outlive the whole pin operation (~a minute).
GLib.timeout_add_seconds(120, loop.quit)
loop.run()
'''

MOVER_JS = """
var report = 'no-window';
try {
  var wins = workspace.windowList();
  for (var i = 0; i < wins.length; i++) {
    var w = wins[i];
    try {
      if ((w.resourceClass || '').toLowerCase() !== 'lyra-kiosk') continue;
      var target = null;
      var screens = workspace.screens;
      for (var j = 0; j < screens.length; j++) {
        if (screens[j].name === 'DP-2') { target = screens[j]; break; }
      }
      if (!target) {
        report = 'no-monitor-output';
      } else {
        if (!w.output || w.output.name !== 'DP-2') {
          workspace.sendClientToScreen(w, target);
        }
        if (!w.fullScreen) {
          try { w.fullScreen = true; } catch (e) {}
        }
        report = 'at=' + (w.output ? w.output.name : '?')
          + ' fs=' + (w.fullScreen ? 'true' : 'false');
      }
      break;
    } catch (e) { report = 'error'; }
  }
} catch (e) { report = 'error'; }
callDBus('__BUSNAME__', '/Pin', '__BUSNAME__', 'Report', report);
"""

STATE_JS = """
var report = 'no-window';
try {
  var wins = workspace.windowList();
  for (var i = 0; i < wins.length; i++) {
    var w = wins[i];
    try {
      if ((w.resourceClass || '').toLowerCase() !== 'lyra-kiosk') continue;
      report = 'at=' + (w.output ? w.output.name : '?')
        + ' fs=' + (w.fullScreen ? 'true' : 'false');
      break;
    } catch (e) { report = 'error'; }
  }
} catch (e) { report = 'error'; }
callDBus('__BUSNAME__', '/Pin', '__BUSNAME__', 'Report', report);
"""

BUS = ["busctl", "--user", "call", "org.kde.KWin", "/Scripting",
       "org.kde.kwin.Scripting"]


def log(msg: str) -> None:
    print(f"kiosk-pin: {msg}", flush=True)


def parse(text):
    if text is None or not text.startswith("at="):
        return None
    fields = dict(part.split("=", 1) for part in text.split()
                  if "=" in part)
    return fields.get("at"), fields.get("fs") == "true"


def run_pin() -> int:
    pid = os.getpid()
    bus_name = f"org.lyra.KioskPin.P{pid}"
    report = Path(f"/tmp/lyra_kiosk_pin_report_{pid}.txt")
    ready = Path(f"/tmp/lyra_kiosk_pin_ready_{pid}.txt")
    mover_js = Path(f"/tmp/lyra_kiosk_pin_mover_{pid}.js")
    state_js = Path(f"/tmp/lyra_kiosk_pin_state_{pid}.js")
    mover_name = f"kioskpin{pid}"
    state_name = f"kioskstate{pid}"
    for stale in (report, ready):
        try:
            stale.unlink()
        except OSError:
            pass
    mover_js.write_text(MOVER_JS.replace("__BUSNAME__", bus_name))
    state_js.write_text(STATE_JS.replace("__BUSNAME__", bus_name))
    listener = subprocess.Popen(
        ["/usr/bin/python3", str(Path(__file__).resolve()),
         "--serve", str(report), str(ready), bus_name],
        stderr=subprocess.DEVNULL)
    deadline = time.time() + DEADLINE_S

    def run_scripts() -> None:
        subprocess.run(BUS + ["start"], capture_output=True, timeout=8)

    def load(name: str, path: Path) -> None:
        subprocess.run(BUS + ["loadScript", "ss", str(path), name],
                       capture_output=True, timeout=8)

    def unload(name: str) -> None:
        subprocess.run(BUS + ["unloadScript", "s", name],
                       capture_output=True, timeout=8)

    def read_report(wait_s: float):
        end = time.time() + wait_s
        while time.time() < end:
            if report.exists():
                text = report.read_text().strip()
                try:
                    report.unlink()
                except OSError:
                    pass
                return text
            time.sleep(0.3)
        return None

    try:
        log(f"started; target {TARGET_OUTPUT}")
        wait_end = time.time() + 5
        while not ready.exists() and time.time() < wait_end:
            time.sleep(0.2)
        if not ready.exists():
            log("report listener did not start; leaving window as-is")
            return 0

        # Let the fresh window land before the first attempt.
        time.sleep(2)

        # Phase 1: the mover puts the window right, retrying while
        # the fresh Firefox has no window yet.
        load(mover_name, mover_js)
        seen = None
        while time.time() < deadline:
            run_scripts()
            text = read_report(4)
            if text == "no-monitor-output":
                log(f"{TARGET_OUTPUT} not present; "
                    "leaving the kiosk on its fallback screen")
                return 0
            seen = parse(text)
            if seen is not None:
                log(f"window first seen at {seen[0]}, fs={seen[1]}")
                break
            time.sleep(1.2)
        if seen == (TARGET_OUTPUT, True):
            log(f"window on {TARGET_OUTPUT}, fullscreen -- pinned")
            return 0
        if seen is None:
            log("kiosk window never reported; leaving it as-is")
            return 0

        # Phase 2: poll the settled state read-only; re-assert
        # with the mover twice in case the fresh window applied
        # its own placement after the first set.
        log("verifying the settled state")
        unload(mover_name)
        load(state_name, state_js)
        best = seen
        for poll in range(8):
            if time.time() >= deadline:
                break
            if poll in (2, 5):
                unload(state_name)
                load(mover_name, mover_js)
                run_scripts()
                text = read_report(4)
                if text == "no-monitor-output":
                    log(f"{TARGET_OUTPUT} went away; "
                        "leaving the kiosk on its fallback screen")
                    return 0
                seen = parse(text)
                if seen is not None:
                    best = seen
                    if seen == (TARGET_OUTPUT, True):
                        log(f"window on {TARGET_OUTPUT}, "
                            "fullscreen -- pinned")
                        return 0
                unload(mover_name)
                load(state_name, state_js)
            time.sleep(1.5)
            run_scripts()
            seen = parse(read_report(4))
            if seen is not None:
                best = seen
                if seen == (TARGET_OUTPUT, True):
                    log(f"window on {TARGET_OUTPUT}, "
                        "fullscreen -- pinned")
                    return 0
        log(f"could not confirm the pin (last seen {best}); "
            "leaving the window as it lies")
        return 0
    finally:
        unload(mover_name)
        unload(state_name)
        try:
            listener.terminate()
        except Exception:
            pass
        for path in (mover_js, state_js, report, ready):
            try:
                path.unlink()
            except OSError:
                pass


def main() -> int:
    if "--sync" not in sys.argv and os.environ.get("KIOSK_PIN_DETACHED") != "1":
        # Detach: the foreground process exits at once so the
        # service's start-post completes immediately; the child
        # does the pin in the background, logging to the same
        # journal through the inherited stdout.
        if os.fork() > 0:
            return 0
        os.environ["KIOSK_PIN_DETACHED"] = "1"
        os.setsid()
    try:
        return run_pin()
    except Exception as exc:  # a pin problem never fails anything
        log(f"failed open ({exc}); leaving the window as-is")
        return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        # Listener mode: argv = --serve REPORT READY BUSNAME
        ns = {"__name__": "kiosk_pin_listener"}
        sys.argv = ["kiosk_pin_listener", sys.argv[2], sys.argv[3],
                    sys.argv[4]]
        exec(compile(LISTENER_SRC, "kiosk_pin_listener", "exec"), ns)
    else:
        sys.exit(main())
