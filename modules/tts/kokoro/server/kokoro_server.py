#!/usr/bin/env python3
"""Lyra local TTS server (Kokoro) for the gadget.

POST /tts  {"text": "...", "voice": "af_heart", "speed": 1.0}
  header X-Lyra-Token must match ../tunnel/token.txt
  -> 200 {"data": "<base64 WAV 24kHz mono s16>"}
     (Gemini-shaped envelope on purpose: the gadget's existing feed
     parser scans the staged body for "data":" and handles WAV.)
POST /tts_stream  (same body and auth)
  -> 200, Transfer-Encoding: chunked, raw s16le PCM at 24 kHz.
     The text is split into sentence-sized pieces; each piece is
     synthesized and flushed as its own chunk, so the gadget plays a
     continuous stream from one connection and never stages a whole
     reply (streaming fix, 2026-10-25: the chunked-request path cut
     long replies off at chunk seams under memory pressure).
GET /health -> {"ok": true}

Every request is logged with one flushed line (the pre-stream server
logged nothing, which left speech failures with no server-side trace).

Engine: kokoro-onnx, model + voices from ~/Documents/LyraOS/modules/tts/kokoro/.
Synth calls are serialized behind one lock (single model instance).
"""
import base64, io, json, os, subprocess, threading, time, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np
from kokoro_onnx import Kokoro

HOME = os.path.expanduser("~")
MODEL = os.path.join(HOME, "Documents/LyraOS/modules/tts/kokoro/kokoro-v1.0.onnx")
VOICES = os.path.join(HOME, "Documents/LyraOS/modules/tts/kokoro/voices-v1.0.bin")
TOKEN_FILE = os.path.join(HOME, "Documents/LyraOS/modules/tts/kokoro/server/token.txt")
PORT = int(os.environ.get("LYRA_TTS_PORT", "8091"))
# Local only (Daniel, 2026-11-20): every consumer is on this box and
# dials 127.0.0.1, so the server never binds a routable interface.
HOST = os.environ.get("LYRA_TTS_HOST", "127.0.0.1")
DEFAULT_VOICE = os.environ.get("LYRA_TTS_VOICE", "af_heart")
# Pitch shift applied to every synthesized line (2026-11-15 trial:
# Daniel ear-testing af_heart shifted up). Semitones, tempo
# preserved via ffmpeg's rubberband filter; 0 = off. Set through
# the service unit's environment (LYRA_TTS_PITCH).
try:
    PITCH_SEMITONES = float(os.environ.get("LYRA_TTS_PITCH", "0"))
except ValueError:
    PITCH_SEMITONES = 0.0

with open(TOKEN_FILE) as f:
    TOKEN = f.read().strip()

_kokoro = Kokoro(MODEL, VOICES)
_lock = threading.Lock()

def log(msg):
    print(time.strftime("%H:%M:%S") + " kokoro: " + msg, flush=True)

def pitch_shift(pcm_bytes, sr):
    """Tempo-preserving pitch shift (ffmpeg rubberband) on raw
    s16le PCM. Any failure returns the unshifted PCM: a voice with
    the wrong pitch beats no voice."""
    if not PITCH_SEMITONES:
        return pcm_bytes
    factor = 2 ** (PITCH_SEMITONES / 12.0)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "s16le", "-ar", str(sr),
             "-ac", "1", "-i", "-",
             "-af", f"rubberband=pitch={factor:.6f}",
             "-f", "s16le", "-ar", str(sr), "-ac", "1", "-"],
            input=pcm_bytes, capture_output=True, timeout=30)
    except Exception as e:
        log(f"pitch shift failed: {e}")
        return pcm_bytes
    if proc.returncode != 0 or not proc.stdout:
        log(f"pitch shift failed ({proc.returncode}): "
            f"{proc.stderr.decode(errors='replace')[:200]}")
        return pcm_bytes
    return proc.stdout

def synth_pcm(text, voice, speed):
    with _lock:
        audio, sr = _kokoro.create(text, voice=voice, speed=speed, lang="en-us")
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    return pitch_shift(pcm.tobytes(), sr), sr

def synth_wav(text, voice, speed):
    pcm, sr = synth_pcm(text, voice, speed)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(pcm)
    return buf.getvalue()

def split_sentences(text):
    """Sentence-sized pieces (aim 60-220 chars): cut after ., !, ? or a
    newline once a piece is long enough; hard-split very long runs at a
    word boundary. Mirrors the gadget's tts_chunk_copy closely enough
    that streaming pieces feel like the old chunks sounded."""
    pieces, cur = [], ""
    for ch in text:
        cur += ch
        if len(cur) >= 60 and ch in ".!?\n":
            pieces.append(cur.strip())
            cur = ""
        elif len(cur) >= 220:
            cut = cur.rfind(" ", 40)
            if cut > 0:
                pieces.append(cur[:cut].strip())
                cur = cur[cut:]
    if cur.strip():
        pieces.append(cur.strip())
    return [p for p in pieces if p]

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, fmt, *args):
        pass
    def _send(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
    def _read_req(self):
        n = int(self.headers.get("Content-Length", "0"))
        req = json.loads(self.rfile.read(n) or b"{}")
        return ((req.get("text") or "").strip(),
                req.get("voice") or DEFAULT_VOICE,
                float(req.get("speed") or 1.0))
    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})
    def do_POST(self):
        if self.path not in ("/tts", "/tts_stream"):
            self._send(404, {"error": "not found"})
            return
        if self.headers.get("X-Lyra-Token") != TOKEN:
            self._send(403, {"error": "bad token"})
            return
        t0 = time.time()
        try:
            text, voice, speed = self._read_req()
        except Exception:
            self._send(400, {"error": "bad request"})
            return
        if not text:
            self._send(400, {"error": "empty text"})
            return
        if self.path == "/tts":
            try:
                wav = synth_wav(text, voice, speed)
            except Exception as e:
                log(f"/tts synth failed: {e}")
                self._send(500, {"error": "synth failed"})
                return
            log(f"/tts {len(text)} chars -> {len(wav)} B wav in {time.time()-t0:.2f}s")
            self._send(200, {"data": base64.b64encode(wav).decode()})
            return
        # /tts_stream: chunked raw PCM, one chunk per sentence piece.
        pieces = split_sentences(text)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/L16")
            self.send_header("X-Lyra-Rate", "24000")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            total = 0
            for piece in pieces:
                pcm, sr = synth_pcm(piece, voice, speed)
                if sr != 24000:
                    log(f"/tts_stream unexpected sample rate {sr}")
                if pcm:
                    self.wfile.write(f"{len(pcm):x}\r\n".encode() + pcm + b"\r\n")
                    self.wfile.flush()
                    total += len(pcm)
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            log(f"/tts_stream {len(text)} chars -> {len(pieces)} pieces, {total} B pcm in {time.time()-t0:.2f}s")
        except (BrokenPipeError, ConnectionResetError):
            log(f"/tts_stream client went away after {time.time()-t0:.2f}s")
        except Exception as e:
            log(f"/tts_stream failed: {e}")

if __name__ == "__main__":
    log(f"pitch shift: {PITCH_SEMITONES:+.1f} semitones")
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
