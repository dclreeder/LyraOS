#!/usr/bin/env python3
"""Lyra web viewer server: static pages, /mirror speech jobs, /ring, /dim, and the streamed /call proxy to the core."""
import base64, json, os, threading, time, urllib.request, urllib.error, urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
ROOT = os.path.expanduser('~/Documents/LyraOS/web')
_lock = threading.Lock(); _counter = [0]
try:
    with open(os.path.join(ROOT, 'latest.json')) as _f: _counter[0] = int(json.load(_f).get('id') or 0)
except Exception: pass
# Dashboard call (2026-11-15): the call page POSTs a WAV utterance to
# /call/start here; this server relays it to the core's /call/start
# with the shared token read server-side (same secrets file the
# dashboard reads). The core transcribes and returns a call id at
# once; the reply is then synthesized sentence by sentence and the
# page polls /call/poll for the sentences as they land.
def core_token():
    with open(os.path.expanduser('~/Documents/LyraOS/core/alarm_secrets.json')) as f:
        return json.load(f)['kill_token']
def call_start(wav):
    req = urllib.request.Request('http://127.0.0.1:8099/call/start?t=' + core_token(),
                                 data=wav, headers={'Content-Type': 'audio/wav'})
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.status, json.loads(r.read())
def call_poll(call_id, after):
    q = urllib.parse.urlencode({'t': core_token(), 'call': call_id, 'after': after})
    with urllib.request.urlopen('http://127.0.0.1:8099/call/poll?' + q, timeout=15) as r:
        return r.status, json.loads(r.read())
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
# Call state (2026-11-15): the call page posts {active: true} when a
# call starts and re-posts it every 10 s as a heartbeat; the TV page
# polls GET /callstate and, while a call is live, flips Lyra to her
# Arm B pose (both fists up) with a phone prop at her hand -- she is
# on a video call, holding her phone up. A heartbeat older than 25 s
# reads as no call, so a crashed call page cannot strand the pose.
CALL_STATE_PATH = os.path.join(ROOT, 'call_state.json')
_call_hb = [0.0]
def get_call_state():
    try:
        with open(CALL_STATE_PATH) as f: st = json.load(f)
    except Exception: st = {'active': False}
    age = (time.time() - _call_hb[0]) if _call_hb[0] else None
    active = bool(st.get('active')) and age is not None and age < 25
    return {'active': active, 'hb_age_s': age}
def set_call_state(on):
    with open(CALL_STATE_PATH, 'w') as f: json.dump({'active': bool(on)}, f)
    _call_hb[0] = time.time()
    return get_call_state()
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
    try:
        for old in os.listdir(os.path.join(ROOT, 'jobs')):
            if old.startswith('job-') and old.endswith('.wav') and int(old[4:-4]) < _counter[0] - 40:
                os.remove(os.path.join(ROOT, 'jobs', old))
    except Exception: pass

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw): super().__init__(*a, directory=ROOT, **kw)
    def log_message(self, *a): pass
    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        if u.path == '/call/poll':
            try:
                q = urllib.parse.parse_qs(u.query)
                call_id = (q.get('call') or [''])[0]
                if not call_id: return self._json(400, {'error': 'call id required'})
                after = int((q.get('after') or ['0'])[0])
                code, obj = call_poll(call_id, after)
                return self._json(code, obj)
            except urllib.error.HTTPError as e:
                try: return self._json(e.code, json.loads(e.read()))
                except Exception: return self._json(e.code, {'error': 'core error'})
            except Exception as e: return self._json(502, {'error': str(e)})
        if u.path == '/callstate': return self._json(200, get_call_state())
        if u.path == '/dim': return self._json(200, {'brightness': get_dim()})
        if u.path == '/ring': return self._json(200, get_ring())
        return super().do_GET()
    def do_POST(self):
        if self.path == '/call/start':
            try:
                n = int(self.headers.get('Content-Length') or 0)
                if n <= 0 or n > 8 * 1024 * 1024: return self._json(413, {'error': 'bad size'})
                wav = self.rfile.read(n)
                code, obj = call_start(wav)
                return self._json(code, obj)
            except urllib.error.HTTPError as e:
                try: return self._json(e.code, json.loads(e.read()))
                except Exception: return self._json(e.code, {'error': 'core error'})
            except Exception as e: return self._json(502, {'error': str(e)})
        if self.path == '/callstate':
            try:
                req = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
                return self._json(200, {'ok': True, **set_call_state(bool(req.get('active')))})
            except Exception as e: return self._json(500, {'error': str(e)})
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
    print('lyra-web on :8095', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 8095), H).serve_forever()
