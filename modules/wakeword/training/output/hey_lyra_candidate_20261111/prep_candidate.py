#!/usr/bin/env python3
"""Candidate staging + feature computation (Daniel's retrain order, 2026-11-11).

- Seeded split: positives 257 -> 218 train / 39 holdout (bench fixture forced
  into holdout); negatives 39 -> 30 train / 9 holdout. Recorded in split.json.
- Features via the stock oWW augment+embed path, total_length=32000 (v53's
  effective value), augmentation rounds x3 for train sets, x1 for holdouts.
- negative_features_train.npy = symlink to the ACAV100M set (v53 pattern);
  real negatives get their own feature file (the fold-in garnish).
- negative_features_test.npy = 3000 windows from oWW's validation stream
  (v53 pattern) + the 9 held-out real negatives' feature windows.
- fp_validation_stream.npy = oWW validation stream frames[::2] for the
  trainer's false-positive validation loader.
"""
import json, os, random, shutil, sys
import numpy as np
sys.path.insert(0, os.path.expanduser("~/Documents/LyraOS/modules/wakeword/training"))
import patch_sndfile  # noqa: F401 -- fix broken torchcodec (same as train.py line 1)

W = os.path.expanduser("~/Documents/LyraOS/modules/wakeword")
TRAIN = os.path.join(W, "training")
RUN = os.path.join(TRAIN, "output", "hey_lyra_candidate_20261111")
CLIPS = os.path.join(W, "data", "clips")
SEED = 20261111
TOTAL_LENGTH = 32000
BENCH = "wake_positive_20261111_061439_0.987.wav"

rng = random.Random(SEED)
pos = sorted(f for f in os.listdir(os.path.join(CLIPS, "positives")) if f.endswith(".wav"))
neg = sorted(f for f in os.listdir(os.path.join(CLIPS, "negatives")) if f.endswith(".wav"))
assert len(pos) == 257 and len(neg) == 39, (len(pos), len(neg))
assert BENCH in pos
pos_rest = [f for f in pos if f != BENCH]
rng.shuffle(pos_rest)
pos_hold = sorted([BENCH] + pos_rest[:38])
pos_train = sorted(pos_rest[38:])
rng.shuffle(neg)
neg_hold = sorted(neg[:9])
neg_train = sorted(neg[9:])
assert len(pos_train) == 218 and len(pos_hold) == 39 and len(neg_train) == 30 and len(neg_hold) == 9
with open(os.path.join(RUN, "split.json"), "w") as fh:
    json.dump({"seed": SEED, "positives_train": pos_train, "positives_holdout": pos_hold,
               "negatives_train": neg_train, "negatives_holdout": neg_hold}, fh, indent=1)
print("split recorded", flush=True)

for sub, files, src in (("positive_train", pos_train, "positives"), ("positive_test", pos_hold, "positives"),
                        ("negative_train", neg_train, "negatives"), ("negative_test", neg_hold, "negatives")):
    d = os.path.join(RUN, sub)
    if os.path.exists(d):
        shutil.rmtree(d)
    os.makedirs(d)
    for f in files:
        shutil.copy2(os.path.join(CLIPS, src, f), os.path.join(d, f))
    print("staged", sub, len(files), flush=True)

from openwakeword.data import augment_clips
from openwakeword.utils import compute_features_from_generator

bg = []
for i in os.scandir(os.path.join(TRAIN, "background")):
    bg.extend([i.path] * 3)

def feats(subdir, rounds, out):
    clips = sorted(os.path.join(RUN, subdir, f) for f in os.listdir(os.path.join(RUN, subdir)) if f.endswith(".wav"))
    gen = augment_clips(clips * rounds, total_length=TOTAL_LENGTH, batch_size=50,
                        background_clip_paths=bg, RIR_paths=[])
    compute_features_from_generator(gen, n_total=len(clips) * rounds, clip_duration=TOTAL_LENGTH,
                                    output_file=out, device="cpu", ncpu=10)
    a = np.load(out)
    print("features", os.path.basename(out), a.shape, a.dtype, flush=True)

feats("positive_train", 3, os.path.join(RUN, "positive_features_train.npy"))
feats("positive_test", 1, os.path.join(RUN, "positive_features_test.npy"))
feats("negative_train", 3, os.path.join(RUN, "real_negative_features_train.npy"))
feats("negative_test", 1, os.path.join(RUN, "heldout_negative_features.npy"))

VAL = os.path.join(TRAIN, "external", "openwakeword_features", "validation_set_features.npy")
frames = np.load(VAL)
starts = np.linspace(0, frames.shape[0] - 16, 3000).astype(int)
wins = np.stack([frames[s:s + 16] for s in starts]).astype(np.float32)
hn = np.load(os.path.join(RUN, "heldout_negative_features.npy")).astype(np.float32)
nt = np.concatenate([wins, hn], axis=0)
np.save(os.path.join(RUN, "negative_features_test.npy"), nt)
print("negative_features_test", nt.shape, flush=True)

fp = frames[::2].astype(np.float32)
np.save(os.path.join(RUN, "fp_validation_stream.npy"), fp)
print("fp_validation_stream", fp.shape, flush=True)

dst = os.path.join(RUN, "negative_features_train.npy")
if os.path.lexists(dst):
    os.unlink(dst)
os.symlink(os.path.join(TRAIN, "external", "openwakeword_features",
                        "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"), dst)
print("linked negative_features_train.npy -> ACAV100M", flush=True)
print("DONE PREP", flush=True)
