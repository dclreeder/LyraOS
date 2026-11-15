# Module: TTS / Kokoro

Local text-to-speech engine assets (Kokoro-82M, ONNX build). One module,
many consumers — projects load these files through their speech code;
nothing copies them into a project folder.

## Contents
- `kokoro-v1.0.onnx` — the Kokoro v1.0 model.
- `voices-v1.0.bin` — voice pack (the bridge's default voice is `af_heart`).

## Entry point
In-process, via the `kokoro_onnx` Python package (installed in the
wakeword module's venv, `~/Documents/modules/wakeword/training/venv`):

```python
from kokoro_onnx import Kokoro
tts = Kokoro("~/Documents/modules/tts/kokoro/kokoro-v1.0.onnx",
             "~/Documents/modules/tts/kokoro/voices-v1.0.bin")
audio_f32, sample_rate = tts.create(text, voice="af_heart", lang="en-us")
```

Output is float audio at 24 kHz.

## Consumers
- Lyra Box bridge (`bridge/bridge_pipeline.py`, class `TTS`) — local speech.
- Wakeword training (`modules/wakeword/training/generate_data.py`) —
  synthetic positive clips.

Note: `~/HAOS/` runs its own independent Kokoro install (Wyoming
service); it is a separate project and does not use this module.

Moved here 2026-10-22 (modules reorganization; previously
`Lyra Box/bridge/models/`).
