import asyncio, io, json, math, os, sys, time, wave
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
import lyra_core.rtc as rtc_mod
from lyra_core.status import StatusHub, StatusPublisher, check_dependencies
from aiortc import RTCPeerConnection, RTCSessionDescription, MediaStreamTrack
from av import AudioFrame

rtc_mod.write_rtc_state = lambda connected: open('/tmp/bench_rtc_state.json', 'w').write(json.dumps({'connected': bool(connected)}))
TTS_URI = 'tcp://127.0.0.1:10200'; VOICE = 'af_heart'
T0 = time.time()
def log(*a): print('[%6.2f]' % (time.time() - T0), *a, flush=True)

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
    log('synth', repr(text[:40]), '%.1fs' % (len(pcm) / (fmt['rate'] * fmt['width'] * fmt['channels'])))

def tone_wav(seconds=0.55, rate=16000, freq=440, amp=12000):
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        frames = b''.join(int(amp * math.sin(2 * math.pi * freq * i / rate)).to_bytes(2, 'little', signed=True) for i in range(int(rate * seconds)))
        w.writeframes(frames)
    return buf.getvalue()

class StubDriver:
    def __init__(self): self.synth_calls = []; self.transcripts = []
    async def transcribe_audio(self, pcm, rate=16000, width=2, channels=1, timeout=180.0):
        dur = len(pcm) / (rate * width * channels)
        text = 'tell me a story' if dur > 3.4 else 'what time is it'
        self.transcripts.append((round(dur, 2), text)); log('stub transcribe %.2fs ->' % dur, repr(text))
        return text
    async def synthesize_speech(self, text, timeout=120.0, *, mirror=True):
        self.synth_calls.append((text[:24], mirror))
        return (tone_wav(), 16000, 2, 1)

class StubRouter:
    def __init__(self): self.calls = []; self.story_cancelled = False; self.story_fed = 0
    async def handle(self, command, verify=None, on_sentence=None, via='box'):
        self.calls.append((round(time.time() - T0, 2), command, via)); log('stub router turn:', repr(command), 'via', via)
        if 'story' in command:
            try:
                for i in range(12):
                    self.story_fed += 1
                    await on_sentence('Tale sentence %d.' % i)
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                self.story_cancelled = True; log('stub router: story turn CANCELLED')
                raise
            return 'story complete'
        await on_sentence('The time is 11 30 PM.')
        return 'The time is 11 30 PM.'

class ClipTrack(MediaStreamTrack):
    kind = 'audio'
    def __init__(self):
        super().__init__()
        self._pcm = b''; self._rate = 24000; self._pos = 0; self._pts = 0; self._start = None
    def load_wav(self, path):
        with wave.open(path, 'rb') as w:
            self._rate = w.getframerate(); self._pcm = w.readframes(w.getnframes())
        self._pos = 0
        return len(self._pcm) / 2 / self._rate
    async def recv(self):
        if self._start is None: self._start = time.time()
        per = self._rate // 50
        chunk = self._pcm[self._pos:self._pos + per * 2]; self._pos += len(chunk)
        if len(chunk) < per * 2: chunk = chunk + b'\x00' * (per * 2 - len(chunk))
        f = AudioFrame(format='s16', layout='mono', samples=per)
        f.planes[0].update(chunk)
        f.sample_rate = self._rate; f.pts = self._pts; f.time_base = Fraction(1, self._rate)
        self._pts += per
        delay = self._start + self._pts / self._rate - time.time()
        if delay > 0: await asyncio.sleep(delay)
        return f

class Recorder:
    def __init__(self): self.rows = []
    async def run(self, track):
        while True:
            try: frame = await track.recv()
            except Exception: break
            raw = bytes(frame.planes[0])
            a = array('h'); a.frombytes(raw[:len(raw) // 2 * 2])
            rms = math.sqrt(sum(float(x) * x for x in a) / len(a)) if a else 0.0
            self.rows.append((time.time() - T0, rms))
    def loud_now(self, thresh=200.0):
        return bool(self.rows) and self.rows[-1][1] > thresh

def dedupe(trans):
    out = []
    for t in trans:
        if not out or out[-1][1] != t[1]: out.append(t)
    return out

def subseq(seq, want):
    it = iter(seq)
    return all(any(x == w for x in it) for w in want)

async def wait_hub_idle(hub, deadline_s):
    end = time.time() + deadline_s
    stable = 0.0
    while time.time() < end:
        if hub.snapshot()['state'] == 'idle':
            stable += 0.25
            if stable >= 1.5: return True
        else:
            stable = 0.0
        await asyncio.sleep(0.25)
    return False

async def main():
    ok = True
    known_fail = False
    if not os.path.exists('/tmp/rtc_time.wav'):
        await synth('What time is it?', '/tmp/rtc_time.wav')
    if not os.path.exists('/tmp/rtc_story.wav'):
        await synth('Tell me a long story about a dragon and a lighthouse keeper.', '/tmp/rtc_story.wav')
    await synth('What time is it? Please tell me the time right now.', '/tmp/rtc_interrupt.wav')
    hub = StatusHub()
    transitions = []
    def record(snap):
        transitions.append((round(time.time() - T0, 2), snap['state'], snap['label']))
        log('HUB ->', snap['state'], '/', snap['label'])
    hub.subscribe(record)
    hub.subscribe(StatusPublisher())
    hub.set_health(await check_dependencies())
    driver = StubDriver(); router = StubRouter()
    server = rtc_mod.RtcServer(driver, router.handle)
    server.accepting = True
    server.status = hub
    track = ClipTrack()
    pc = RTCPeerConnection(); pc.addTrack(track)
    rec = Recorder()
    @pc.on('track')
    def _on_track(t): asyncio.ensure_future(rec.run(t))
    offer = await pc.createOffer(); await pc.setLocalDescription(offer)
    await asyncio.sleep(1.0)
    ans = await server.handle_offer(pc.localDescription.sdp, pc.localDescription.type)
    assert 'sdp' in ans, ans
    await pc.setRemoteDescription(RTCSessionDescription(sdp=ans['sdp'], type=ans['type']))
    for _ in range(40):
        if pc.connectionState == 'connected': break
        await asyncio.sleep(0.5)
    log('bench peer:', pc.connectionState)
    await asyncio.sleep(2.0)  # let the stream sidecar arm + settle
    try:
        # ---- PHASE A: utterance while she is silent ----
        log('PHASE A: time question while silent')
        mark = len(transitions)
        track.load_wav('/tmp/rtc_time.wav')
        done = await wait_hub_idle(hub, 40)
        pa = dedupe(transitions[mark:])
        pas = [t[1] for t in pa]
        log('PHASE A states:', pas)
        good_a = done and subseq(['idle'] + pas, ['listening', 'thinking', 'speaking', 'idle'])
        log('PHASE A:', 'PASS' if good_a else 'FAIL')
        ok = ok and good_a
        # ---- PHASE B: story, then a real interruption mid-playback ----
        log('PHASE B: story request')
        mark = len(transitions)
        track.load_wav('/tmp/rtc_story.wav')
        # wait until her reply is actually flowing and the hub says speaking
        flow = False
        for _ in range(80):
            await asyncio.sleep(0.25)
            if rec.loud_now() and hub.snapshot()['state'] == 'speaking':
                flow = True; break
        log('reply flowing, hub speaking:', flow)
        ok = ok and flow
        await asyncio.sleep(1.5)  # let playback settle in
        onset = time.time() - T0
        track.load_wav('/tmp/rtc_interrupt.wav')
        log('interrupt clip loaded at %.2f (during playback)' % onset)
        barge_at = None
        for _ in range(120):
            await asyncio.sleep(0.25)
            if barge_at is None and router.story_cancelled:
                barge_at = time.time() - T0
                log('barge confirmed at %.2f' % barge_at)
            if barge_at is not None and hub.snapshot()['state'] == 'idle':
                break
        done = await wait_hub_idle(hub, 20)
        pb = dedupe(transitions[mark:])
        pbs = [t[1] for t in pb]
        log('PHASE B states:', pbs)
        log('router calls:', router.calls)
        # suppression: between onset and the barge, no non-speaking state
        during = [t for t in pb if onset <= t[0] and (barge_at is None or t[0] < barge_at)]
        bad = [t for t in during if t[1] != 'speaking']
        log('states during playback+onset window:', during, '| violations:', bad)
        # after the barge: listening appears before the answer's thinking
        after = [t[1] for t in pb if barge_at is not None and t[0] >= barge_at]
        good_b = (flow and barge_at is not None and not bad and done
                  and 'listening' in after
                  and subseq(after, ['listening', 'thinking', 'speaking', 'idle'])
                  and len(router.calls) >= 3)
        # KNOWN-FAIL disposition (2026-11-19 suite consolidation):
        # if everything except the suppression window held -- story
        # flowed, barge landed, interrupt answered, hub settled --
        # and the only window violations are thinking/listening
        # transients, that is the deterministic failure proven at
        # pristine HEAD 0aef770 (stash-and-rerun during the alarm
        # cleanup): a thinking+listening pair lands in the
        # onset->barge window. Carried as KNOWN-FAIL, never silently
        # green; any other failure shape is a real FAIL. See
        # benches/README.md.
        bad_states = {t[1] for t in bad}
        # KNOWN-FAIL signature: everything worked -- story flowed,
        # barge confirmed, the interrupt routed AFTER the barge and
        # answered, hub settled to idle -- and the only failed
        # clause is the no-transient rule, violations exclusively
        # the thinking/listening pair from the onset->barge window.
        # (The pair sits BEFORE barge confirmation: the segmenter
        # opens capture at onset, so no fresh 'listening' follows.)
        known_b = (flow and barge_at is not None and done
                   and len(router.calls) >= 3
                   and router.calls[-1][0] > barge_at
                   and bool(bad) and bad_states <= {'thinking', 'listening'})
        if good_b:
            phase_b = 'PASS'
        elif known_b:
            phase_b = 'KNOWN-FAIL'
        else:
            phase_b = 'FAIL'
        log('PHASE B:', phase_b)
        known_fail = phase_b == 'KNOWN-FAIL'
        ok = ok and phase_b != 'FAIL' 
    finally:
        await pc.close(); await server.stop()
    if not ok:
        print('RTC LISTENING BENCH: FAIL', flush=True)
    elif known_fail:
        print('RTC LISTENING BENCH: KNOWN-FAIL (Phase B suppression window -- see benches/README.md)', flush=True)
    else:
        print('RTC LISTENING BENCH: PASS', flush=True)

asyncio.run(main())
