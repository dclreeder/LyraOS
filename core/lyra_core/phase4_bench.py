"""Phase 4 bench: the core API (POST /tool, POST /announce) against a
bench HTTP instance on an alternate port, with a fake announce
recorder -- the production service (which holds :8099) is never
touched and never speaks.

Run: python -m lyra_core.phase4_bench

Composition mirrors production_main, with two substitutions:
- the announce callable records instead of speaking;
- the Alarm runs on a throwaway state/secrets pair in a temp dir
  (production alarm_state.json is never opened).

Hardware contact is READ-ONLY: light state (Shelly GetStatus)
against the real tool; the weather call is the live Open-Meteo
fetch. Nothing is toggled; no audio is played.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from .alarm import Alarm, ShellyLight
from .api import CoreApi
from .audio_http import AudioHttpServer
from .config import CoreConfig
from .tools import (
    AlarmTool,
    ClockTool,
    LightTool,
    WeatherTool,
)

PORT = 8199
TOKEN = "bench-token-p4"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "ok" if condition else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        _failures.append(name)


def _request(method: str, path: str, payload=None, *, raw=None, token=TOKEN):
    url = f"http://127.0.0.1:{PORT}{path}"
    if token is not None:
        url += f"?t={token}"
    data = raw
    if payload is not None:
        data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read()
            try:
                return resp.status, json.loads(body) if body else {}
            except ValueError:
                return resp.status, {'raw': body.decode('utf-8', 'replace')}
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body) if body else {}
        except ValueError:
            return exc.code, {"raw": body.decode("utf-8", "replace")}


async def amain() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="lyra-phase4-bench-"))
    http = AudioHttpServer("127.0.0.1", PORT)
    http.start()
    try:
        cfg = CoreConfig.load()
        cfg.alarm.state_path = str(tmp / "alarm_state.json")
        cfg.alarm.secrets_path = str(tmp / "alarm_secrets.json")
        alarm = Alarm(cfg, http=http)
        announced: list[str] = []

        async def fake_announce(text: str) -> None:
            announced.append(text)

        api = CoreApi(
            http=http,
            token=TOKEN,
            light=LightTool(ShellyLight(cfg.alarm.light_host, cfg.alarm.light_id)),
            alarm_tool=AlarmTool(alarm),
            clock=ClockTool(cfg.router.timezone),
            weather=WeatherTool(
                cfg.router.weather_latitude,
                cfg.router.weather_longitude,
                cfg.router.timezone,
            ),
            announce=fake_announce,
        )
        await api.start()

        post = lambda *a, **kw: asyncio.to_thread(_request, "POST", *a, **kw)  # noqa: E731
        get = lambda *a, **kw: asyncio.to_thread(_request, "GET", *a, **kw)  # noqa: E731

        # -- pre-existing GET surface is intact -------------------------
        status, body = await get("/healthz", token=None)
        check("GET /healthz still answers", status == 200, f"{status} {body}")
        status, _ = await post("/nope", {"tool": "time"})
        check("POST unknown path -> 404", status == 404, f"{status}")

        # -- auth ---------------------------------------------------------
        status, _ = await post("/tool", {"tool": "time"}, token=None)
        check("POST /tool without token -> 403", status == 403, f"{status}")
        status, _ = await post("/tool", {"tool": "time"}, token="wrong")
        check("POST /tool wrong token -> 403", status == 403, f"{status}")
        status, _ = await post("/announce", {"text": "A statement."}, token=None)
        check("POST /announce without token -> 403", status == 403, f"{status}")
        status, _ = await post(
            "/announce", {"text": "What is this?"}, token=None
        )
        check(
            "POST /announce bad text + no token -> 403 (auth first)",
            status == 403,
            f"{status}",
        )

        # -- /tool happy paths ---------------------------------------------
        status, body = await post("/tool", {"tool": "time"})
        check(
            "tool time -> 200 + HH:MM confirmation",
            status == 200
            and body.get("ok") is True
            and re.fullmatch(r"It's \d{2}:\d{2}\.", body.get("confirmation", ""))
            is not None,
            f"{status} {body}",
        )
        status, body = await post("/tool", {"tool": "weather"})
        check(
            "tool weather -> 200 + live report with rain chance",
            status == 200
            and body.get("ok") is True
            and "Rain chance" in body.get("confirmation", ""),
            f"{status} {body}",
        )
        print(f"       weather confirmation: {body.get('confirmation')!r}")
        status, body = await post("/tool", {"tool": "light", "action": "state"})
        check(
            "tool light state (real Shelly read) -> 200, light off",
            status == 200
            and body.get("ok") is True
            and body.get("state", {}).get("on") is False,
            f"{status} {body}",
        )
        status, body = await post("/tool", {"tool": "alarm", "action": "summary"})
        check(
            "tool alarm summary -> migrated state",
            status == 200
            and body.get("state", {}).get("time") == "06:00"
            and body.get("state", {}).get("enabled") is True
            and body.get("state", {}).get("repeat") == "Weekdays",
            f"{status} {body}",
        )
        status, _ = await post("/tool", {"tool": "media_volume", "action": "get"})
        check("tool media_volume (removed with the box) -> 400", status == 400, f"{status}")
        status, _ = await post("/tool", {"tool": "monitor", "action": "state"})
        check("tool monitor (removed) -> 400", status == 400, f"{status}")
        status, _ = await post("/tool", {"tool": "firmware", "action": "update_status"})
        check("tool firmware (removed with the box) -> 400", status == 400, f"{status}")
        status, body = await post("/tool", {"tool": "alarm", "action": "kill"})
        check(
            "tool alarm kill while not ringing -> ok=False refusal",
            status == 200
            and body.get("ok") is False
            and body.get("confirmation") == "The alarm isn't ringing.",
            f"{status} {body}",
        )

        # -- /tool validation ----------------------------------------------
        status, _ = await post("/tool", {"tool": "bogus"})
        check("tool unknown tool -> 400", status == 400, f"{status}")
        status, _ = await post("/tool", {"tool": "light", "action": "bogus"})
        check("tool unknown action -> 400", status == 400, f"{status}")
        status, _ = await post(
            "/tool", {"tool": "light", "action": "dim", "args": {"percent": 150}}
        )
        check("tool light dim percent 150 -> 400", status == 400, f"{status}")
        status, _ = await post("/tool", {"tool": "light", "action": "dim"})
        check("tool light dim without percent -> 400", status == 400, f"{status}")
        status, _ = await post("/tool", {"tool": "alarm", "action": "set"})
        check("tool alarm set without time -> 400", status == 400, f"{status}")
        status, _ = await post("/tool", raw=b"{not json")
        check("tool malformed JSON -> 400", status == 400, f"{status}")

        # -- /announce -------------------------------------------------------
        status, _ = await post("/announce", {"text": "What time is it?"})
        check("announce with '?' -> 400", status == 400, f"{status}")
        check("announce '?' never reached the box path", announced == [])
        status, _ = await post("/announce", {"text": "   "})
        check("announce empty text -> 400", status == 400, f"{status}")
        status, _ = await post("/announce", {"text": "x" * 201})
        check("announce 201 chars -> 400", status == 400, f"{status}")
        status, body = await post(
            "/announce", {"text": "The weather is clear tonight."}
        )
        check(
            "announce valid statement -> 200 + recorder saw it",
            status == 200
            and body.get("ok") is True
            and announced == ["The weather is clear tonight."],
            f"{status} {body} announced={announced}",
        )
    finally:
        http.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    if _failures:
        print(f"phase4 bench FAILED: {_failures}")
        return 1
    print("===PHASE4_BENCH_PASS===")
    return 0


def main() -> int:
    return asyncio.run(amain())


if __name__ == "__main__":
    sys.exit(main())
