#!/usr/bin/env python3
"""Stage the v53 openWakeWord-negatives fork (Daniel, 2026-11-01).

Fork definition:
- Positives: EXACTLY v52's — same staged clips and the same computed
  positive feature files (symlinked), so the ONLY variable vs v52 is
  the negative class.
- Negatives: ALL of openWakeWord's ACAV100M precomputed features
  (external/openwakeword_features/...ACAV100M...npy, symlinked as
  negative_features_train.npy). NONE of our negative clips/features
  are used in training or in the in-training validation.
- In-training negative validation: windows sampled from openWakeWord's
  own false-positive validation stream (their data, not ours).
- Our negatives are reserved untouched as the post-training test set.
"""
import os, shutil, sys
import numpy as np

W = os.path.expanduser("~/Documents/LyraOS/modules/wakeword/training")
SRC = os.path.join(W, "output", "hey_lyra_v52")
DST = os.path.join(W, "output", "hey_lyra_v53_oww")
ACAV = os.path.join(W, "external", "openwakeword_features",
                    "openwakeword_features_ACAV100M_2000_hrs_16bit.npy")
VAL = os.path.join(W, "external", "openwakeword_features",
                   "validation_set_features.npy")

os.makedirs(DST, exist_ok=True)
# positive clips (copied; only used for total_length sampling)
for sub in ("positive_train", "positive_test"):
    d = os.path.join(DST, sub)
    if os.path.exists(d):
        shutil.rmtree(d)
    shutil.copytree(os.path.join(SRC, sub), d)
    print(sub, len(os.listdir(d)))
# negative clip dirs intentionally EMPTY
for sub in ("negative_train", "negative_test"):
    os.makedirs(os.path.join(DST, sub), exist_ok=True)
# positive features: symlink v52's computed files
for f in ("positive_features_train.npy", "positive_features_test.npy"):
    dst = os.path.join(DST, f)
    if os.path.lexists(dst):
        os.unlink(dst)
    os.symlink(os.path.join(SRC, f), dst)
    print("linked", f)
# negative train features: symlink the full ACAV set
dst = os.path.join(DST, "negative_features_train.npy")
if os.path.lexists(dst):
    os.unlink(dst)
os.symlink(ACAV, dst)
print("linked negative_features_train.npy -> ACAV100M")
# negative test features: 3000 windows sampled evenly from their
# validation stream (481345 frames, 16-frame windows)
frames = np.load(VAL)  # (N, 96) float32
n_win = 3000
starts = np.linspace(0, frames.shape[0] - 16, n_win).astype(int)
wins = np.stack([frames[s:s + 16] for s in starts]).astype(np.float32)
out = os.path.join(DST, "negative_features_test.npy")
if os.path.lexists(out):
    os.unlink(out)
np.save(out, wins)
print("negative_features_test.npy", wins.shape, wins.dtype)
# holdout copied for post-training comparison (eval only, never trained)
hd = os.path.join(DST, "holdout")
if os.path.exists(hd):
    shutil.rmtree(hd)
shutil.copytree(os.path.join(SRC, "holdout"), hd)
print("holdout copied:", {s: len(os.listdir(os.path.join(hd, s))) for s in ("pos", "neg")})
