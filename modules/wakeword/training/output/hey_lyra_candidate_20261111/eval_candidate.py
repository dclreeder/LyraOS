#!/usr/bin/env python3
"""Head-to-head eval: Wakeword Stable V1 vs hey_lyra_candidate_20261111.

Same protocol for both models: the production HeyLyraGate front end
(streaming, 1280-sample chunks, threshold 0.8), clip preceded by 1.92 s of
silence (eval_v53-style pre-roll), clip score = max gate score over the
stream. Features/front end are shared; only the classifier ONNX differs.
"""
import glob, json, os, sys, wave
import numpy as np
sys.path.insert(0, os.path.expanduser("~/Documents/LyraOS/modules/wyoming"))
from wake_gate import HeyLyraGate

W = os.path.expanduser("~/Documents/LyraOS/modules/wakeword")
RUN = os.path.join(W, "training", "output", "hey_lyra_candidate_20261111")
MODELS = {
    "stable_v1": os.path.join(W, "models", "hey_lyra_stable_v1.onnx"),
    "candidate": os.path.join(W, "training", "output", "hey_lyra_candidate_20261111.onnx"),
}
THR = 0.8
PAD = np.zeros(int(1.92 * 16000), dtype=np.int16)
CHUNK = 1280
split = json.load(open(os.path.join(RUN, "split.json")))
CLIPS = os.path.join(W, "data", "clips")
SETS = {
    "holdout_positives": [os.path.join(CLIPS, "positives", f) for f in split["positives_holdout"]],
    "holdout_real_negatives": [os.path.join(CLIPS, "negatives", f) for f in split["negatives_holdout"]],
    "suspect_positives": sorted(glob.glob(os.path.join(CLIPS, "suspect_positives", "*.wav"))),
    "train_negatives_sanity": [os.path.join(CLIPS, "negatives", f) for f in split["negatives_train"][:3]],
    "v52_holdout_pos": sorted(glob.glob(os.path.join(W, "training", "output", "hey_lyra_stable_v1", "holdout", "pos", "*.wav"))),
    "v52_holdout_neg": sorted(glob.glob(os.path.join(W, "training", "output", "hey_lyra_stable_v1", "holdout", "neg", "*.wav"))),
}
BENCH = os.path.join(CLIPS, "positives", "wake_positive_20261111_061439_0.987.wav")

gates = {k: HeyLyraGate(p) for k, p in MODELS.items()}

def read_wav(path):
    w = wave.open(path)
    d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    w.close()
    return d

def score(gate, audio, pad=True):
    gate.reset()
    a = np.concatenate([PAD, audio]) if pad else audio
    best = 0.0
    first_cross = None
    for i in range(0, len(a), CHUNK):
        s = float(gate.predict(a[i:i + CHUNK]))
        if s > best:
            best = s
        if first_cross is None and s >= THR:
            first_cross = s
    return best, first_cross

rows = []
summary = {}
for setname, files in SETS.items():
    vals = {k: [] for k in MODELS}
    for f in files:
        d = read_wav(f)
        r = {"set": setname, "file": os.path.basename(f)}
        for k, g in gates.items():
            b, _ = score(g, d)
            r[k] = round(b, 4)
            vals[k].append(b)
        rows.append(r)
    summary[setname] = {}
    for k in MODELS:
        a = np.array(vals[k]) if vals[k] else np.array([])
        summary[setname][k] = {"n": int(len(a)),
                               "fires>=0.8": int((a >= THR).sum()) if len(a) else 0,
                               "max": float(a.max()) if len(a) else None,
                               "mean": float(a.mean()) if len(a) else None}
    print(setname, json.dumps(summary[setname]), flush=True)

bench = {}
d = read_wav(BENCH)
for k, g in gates.items():
    b, fc = score(g, d, pad=True)
    b2, fc2 = score(g, d, pad=False)
    bench[k] = {"padded_max": round(b, 4), "padded_first_cross": (round(fc, 4) if fc else None),
                "raw_max": round(b2, 4), "raw_first_cross": (round(fc2, 4) if fc2 else None)}
summary["bench_fixture"] = bench
print("bench_fixture", json.dumps(bench), flush=True)

with open(os.path.join(RUN, "eval_candidate_results.csv"), "w") as fh:
    fh.write("set,file," + ",".join(MODELS) + "\n")
    for r in rows:
        fh.write(f"{r['set']},{r['file']},{r['stable_v1']},{r['candidate']}\n")
with open(os.path.join(RUN, "eval_candidate_summary.json"), "w") as fh:
    json.dump(summary, fh, indent=1)
print("WROTE eval files", flush=True)
