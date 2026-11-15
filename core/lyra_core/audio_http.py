"""HTTP audio endpoint: serves generated speech (and, in later phases, the
alarm tone) at URLs the speaker box can fetch.

This mirrors how the box consumes audio today: ESPHome pipeline TTS and
announcements reach the device as HTTP URLs (Phase 1a design §1), the same
way HA serves its TTS cache. Stdlib ThreadingHTTPServer on purpose — the
endpoint is a file server with one route shape, and stdlib keeps the core
runnable under the esphome venv with no extra dependencies.

Bind: loopback + the Fedora's LAN address on :8099 by default
(config.http.bind; dial-in 2026-11-11, item C) -- reachable from the
box and the home LAN, not from other interfaces such as Tailscale.
The public URL form is http://<fedora-lan-ip>:8099/audio/<id>.wav.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

# An extra route answers a GET for one exact path: it receives the
# parsed query string and returns (status, content_type, body). The
# alarm registers its tone file and its authenticated kill endpoint
# this way; the handler runs them in the HTTP worker thread.
RouteHandler = Callable[[dict], tuple[int, str, bytes]]

# A POST route receives the parsed query string and the raw request
# body, and returns the same triple. The Phase 4 core API registers
# /tool and /announce this way.
PostRouteHandler = Callable[[dict, bytes], tuple[int, str, bytes]]

MAX_POST_BODY_BYTES = 65536


class AudioStore:
    """In-memory id -> WAV bytes store. Speech is ephemeral; nothing is
    written to disk by the endpoint.

    Bounded (dial-in 2026-11-11, item D): entries expire after
    `ttl_s` and the store is hard-capped at `max_items` with
    oldest-first eviction, so the store can never grow without
    bound. Defaults -- TTL 600 s, cap 32 -- are sized to the real
    consumers: the box fetches a TTS reply or announcement within
    seconds of the URL being handed out, so ten minutes is a wide
    margin, and 32 live clips is far beyond any burst the single
    voice pipeline can produce. (The alarm tone is NOT kept here;
    it is served from its own route by the alarm module.)"""

    def __init__(self, ttl_s: float = 600.0, max_items: int = 32) -> None:
        self._lock = threading.Lock()
        # id -> (put time, wav bytes); dict order is insertion order,
        # so the first entry is always the oldest live one.
        self._items: dict[str, tuple[float, bytes]] = {}
        self._ttl_s = ttl_s
        self._max_items = max_items

    def _prune_locked(self, now: float) -> None:
        expired = [
            key
            for key, (put_at, _data) in self._items.items()
            if now - put_at > self._ttl_s
        ]
        for key in expired:
            del self._items[key]
        while len(self._items) > self._max_items:
            del self._items[next(iter(self._items))]

    def put(self, wav_bytes: bytes) -> str:
        audio_id = uuid.uuid4().hex[:16]
        with self._lock:
            now = time.monotonic()
            self._items[audio_id] = (now, wav_bytes)
            self._prune_locked(now)
        return audio_id

    def get(self, audio_id: str) -> bytes | None:
        with self._lock:
            item = self._items.get(audio_id)
            if item is None:
                return None
            put_at, data = item
            if time.monotonic() - put_at > self._ttl_s:
                del self._items[audio_id]
                return None
            return data


def _make_handler(
    store: AudioStore,
    routes: dict[str, RouteHandler] | None = None,
    post_routes: dict[str, PostRouteHandler] | None = None,
    post_limits: dict[str, int] | None = None,
):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            if self.path == "/healthz":
                body = b"ok\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            split = urlsplit(self.path)
            if routes and split.path in routes:
                status, content_type, body = routes[split.path](
                    parse_qs(split.query)
                )
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            prefix = "/audio/"
            if self.path.startswith(prefix) and self.path.endswith(".wav"):
                audio_id = self.path[len(prefix):-len(".wav")]
                data = store.get(audio_id)
                if data is not None:
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_POST(self) -> None:  # noqa: N802 (stdlib naming)
            split = urlsplit(self.path)
            handler = post_routes.get(split.path) if post_routes else None
            if handler is None:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            limit = (post_limits or {}).get(split.path, MAX_POST_BODY_BYTES)
            if length < 0 or length > limit:
                self.send_response(413)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = self.rfile.read(length) if length else b""
            status, content_type, resp_body = handler(
                parse_qs(split.query), body
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            self.wfile.write(resp_body)

        def log_message(self, format: str, *args) -> None:  # silence access log
            pass

    return Handler


class AudioHttpServer:
    def __init__(
        self, bind: str | list[str], port: int, store: AudioStore | None = None
    ):
        self.store = store or AudioStore()
        # Shared with the handler: routes registered after start() are
        # picked up because the handler consults the same dict per GET.
        self.routes: dict[str, RouteHandler] = {}
        self.post_routes: dict[str, PostRouteHandler] = {}
        self.post_limits: dict[str, int] = {}
        # One ThreadingHTTPServer per bind address, all sharing the
        # store and route dicts (dial-in 2026-11-11, item C). A plain
        # string keeps the historical single-bind behaviour.
        addresses = [bind] if isinstance(bind, str) else list(bind)
        if not addresses:
            raise ValueError("AudioHttpServer needs at least one bind address")
        handler = _make_handler(
            self.store, self.routes, self.post_routes, self.post_limits
        )
        self._servers = [
            ThreadingHTTPServer((address, port), handler) for address in addresses
        ]
        self._threads: list[threading.Thread] = []
        self.port = self._servers[0].server_address[1]
        self.binds = [server.server_address[0] for server in self._servers]

    def start(self) -> None:
        for i, server in enumerate(self._servers):
            thread = threading.Thread(
                target=server.serve_forever,
                name=f"lyra-audio-http-{i}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        for server in self._servers:
            server.shutdown()
            server.server_close()
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads.clear()

    def add_route(self, path: str, handler: RouteHandler) -> None:
        """Register an extra GET route (exact path match)."""
        self.routes[path] = handler

    def add_post_route(
        self,
        path: str,
        handler: PostRouteHandler,
        max_body_bytes: int | None = None,
    ) -> None:
        """Register an extra POST route (exact path match).

        max_body_bytes overrides the small global POST cap for this
        route only (the dashboard call endpoint takes whole spoken
        utterances as WAV, which are far larger than a JSON body)."""
        self.post_routes[path] = handler
        if max_body_bytes is not None:
            self.post_limits[path] = max_body_bytes

    def url_for(self, audio_id: str, public_host: str) -> str:
        return f"http://{public_host}:{self.port}/audio/{audio_id}.wav"
