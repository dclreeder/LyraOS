import asyncio, sys, time, wave
from array import array
# --- suite bootstrap: repo-relative import paths (core + this dir) ---
import os as _os, sys as _sys
_HERE = _os.path.dirname(_os.path.abspath(__file__))
for _p in (_os.path.join(_HERE, "..", "core"), _HERE):
    if _p not in _sys.path:
        _sys.path.insert(0, _p)
# --- end suite bootstrap ---
from lyra_core.audio_http import AudioHttpServer
from lyra_core.config import CoreConfig
from lyra_core.pipeline import PipelineDriver
from lyra_core.session import VoiceSession
from lyra_core.status import StatusHub, StatusPublisher, check_dependencies
from lyra_core.verify import VerifyGate
from lyra_core.wyoming_client import WyomingConnection, synthesize

CLIP = "/home/dclreeder/Documents/LyraOS/modules/wakeword/data/clips/positives/wake_positive_20261111_061439_0.987.wav"
CHUNK = 3200  # 100 ms at 16 kHz s16 mono
T0 = time.time()
def log(*a): print("[%6.2f]" % (time.time() - T0), *a, flush=True)
def SIL(secs): return b"\x00\x00" * int(16000 * secs)

class FakeBox:
    def __init__(self, on_audio):
        self._on_audio = on_audio
        self.is_connected = True
    def evt_run_start(self): pass
    def evt_run_end(self): pass
    def evt_stt_start(self): pass
    def evt_stt_vad_start(self): pass
    def evt_stt_vad_end(self): pass
    def evt_stt_end(self, text): pass
    def evt_intent_start(self): pass
    def evt_intent_end(self): pass
    def evt_tts_start(self, text): pass
    def evt_tts_end(self, url): pass
    def evt_error(self, code, message=""): pass
    async def feed(self, pcm, pace=True):
        for off in range(0, len(pcm), CHUNK):
            await self._on_audio(pcm[off:off + CHUNK])
            if pace: await asyncio.sleep(0.09)

def read_pcm(path):
    with wave.open(path, "rb") as wf:
        return wf.readframes(wf.getnframes())

async def synth_16k(text):
    pcm = bytearray(); fmt = {}
    async with await WyomingConnection.connect("tcp://127.0.0.1:10200") as conn:
        await conn.send(synthesize(text, voice="af_heart"))
        while True:
            ev = await conn.receive()
            if ev is None: break
            if ev.type == "audio-start": fmt = ev.data
            elif ev.type == "audio-chunk": pcm += ev.payload
            elif ev.type == "audio-stop": break
    rate = fmt["rate"]
    if rate == 16000: return bytes(pcm)
    a = array("h"); a.frombytes(bytes(pcm))
    n_out = int(len(a) * 16000 / rate)
    out = array("h", bytes(2 * n_out))
    for i in range(n_out):
        x = i * rate / 16000
        j = int(x); frac = x - j
        v = a[j] * (1 - frac) + a[min(j + 1, len(a) - 1)] * frac
        out[i] = int(max(-32768, min(32767, v)))
    return out.tobytes()

async def heartbeat(hub):
    while True:
        hub.republish()
        await asyncio.sleep(5)

async def wait_idle(session, hub, deadline_s=60):
    end = time.time() + deadline_s
    while time.time() < end:
        if session.state == "idle" and hub.snapshot()["state"] == "idle":
            return True
        await asyncio.sleep(0.2)
    return False

def subseq(seq, want):
    it = iter(seq)
    return all(any(x == w for x in it) for w in want)

def dedupe(seq):
    out = []
    for x in seq:
        if not out or out[-1] != x:
            out.append(x)
    return out

async def main():
    cfg = CoreConfig.load()
    cfg.http.port = 8199
    hub = StatusHub()
    transitions = []
    def record(snap):
        transitions.append((round(time.time() - T0, 2), snap["state"], snap["label"]))
        log("HUB ->", snap["state"], "/", snap["label"])
    hub.subscribe(record)
    hub.subscribe(StatusPublisher())
    hub.set_health(await check_dependencies())
    driver = PipelineDriver(cfg)
    driver.on_speech = hub.note_speech
    # Silence (house rule): record the driver's TV-mirror posts
    # in-process so no bench speech reaches the TV page. (The
    # on_speech clock still fires; only the POST is intercepted.)
    mirrored = []
    async def _record_mirror(wav_bytes):
        mirrored.append(len(wav_bytes))
    import lyra_core.pipeline as _pipeline_mod
    _pipeline_mod._mirror_wav_to_tv = _record_mirror

    _session_processing = [False]
    _capture_speech = [False]
    det_marks = []
    def hook(kind, detail):
        if kind in ("detection", "vad_start", "vad_end", "verify", "silent_drop", "transcript", "state"):
            log("EVT", kind, detail)
        if kind == "detection":
            det_marks.append(round(time.time() - T0, 2))
        # production_main._status_event_hook, verbatim logic:
        if kind == "vad_start":
            _capture_speech[0] = True
        elif kind == "verify":
            if detail.get("verdict") == "pass" and not _session_processing[0]:
                _session_processing[0] = True
                hub.turn_begin()
                hub.set_listening("wakegate", False)
        elif kind == "silent_drop":
            hub.set_listening("wakegate", False)
        elif kind == "state":
            st = detail.get("state")
            if st == "capturing":
                _capture_speech[0] = False
                hub.set_listening("wakegate", True)
            elif st == "processing":
                if not _session_processing[0] and _capture_speech[0]:
                    _session_processing[0] = True
                    hub.turn_begin()
                    hub.set_listening("wakegate", False)
            elif st == "idle":
                hub.set_listening("wakegate", False)
                if _session_processing[0]:
                    _session_processing[0] = False
                    hub.turn_end()

    server = AudioHttpServer(cfg.http.bind, cfg.http.port)
    server.start()
    gate = VerifyGate(dry_run=False)
    session = VoiceSession(cfg, driver, server, verify_gate=gate, on_event=hook)
    box = FakeBox(session.handle_audio)
    session.attach_box(box)
    await session.start()
    hb = asyncio.ensure_future(heartbeat(hub))
    ok = True
    try:
        await asyncio.sleep(1.0)
        log("PHASE 1: pool positive (wake + command)")
        mark = len(transitions)
        await box.feed(read_pcm(CLIP))
        await box.feed(SIL(2))
        done = await wait_idle(session, hub)
        p1 = dedupe([t[1] for t in transitions[mark:]])
        log("PHASE 1 states:", p1, "| transcript:", session.last_transcript)
        good1 = (done and subseq(["idle"] + p1, ["listening", "thinking", "speaking", "idle"])
                 and "idle" not in p1[:p1.index("thinking")] if "thinking" in p1 else False)
        log("PHASE 1:", "PASS" if good1 else "FAIL")
        ok = ok and good1

        log("PHASE 2: synthesized wake word, then silence (wake-only turn)")
        wake = await synth_16k("Hey Lyra")
        log("wake clip: %.2f s of speech" % (len(wake) / 32000))
        mark = len(transitions)
        await box.feed(SIL(0.7) + wake + SIL(9))
        done = await wait_idle(session, hub, 45)
        p2 = [t for t in transitions[mark:]]
        p2s = dedupe([t[1] for t in p2])
        log("PHASE 2 states:", p2s)
        # listening spans gate-open (detection) to the verdict: the
        # capture start is the first transition after the mark.
        listen_span = 0.0
        p2d = []  # timestamp-deduped: first sighting of each run
        for t in p2:
            if not p2d or p2d[-1][1] != t[1]:
                p2d.append(t)
        for i, t in enumerate(p2d):
            if t[1] == "listening":
                nxt = p2d[i + 1][0] if i + 1 < len(p2d) else time.time() - T0
                listen_span = nxt - t[0]
                break
        good2 = (done and subseq(["idle"] + p2s, ["listening", "thinking", "speaking", "idle"])
                 and "idle" not in p2s[:p2s.index("thinking")] and listen_span >= 4.0)
        log("PHASE 2: pink held %.1f s ->" % listen_span, "PASS" if good2 else "FAIL")
        ok = ok and good2

        log("PHASE 3 (best effort): near-miss phrase, gate should drop it")
        mark = len(transitions)
        det_mark = len(det_marks)
        near = await synth_16k("Hey Laura")
        await box.feed(SIL(0.7) + near + SIL(9))
        done = await wait_idle(session, hub, 45)
        p3s = dedupe([t[1] for t in transitions[mark:]])
        log("PHASE 3 states:", p3s, "| detections this phase:", len(det_marks) - det_mark)
        if len(det_marks) > det_mark:
            good3 = done and "listening" in p3s and "speaking" not in p3s and p3s[-1] == "idle"
            log("PHASE 3:", "PASS" if good3 else "FAIL")
            ok = ok and good3
        else:
            log("PHASE 3: wake model did not fire on the near-miss; drop path not driven")
    finally:
        hb.cancel()
        await session.stop()
        server.stop()
    print("WAKEGATE LISTENING BENCH:", "PASS" if ok else "FAIL", flush=True)

asyncio.run(main())
