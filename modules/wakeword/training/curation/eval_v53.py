#!/usr/bin/env python3
"""Test hey_lyra_v53_oww against OUR negatives (Daniel, 2026-11-01).

v53 was trained on our positives + openWakeWord's ACAV negatives ONLY;
none of our negative clips were trained on or used for validation.
This scores v53 vs the live v52, stream-faithful (same method as
eval_v52_faithful: 1.92 s silence left-pad, 2 s windows, 0.08 s step,
max score; threshold 0.80), on:
  neg_pool      data/clips/negatives (production false alarms incl. TV)
  neg_test_v52  v52's staged negative_test clips
  holdout_neg   v52 holdout sleep negatives (7)
  holdout_pos   v52 holdout positives (11, incl. quarantined mislabel)
  pos_test_v52  v52's staged positive_test clips
Features are computed once per window and fed to both classifiers.
"""
import glob, json, os, sys, wave
import numpy as np
sys.path.insert(0, os.path.expanduser("~/Documents/Local Satellite/fedora"))
import onnxruntime as ort
from wake_gate import HeyLyraGate

W = os.path.expanduser("~/Documents/LyraOS/modules/wakeword/training")
MODELS = {
    "v52": os.path.expanduser("~/Documents/LyraOS/modules/wakeword/models/hey_lyra_v52.onnx"),
    "v53": os.path.join(W, "output", "hey_lyra_v53_oww.onnx"),
}
SETS = {
    "neg_pool": sorted(glob.glob(os.path.expanduser("~/Documents/LyraOS/modules/wakeword/data/clips/negatives/*.wav"))),
    "neg_test_v52": sorted(glob.glob(os.path.join(W, "output", "hey_lyra_v52", "negative_test", "*.wav"))),
    "holdout_neg": sorted(glob.glob(os.path.join(W, "output", "hey_lyra_v52", "holdout", "neg", "*.wav"))),
    "holdout_pos": sorted(glob.glob(os.path.join(W, "output", "hey_lyra_v52", "holdout", "pos", "*.wav"))),
    "pos_test_v52": sorted(glob.glob(os.path.join(W, "output", "hey_lyra_v52", "positive_test", "*.wav"))),
}
THR = 0.80
opts = ort.SessionOptions(); opts.intra_op_num_threads = 6
gate = HeyLyraGate()
feats_cache = {}
wakes = {}
for k, p in MODELS.items():
    s = ort.InferenceSession(p, sess_options=opts)
    wakes[k] = (s, s.get_inputs()[0].name)

def scores_for(path):
    w = wave.open(path); sr = w.getframerate()
    d = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16); w.close()
    win = 2 * sr; step = int(0.08 * sr); pad = int(1.92 * sr)
    dp = np.concatenate([np.zeros(pad, dtype=np.int16), d])
    best = {k: 0.0 for k in MODELS}
    for s in range(0, max(1, len(dp) - win + 1), step):
        feats = gate._features(dp[s:s + win])
        for k, (sess, inp) in wakes.items():
            v = float(np.squeeze(sess.run(None, {inp: feats})[0]))
            if v > best[k]:
                best[k] = v
    return best

rows = []
summary = {}
for setname, files in SETS.items():
    vals = {k: [] for k in MODELS}
    for f in files:
        sc = scores_for(f)
        rows.append({"set": setname, "file": os.path.basename(f), **{k: round(sc[k], 4) for k in MODELS}})
        for k in MODELS:
            vals[k].append(sc[k])
    summary[setname] = {}
    for k in MODELS:
        a = np.array(vals[k]) if vals[k] else np.array([0.0])
        summary[setname][k] = {"n": int(len(a)), "fires>=0.8": int((a >= THR).sum()),
                               "max": float(a.max()), "mean": float(a.mean())}
    print(setname, json.dumps(summary[setname]), flush=True)

out_csv = os.path.join(W, "output", "hey_lyra_v53_oww", "eval_v53_results.csv")
with open(out_csv, "w") as fh:
    fh.write("set,file," + ",".join(MODELS) + "\n")
    for r in rows:
        fh.write(f"{r['set']},{r['file']},{r['v52']},{r['v53']}\n")
with open(os.path.join(W, "output", "hey_lyra_v53_oww", "eval_v53_summary.json"), "w") as fh:
    json.dump(summary, fh, indent=1)
print("WROTE", out_csv)
