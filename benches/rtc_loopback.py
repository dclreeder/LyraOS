"""RTC loopback bench (suite: rtc_loopback).

Drives the PRODUCTION core's RTC seat with an aiortc peer. Phase 1
plays a time question and expects one answered turn; phase 2 plays
a story request, waits for the reply to flow, then barges in with
a second question and expects the cut plus a new answer. Turn
accounting comes from the core journal, never from log files.
Manages its own preconditions: the kiosk is stopped (the TV page
contends for the single RTC seat) and the mode is set to rtc;
both are restored in the finally. Run via benches/run_all.py.
"""
import asyncio, json, math, os, sys, time, urllib.request, wave
from array import array
from fractions import Fraction
# --- suite bootstrap: repo-relative import paths (core + this dir) ---
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.join(_HERE, "..", "core"), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end suite bootstrap ---
from lyra_core.wyoming_client import WyomingConnection, synthesize
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

CORE_CFG = {}
from lyra_core.config import CoreConfig as _CC
TTS_URI = 'tcp://127.0.0.1:10200'; VOICE = 'af_heart'
RTC_STATE = os.path.expanduser('~/Documents/LyraOS/web/rtc_state.json')
STATUS_STATE = os.path.expanduser('~/Documents/LyraOS/web/status_state.json')
DASH = 'http://127.0.0.1:8098' 
T0 = time.time()
def log(*a): print('[%6.1f]' % (time.time() - T0), *a, flush=True)

async def synth(text, path):
    pcm = bytearray(); fmt = {}
    async with await WyomingConnection.connect(TTS_URI) as conn:
        await conn.send(synthesize(text, voice=VOICE))
        while True:
            ev = await conn.receive()
            if ev is None: break
            if ev.type == 'audio-start': fmt = ev.data
            elif ev.type == 'audio-chunk': pcm += ev.payload
            elif ev.type == 'audio-stop': break
    with wave.open(path, 'wb') as w:
        w.setnchannels(fmt['channels']); w.setsampwidth(fmt['width'])
        w.setframerate(fmt['rate']); w.writeframes(bytes(pcm))
    dur = len(pcm) / (fmt['rate'] * fmt['width'] * fmt['channels'])
    log('synth', repr(text[:40]), '->', path, '%.1fs' % dur, fmt['rate'], 'Hz')
    return dur

class ClipTrack(MediaStreamTrack):
    kind = 'audio'
    def __init__(self):
        super().__init__()
        self._pcm = b''; self._rate = 24000; self._pos = 0
        self._pts = 0; self._start = None
    def load_wav(self, path):
        with wave.open(path, 'rb') as w:
            self._rate = w.getframerate(); self._pcm = w.readframes(w.getnframes())
        self._pos = 0
        return len(self._pcm) / 2 / self._rate
    async def recv(self):
        if self._start is None: self._start = time.time()
        per = self._rate // 50  # 20 ms of s16 mono
        chunk = self._pcm[self._pos:self._pos + per * 2]
        self._pos += len(chunk)
        if len(chunk) < per * 2: chunk = chunk + b'\x00' * (per * 2 - len(chunk))
        f = AudioFrame(format='s16', layout='mono', samples=per)
        f.planes[0].update(chunk)
        f.sample_rate = self._rate; f.pts = self._pts; f.time_base = Fraction(1, self._rate)
        self._pts += per
        delay = self._start + self._pts / self._rate - time.time()
        if delay > 0: await asyncio.sleep(delay)
        return f

class Recorder:
    def __init__(self): self.t0 = time.time(); self.rows = []
    async def run(self, track):
        while True:
            try: frame = await track.recv()
            except Exception: break
            raw = bytes(frame.planes[0])
            a = array('h'); a.frombytes(raw[:len(raw) // 2 * 2])
            rms = math.sqrt(sum(float(x) * x for x in a) / len(a)) if a else 0.0
            self.rows.append((time.time() - self.t0, rms))
    def loud_after(self, t, thresh=200.0):
        return any(r > thresh for (tt, r) in self.rows if tt >= t)
    def first_loud_after(self, t, thresh=200.0):
        for (tt, r) in self.rows:
            if tt >= t and r > thresh: return tt
        return None
    def silent_from(self, t, dur=0.6, thresh=200.0):
        run = 0.0; prev = None
        for (tt, r) in self.rows:
            if tt < t: continue
            if r <= thresh:
                run = run + (tt - prev if prev else 0.02); prev = tt
                if run >= dur: return tt - dur
            else:
                run = 0.0; prev = tt
        return None

# Turn accounting (2026-11-19 suite consolidation): the core
# journal is the only ledger. This bench used to count turns from
# memory/inbox/*.jsonl -- the Phase 0 logs deleted 2026-11-17 --
# which made its PASS string unobtainable. Transcripts and the
# barge now come from lyra-core's journal lines:
#   rtc transcript [parakeet-tdt]: '<text>'
#   rtc barge-in: reply interrupted by peer speech
import re as _re
import subprocess as _sp

def journal_since(ts):
    since = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))
    return _sp.run(['journalctl', '--user', '-u', 'lyra-core.service',
                    '--since', since, '--no-pager', '-o', 'cat'],
                   capture_output=True, text=True).stdout

def rtc_transcripts(ts):
    return _re.findall(r"rtc transcript \[parakeet-tdt\]: '([^']*)'",
                        journal_since(ts))

def rtc_barged(ts):
    return 'rtc barge-in: reply interrupted by peer speech' in journal_since(ts)

def read_mode():
    return json.load(open(STATUS_STATE))['mode']

def set_mode(mode):
    body = json.dumps({'mode': mode}).encode()
    req = urllib.request.Request(DASH + '/api/mode', data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

async def wait_mode(mode, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if read_mode() == mode: return True
        except Exception: pass
        await asyncio.sleep(0.5)
    return False

def sh(*args):
    env = dict(os.environ, XDG_RUNTIME_DIR='/run/user/1000',
               DBUS_SESSION_BUS_ADDRESS='unix:path=/run/user/1000/bus')
    return _sp.run(args, env=env, capture_output=True, text=True)

def post_offer(sdp, typ):
    body = json.dumps({'sdp': sdp, 'type': typ}).encode()
    req = urllib.request.Request('http://127.0.0.1:8095/rtc/offer', data=body,
                                 headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r: return json.loads(r.read())

async def connect(track):
    pc = RTCPeerConnection()
    pc.addTrack(track)
    rec = Recorder()
    @pc.on('track')
    def _on_track(t): asyncio.ensure_future(rec.run(t))
    offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    await asyncio.sleep(1.5)
    ans = await asyncio.get_event_loop().run_in_executor(
        None, post_offer, pc.localDescription.sdp, pc.localDescription.type)
    await pc.setRemoteDescription(RTCSessionDescription(sdp=ans['sdp'], type=ans['type']))
    for _ in range(60):
        if pc.connectionState == 'connected': break
        await asyncio.sleep(0.5)
    log('peer connectionState =', pc.connectionState)
    return pc, rec

async def main():
    orig_mode = read_mode()
    log('original mode:', orig_mode)
    ok1 = ok2 = False
    pcs = []
    # The live TV page and this bench contend for the single RTC
    # seat (the page re-offers and steals the session -- seen in
    # the 2026-11-19 verifications), so the kiosk stops for the
    # duration and is restored in the finally, as those runs did.
    sh('systemctl', '--user', 'stop', 'lyra-kiosk.service')
    try:
        set_mode('rtc')
        if not await wait_mode('rtc'):
            print('RTC LOOPBACK BENCH: FAIL (mode never became rtc)', flush=True)
            return
        await asyncio.sleep(2.0)
        await synth('What time is it?', '/tmp/rtc_time.wav')
        await synth('Tell me a long story about a dragon and a lighthouse keeper.', '/tmp/rtc_story.wav')
        # ---- PHASE 1: one utterance -> one answered turn ----
        t1 = time.time()
        pc = rec = None; dur = 0.0
        for attempt in range(3):
            track = ClipTrack(); dur = track.load_wav('/tmp/rtc_time.wav')
            try:
                pc, rec = await connect(track); break
            except Exception as e:
                log('phase1 connect attempt', attempt + 1, 'failed:', repr(e))
                await asyncio.sleep(2)
        if pc is None:
            log('PHASE1 loopback: FAIL (peer never connected)')
        else:
            pcs.append(pc)
            await asyncio.sleep(2)
            try: log('rtc_state mid-session:', json.load(open(RTC_STATE)))
            except Exception as e: log('rtc_state read failed:', e)
            for _ in range(40):
                await asyncio.sleep(1)
                turns = rtc_transcripts(t1 - 2)
                if turns and rec.loud_after(dur + 1.0): ok1 = True; break
            turns = rtc_transcripts(t1 - 2)
            log('PHASE1 loopback:', 'PASS' if ok1 else 'FAIL')
            for t in turns: log('  rtc transcript:', repr(t))
            log('  reply audio first loud at', rec.first_loud_after(dur + 1.0))
            await pc.close(); pcs.remove(pc); await asyncio.sleep(2)
        # ---- PHASE 2: story, then a real barge-in ----
        t2 = time.time()
        pc2 = rec2 = None; dur2 = 0.0; track2 = None
        for attempt in range(3):
            track2 = ClipTrack(); dur2 = track2.load_wav('/tmp/rtc_story.wav')
            try:
                pc2, rec2 = await connect(track2); break
            except Exception as e:
                log('phase2 connect attempt', attempt + 1, 'failed:', repr(e))
                await asyncio.sleep(2)
        if pc2 is None:
            log('PHASE2 barge-in: FAIL (peer never connected)')
        else:
            pcs.append(pc2)
            flowing = None
            for _ in range(45):
                await asyncio.sleep(1)
                flowing = rec2.first_loud_after(dur2 + 1.0)
                if flowing is not None and rec2.loud_after(flowing + 1.5): break
            if flowing is None:
                log('PHASE2 barge-in: FAIL (story reply never flowed)')
            else:
                log('story reply flowing since', round(flowing, 1), '- interrupting now')
                t_int = time.time() - rec2.t0
                track2.load_wav('/tmp/rtc_time.wav')
                cut = None; answered = None
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    if cut is None: cut = rec2.silent_from(t_int + 0.2)
                    if cut is not None:
                        answered = rec2.first_loud_after(cut + 0.8)
                        if answered is not None: break
                turns2 = rtc_transcripts(t2 - 2)
                barged = rtc_barged(t2 - 2)
                ok2 = (cut is not None and answered is not None and barged
                       and len(turns2) >= 2)
                log('PHASE2 barge-in:', 'PASS' if ok2 else 'FAIL',
                    '| cut at', None if cut is None else round(cut, 2),
                    '| new answer at', None if answered is None else round(answered, 2),
                    '| journal barge:', barged, '| transcripts:', len(turns2))
                for t in turns2: log('  rtc transcript:', repr(t))
            await pc2.close(); pcs.remove(pc2); await asyncio.sleep(1)
    finally:
        for pc in pcs:
            try: await pc.close()
            except Exception: pass
        try: set_mode(orig_mode)
        except Exception as e: log('mode restore failed:', e)
        await wait_mode(orig_mode)
        sh('systemctl', '--user', 'start', 'lyra-kiosk.service')
    print('RTC LOOPBACK BENCH:', 'PASS' if (ok1 and ok2) else 'FAIL', flush=True)

asyncio.run(main())
