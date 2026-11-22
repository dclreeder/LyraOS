"""Enroll Daniel's voiceprint + calibrate the speaker threshold.

Data: the wake clip pools (modules/wakeword/data/clips). Positives
are Daniel (filed on wake PASS); negatives are everything the gate
rejected (TV, noise, other sounds) -- the not-Daniel distribution.
Positives split deterministically: even indices enroll, odd are
held out as genuine trials. Prints the score distributions and
writes data/speaker/daniel_voiceprint.npy + threshold.txt.

Threshold rule: midpoint between the genuine 1st percentile and
the impostor 99th percentile, clamped to [0.25, 0.55] -- the two
error directions both matter (a guest filed as Daniel pollutes
memory; Daniel filed as a guest confuses her), so the operating
point sits in the middle of the measured gap.
"""
import sys, wave
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))
from lyra_core.speaker import SpeakerGate, VOICEPRINT_PATH, THRESHOLD_PATH

CLIPS = Path.home() / "Documents/LyraOS/modules/wakeword/data/clips"
MAX_ENROLL = 150
MAX_HELDOUT = 150
MAX_NEG = 346
MAX_MAYBE = 100


def read_wav(path: Path) -> bytes | None:
    try:
        with wave.open(str(path), "rb") as w:
            if (w.getframerate(), w.getsampwidth(), w.getnchannels()) != (16000, 2, 1):
                return None
            return w.readframes(w.getnframes())
    except Exception:
        return None


def embed_all(gate, paths):
    embs, used = [], 0
    for p in paths:
        pcm = read_wav(p)
        if pcm is None:
            continue
        e = gate.embed(pcm)
        if e is not None:
            embs.append(e); used += 1
    return embs, used


def main():
    gate = SpeakerGate()
    pos = sorted((CLIPS / "positives").glob("*.wav"))
    enroll_paths = pos[0::2][:MAX_ENROLL]
    heldout_paths = pos[1::2][:MAX_HELDOUT]
    neg_paths = sorted((CLIPS / "negatives").glob("*.wav"))[:MAX_NEG]
    maybe_paths = sorted((CLIPS / "wake_maybe").glob("*.wav"))[:MAX_MAYBE]
    enroll, n_e = embed_all(gate, enroll_paths)
    vp = np.mean(enroll, axis=0); vp = (vp / np.linalg.norm(vp)).astype(np.float32)
    held, n_h = embed_all(gate, heldout_paths)
    neg, n_n = embed_all(gate, neg_paths)
    maybe, n_m = embed_all(gate, maybe_paths)
    genuine = np.array([float(e @ vp) for e in held])
    impostor = np.array([float(e @ vp) for e in neg])
    maybe_s = np.array([float(e @ vp) for e in maybe])
    def stats(name, a):
        if len(a) == 0: print(f"{name}: none"); return
        print(f"{name}: n={len(a)} min={a.min():.3f} p1={np.percentile(a,1):.3f} "
              f"p50={np.percentile(a,50):.3f} p99={np.percentile(a,99):.3f} max={a.max():.3f}")
    print(f"enrolled on {n_e} clips; heldout {n_h}; negatives {n_n}; maybe {n_m}")
    stats("genuine (held-out Daniel)", genuine)
    stats("impostor (negatives)", impostor)
    stats("borderline (wake_maybe)", maybe_s)
    g1 = float(np.percentile(genuine, 1)); i99 = float(np.percentile(impostor, 99))
    thr = min(0.55, max(0.25, (g1 + i99) / 2.0))
    print(f"threshold: genuine p1={g1:.3f}, impostor p99={i99:.3f} -> {thr:.3f}")
    print(f"at threshold: genuine accepted {float((genuine >= thr).mean()):.3f}, "
          f"impostor accepted {float((impostor >= thr).mean()):.3f}")
    np.save(VOICEPRINT_PATH, vp)
    THRESHOLD_PATH.write_text(f"{thr:.3f}\n")
    print("wrote", VOICEPRINT_PATH, "and", THRESHOLD_PATH)


if __name__ == "__main__":
    main()
