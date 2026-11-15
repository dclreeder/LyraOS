"""Generate synthetic training data for hey Lyra wake word using Kokoro TTS."""
import os
import sys
import numpy as np
import scipy.io.wavfile

# kokoro_onnx comes from this module's own venv (training/venv);
# model files live in the TTS module (modules/tts/kokoro).
from kokoro_onnx import Kokoro

MODEL = "/home/dclreeder/Documents/LyraOS/modules/tts/kokoro/kokoro-v1.0.onnx"
VOICES = "/home/dclreeder/Documents/LyraOS/modules/tts/kokoro/voices-v1.0.bin"
OUT = "/home/dclreeder/Documents/LyraOS/modules/wakeword/training/output/hey_lyra_v2"

kokoro = Kokoro(MODEL, VOICES)

# Get voice list
import glob
# Kokoro voices are stored in the bin file; list from known names
VOICE_NAMES = [
    "af_heart", "af_alloy", "af_aoede", "af_bella", "af_jessica", "af_kore",
    "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
    "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
    "am_onyx", "am_puck", "am_santa",
    "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
    "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    "ef_dora", "em_alex", "em_santa",
    "ff_siw", "hf_alpha", "hf_beta", "hm_omega", "hm_psi",
    "if_sara", "im_nicola",
    "jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro",
    "jm_kumo", "pf_dora", "pm_alex", "pm_santa",
]

print(f"Using {len(VOICE_NAMES)} voices", flush=True)

def save_wav(audio, sr, path):
    # Kokoro outputs 24kHz float32; convert to 16kHz int16
    import scipy.signal
    if sr != 16000:
        num_samples = int(len(audio) * 16000 / sr)
        audio = scipy.signal.resample(audio, num_samples)
        sr = 16000
    audio_i16 = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    scipy.io.wavfile.write(path, sr, audio_i16)

def generate(text, voice, speed, path):
    try:
        audio, sr = kokoro.create(text, voice=voice, speed=speed, lang="en-us")
        save_wav(audio, sr, path)
        return True
    except Exception as e:
        print(f"  Failed: {text} / {voice}: {e}", flush=True)
        return False

# --- Positives ---
positive_phrases = [
    "hey Lyra", "hey Lyra.", "hey, Lyra", "hey Lyra!",
    "Hey Lyra", "HEY LYRA", "hey lyra",
    "uh, hey Lyra", "hey Lyra, ",
]
speeds = [0.85, 0.95, 1.0, 1.05, 1.15]

count = 0
for phrase in positive_phrases:
    for voice in VOICE_NAMES:
        for speed in speeds:
            # 90% train, 10% test
            subdir = "positive_test" if (count % 10 == 9) else "positive_train"
            path = os.path.join(OUT, subdir, f"pos_{count:05d}.wav")
            if generate(phrase, voice, speed, path):
                count += 1
    print(f"Positives: {count} generated", flush=True)

print(f"Total positives: {count}", flush=True)

# --- Hard negatives (confusable phrases) ---
hard_negatives = [
    "hey Lara", "hey Lira", "hey Lyla", "hey Lila",
    "hey Sarah", "hey Tara", "hey Kara", "hey Laura",
    "a Lyra", "the Lyra", "my Lyra", "hey lyre",
    "play Lyra", "say Lyra", "see Lyra", "be Lyra",
    "hey Larry", "hey Barry", "hey Harry", "hey Mary",
    "hey Lira!", "hey, Lara", "oh Lyra", "hi Lyra",
    "hey Lura", "hey Lyro", "hey Lyri",
]

count = 0
for phrase in hard_negatives:
    for voice in VOICE_NAMES[:20]:  # subset of voices for negatives
        for speed in [0.95, 1.0, 1.05]:
            subdir = "negative_test" if (count % 10 == 9) else "negative_train"
            path = os.path.join(OUT, subdir, f"hardneg_{count:05d}.wav")
            if generate(phrase, voice, speed, path):
                count += 1
    print(f"Hard negatives: {count} generated", flush=True)

print(f"Total hard negatives: {count}", flush=True)

# --- General negatives (random speech) ---
general_phrases = [
    "what time is it", "turn on the lights", "play some music",
    "what is the weather today", "set a timer for five minutes",
    "how are you doing", "tell me a joke", "what is for dinner",
    "call mom", "send a text message", "open the garage door",
    "lock the front door", "turn off the TV", "volume up",
    "volume down", "pause the music", "next song",
    "what did you say", "I did not hear you", "can you repeat that",
    "good morning", "good night", "hello there", "how is it going",
    "the quick brown fox jumps over the lazy dog",
    "she sells seashells by the seashore",
    "how much wood would a woodchuck chuck",
]

count = 0
for phrase in general_phrases:
    for voice in VOICE_NAMES[:15]:
        for speed in [0.95, 1.05]:
            subdir = "negative_test" if (count % 10 == 9) else "negative_train"
            path = os.path.join(OUT, subdir, f"genneg_{count:05d}.wav")
            if generate(phrase, voice, speed, path):
                count += 1
    if count % 500 == 0:
        print(f"General negatives: {count} generated", flush=True)

print(f"Total general negatives: {count}", flush=True)
print("DONE", flush=True)
