#!/usr/bin/env python3
"""Lyra web viewer server: static pages, /mirror speech jobs, /ring, /dim, and the /rtc/offer signaling proxy."""
import base64, json, os, queue, sys, threading, time, urllib.request, urllib.error, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.expanduser('~/Documents/LyraOS/web')
_lock = threading.Lock(); _counter = [0]
try:
    with open(os.path.join(ROOT, 'latest.json')) as _f: _counter[0] = int(json.load(_f).get('id') or 0)
except Exception: pass
def core_token():
    with open(os.path.expanduser('~/Documents/LyraOS/core/alarm_secrets.json')) as f:
        return json.load(f)['kill_token']
# RTC signaling (2026-11-16): in rtc mode the TV page itself is the
# WebRTC peer; it POSTs its SDP offer to /rtc/offer here and this
# server relays it to the core with the token server-side. The
# core answers 409 unless rtc mode is
# active; that status passes straight through to the page.
def rtc_offer(payload):
    req = urllib.request.Request('http://127.0.0.1:8099/rtc/offer?t=' + core_token(),
                                 data=payload, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, json.loads(r.read())
# Next-alarm line for the TV clock (2026-11-15): the core's alarm
# tool summary, relayed server-side like the offer proxy. The TV page
# polls GET /alarm and renders the next fire under the clock.
def alarm_summary():
    body = json.dumps({'tool': 'alarm', 'action': 'summary', 'args': {}}).encode()
    req = urllib.request.Request('http://127.0.0.1:8099/tool?t=' + core_token(),
                                 data=body, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        obj = json.loads(r.read())
    st = obj.get('state') or {}
    return {'enabled': st.get('enabled'), 'time': st.get('time'),
            'next_fire': st.get('next_fire'), 'ringing': st.get('ringing'),
            'snooze_until': st.get('snooze_until')}
# Room state for the TV page's painted room (2026-11-15): the room
# background mirrors the real room -- ceiling light (via the core's
# light tool, cached 4 s) and display state (the local state file).
_light_cache = [0.0, None]
def _light_state():
    now = time.time()
    if _light_cache[1] is not None and now - _light_cache[0] < 4:
        return _light_cache[1]
    try:
        body = json.dumps({'tool': 'light', 'action': 'state', 'args': {}}).encode()
        req = urllib.request.Request('http://127.0.0.1:8099/tool?t=' + core_token(),
                                     data=body, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=6) as r:
            obj = json.loads(r.read())
        st = obj.get('state') or {}
        val = {'on': st.get('on'), 'brightness': st.get('brightness')}
    except Exception:
        val = {'on': None, 'brightness': None}
    _light_cache[0] = now; _light_cache[1] = val
    return val
# Lyra's mode (2026-11-19): the CORE owns mode state; this server
# consumes the mode from the core's pushed status slice (below),
# like every other state on the page -- it no longer reads a mode
# file. rtc_state.json is the core's published fact about whether
# an RTC peer is connected. Sleep is a mode now; the page's
# `sleep` flag derives from the pushed mode.
RTC_STATE_PATH = os.path.join(ROOT, 'rtc_state.json')
def get_mode():
    with _sub_lock: return _core_slice.get('mode') or 'wakegate'
def get_rtc_connected():
    try:
        with open(RTC_STATE_PATH) as f: return bool(json.load(f).get('connected'))
    except Exception: return False
def room_state():
    try:
        with open(os.path.join(ROOT, 'display_state.json')) as f:
            display_on = bool(json.load(f).get('display_on'))
    except Exception:
        display_on = True
    mode = get_mode()
    return {'light': _light_state(), 'display_on': display_on,
            'sleep': mode == 'sleep', 'mode': mode,
            'rtc_connected': get_rtc_connected()}
DIM_PATH = os.path.join(ROOT, 'dim.json')
def get_dim():
    try:
        with open(DIM_PATH) as f: return float(json.load(f).get('brightness', 1.0))
    except Exception: return 1.0
def set_dim(b):
    b = max(0.0, min(1.0, float(b)))
    with open(DIM_PATH, 'w') as f: json.dump({'brightness': b}, f)
    return b
# Alarm ring (2026-11-15): the core's TvRinger POSTs /ring to start/stop;
# the page polls GET /ring, loops assets/alarm-tone.opus while ringing and
# POSTs /ring/ack while actually sounding, so the core can tell a live
# ring from a dead page. State persists across a lyra-web restart, but a
# ring older than 20 min is stale (the core's hard stop is 15) and is
# dropped on read.
RING_PATH = os.path.join(ROOT, 'ring_state.json')
_last_ack = [0.0]
def get_ring():
    try:
        with open(RING_PATH) as f: st = json.load(f)
    except Exception: st = {'ringing': False, 'since': 0}
    if st.get('ringing') and time.time() - float(st.get('since') or 0) > 1200:
        st = {'ringing': False, 'since': 0}
    age = (time.time() - _last_ack[0]) if _last_ack[0] else None
    return {'ringing': bool(st.get('ringing')), 'since': st.get('since'), 'ack_age_s': age}
def set_ring(on):
    with open(RING_PATH, 'w') as f: json.dump({'ringing': bool(on), 'since': time.time()}, f)
    return get_ring()
def _write_job(wav, silent):
    """One speech job = one file (2026-11-15): streamed replies land
    as several jobs seconds apart and the page plays them from a
    queue, so a job must never overwrite the previous one's file."""
    _counter[0] += 1
    name = 'jobs/job-%d.wav' % _counter[0]
    with open(os.path.join(ROOT, name), 'wb') as f: f.write(wav)
    job = {'id': _counter[0], 'file': name}
    if silent: job['silent'] = True
    with open(os.path.join(ROOT, 'latest.json'), 'w') as f: json.dump(job, f)
    _broadcast('job', job)
    try:
        for old in os.listdir(os.path.join(ROOT, 'jobs')):
            if old.startswith('job-') and old.endswith('.wav') and int(old[4:-4]) < _counter[0] - 40:
                os.remove(os.path.join(ROOT, 'jobs', old))
    except Exception: pass

# ---------------------------------------------------------------------
# Pushed page state (2026-11-18): ONE channel for everything
# state-shaped on the TV page -- Daniel's state-delivery
# rule (pushed, never polled). The core pushes its slice (status
# light state, mode, activity, dependency health) to POST
# /statuspush on every change plus a 15 s heartbeat; this server
# merges it with its own slice (room light, display,
# RTC peer fact, dim, ring, next-alarm line -- the
# state files and cached core reads it already owns) and fans the
# merged state out to the pages over SSE at GET /status. Speech
# jobs are events on the same channel (see _write_job). Pages
# render from pushes only; a watcher thread here recomputes the
# web slice server-side and broadcasts ONLY on change -- that is
# service monitoring inside the server, not UI polling.
STATUS_STATE_PATH = os.path.join(ROOT, 'status_state.json')
_sub_lock = threading.Lock()
_subs = []
_core_slice = {'state': 'idle', 'label': 'Idle', 'fault': None,
               'mode': 'wakegate', 'activity': 'idle', 'health': {}}
_core_seen_at = [0.0]
_last_merged = [None]
_alarm_cache = [0.0, None]
CORE_STALE_S = 35

def _broadcast(kind, payload):
    with _sub_lock: subs = list(_subs)
    for q in subs:
        try: q.put_nowait((kind, payload))
        except Exception: pass

def _seed_core_slice():
    try:
        with open(STATUS_STATE_PATH) as f: snap = json.load(f)
        for k in ('state', 'label', 'fault', 'mode', 'activity', 'health'):
            if k in snap: _core_slice[k] = snap[k]
        _core_seen_at[0] = os.path.getmtime(STATUS_STATE_PATH)
    except Exception:
        _core_seen_at[0] = time.time()

def _accept_core_status(snap):
    if not isinstance(snap, dict): return
    with _sub_lock:
        for k in ('state', 'label', 'fault', 'mode', 'activity', 'health'):
            if k in snap: _core_slice[k] = snap[k]
        _core_seen_at[0] = time.time()
    _refresh_state()

def _alarm_cached():
    now = time.time()
    if now - _alarm_cache[0] > 15:
        try: _alarm_cache[1] = alarm_summary()
        except Exception: pass
        _alarm_cache[0] = now
    return _alarm_cache[1]

def _merged_state():
    with _sub_lock: core = dict(_core_slice)
    if _core_seen_at[0] and time.time() - _core_seen_at[0] > CORE_STALE_S:
        # The core pushes a heartbeat every 15 s; silence longer
        # than this is a dead core -- a service fault, shown red.
        core = dict(core, state='error', label='Core offline',
                    fault='Core offline')
    try:
        with open(os.path.join(ROOT, 'display_state.json')) as f:
            display_on = bool(json.load(f).get('display_on'))
    except Exception:
        display_on = True
    mode = core.get('mode') or 'wakegate'
    return {
        'state': core.get('state'), 'label': core.get('label'),
        'fault': core.get('fault'), 'mode': mode,
        'activity': core.get('activity'),
        'health': core.get('health') or {},
        'sleep': mode == 'sleep',
        'light': _light_state(),
        'display_on': display_on,
        'rtc_connected': get_rtc_connected(),
        'dim': get_dim(),
        'ring': {'ringing': bool(get_ring().get('ringing'))},
        'alarm': _alarm_cached(),
    }

def _refresh_state():
    state = _merged_state()
    if state != _last_merged[0]:
        _last_merged[0] = state
        _broadcast('state', state)
    return state

def _status_watcher():
    while True:
        try: _refresh_state()
        except Exception: pass
        time.sleep(2)

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=ROOT, **kw)
    def log_message(self, *a): pass
    def end_headers(self):
        # HTML must revalidate on every load: a stale page
        # silently lost its streaming client for a whole evening
        # (2026-11-18). If-Modified-Since still makes
        # an unchanged page a cheap 304.
        pth = urllib.parse.urlparse(self.path).path
        if pth.endswith('.html') or pth == '/':
            self.send_header('Cache-Control', 'no-cache')
        super().end_headers()
    def _sse_write(self, event, payload):
        if event: self.wfile.write(b'event: ' + event.encode() + b'\n')
        self.wfile.write(b'data: ' + json.dumps(payload).encode() + b'\n\n')
        self.wfile.flush()
    def _serve_status_stream(self):
        q = queue.Queue()
        with _sub_lock: _subs.append(q)
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            # Snapshot first (state-driven: a fresh or reconnected
            # page renders current truth at once), then transitions
            # as they are broadcast. The keepalive comment only
            # holds the socket open; it carries no state.
            self._sse_write(None, _last_merged[0] or _refresh_state())
            last_ping = time.time()
            while True:
                try: kind, payload = q.get(timeout=5)
                except queue.Empty: kind = None
                if kind is not None:
                    self._sse_write(None if kind == 'state' else kind, payload)
                if time.time() - last_ping >= 25:
                    self.wfile.write(b': keepalive\n\n'); self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            with _sub_lock:
                if q in _subs: _subs.remove(q)
    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == '/roomstate': return self._json(200, room_state())
        if u.path == '/status': return self._serve_status_stream()
        if u.path == '/alarm':
            try: return self._json(200, alarm_summary())
            except Exception as e: return self._json(502, {'error': str(e)})
        if u.path == '/dim': return self._json(200, {'brightness': get_dim()})
        if u.path == '/ring': return self._json(200, get_ring())
        return super().do_GET()
    def do_POST(self):
        if self.path == '/statuspush':
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0 or n > 64 * 1024: return self._json(413, {'error': 'bad size'})
                _accept_core_status(json.loads(self.rfile.read(n) or b'{}'))
                return self._json(200, {'ok': True})
            except Exception as e: return self._json(400, {'error': str(e)})
        if self.path == '/rtc/offer':
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0 or n > 256 * 1024: return self._json(413, {'error': 'bad size'})
                payload = self.rfile.read(n)
                code, obj = rtc_offer(payload)
                return self._json(code, obj)
            except urllib.error.HTTPError as e:
                try: return self._json(e.code, json.loads(e.read()))
                except Exception: return self._json(e.code, {'error': 'core error'})
            except Exception as e: return self._json(502, {'error': str(e)})
        if self.path == '/ring/ack':
            _last_ack[0] = time.time()
            return self._json(200, {'ok': True})
        if self.path == '/ring':
            try:
                req = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                return self._json(200, {'ok': True, **set_ring(bool(req.get('ringing')))})
            except Exception as e: return self._json(500, {'error': str(e)})
        if self.path == '/mirror':
            try:
                wav = self.rfile.read(int(self.headers.get('Content-Length') or 0))
                if not wav: return self._json(400, {'error': 'empty'})
                _write_job(wav, True)
            except Exception as e: return self._json(500, {'error': str(e)})
            return self._json(200, {'ok': True})
        if self.path == '/dim':
            try:
                req = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                b = req.get('brightness')
                if b is None and req.get('percent') is not None: b = float(req['percent']) / 100.0
                if b is None: return self._json(400, {'error': 'brightness or percent required'})
                return self._json(200, {'ok': True, 'brightness': set_dim(b)})
            except Exception as e: return self._json(500, {'error': str(e)})
        return self._json(404, {'error': 'not found'})
if __name__ == '__main__':
    os.makedirs(os.path.join(ROOT, 'jobs'), exist_ok=True)
    _seed_core_slice()
    _refresh_state()
    threading.Thread(target=_status_watcher, daemon=True).start()
    print('lyra-web on :8095', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8095), H).serve_forever()
