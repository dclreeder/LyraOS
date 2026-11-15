# hey_lyra_v53_oww — fork run summary (2026-11-01)

Fork (Daniel's order): positives = v52's exactly (clips + computed
feature files symlinked); negatives = ALL openWakeWord ACAV100M
precomputed features (5,625,000 windows, symlinked as
negative_features_train.npy); NONE of our negatives trained on or
used in in-training validation (negative validation = 3,000 windows
sampled from openWakeWord's own FP validation stream). Trainer:
curation/train_fork_oww.py (train.py + float32 cast for the float16
ACAV features). Config: training_config_v53.yaml (v52 recipe,
100k steps). Staging: curation/stage_v53_oww.py.

## Post-training test — OUR negatives (held out, stream-faithful)
eval: curation/eval_v53.py; full scores: eval_v53_results.csv

| set | n | v52 fires>=0.8 | v53 fires>=0.8 | v52 mean | v53 mean |
|---|---|---|---|---|---|
| neg_pool (production) | 733 | 654 | 6 | 0.891 | 0.015 |
| neg_test_v52 | 281 | 174 | 76 | 0.692 | 0.297 |
| holdout_neg (sleep) | 7 | 0 | 0 | 0.144 | 0.0002 |
| holdout_pos | 11 | 10 | 9 | 0.910 | 0.817 |
| pos_test_v52 | 198 | 187 | 162 | 0.964 | 0.860 |

## Verdict: does NOT clear the deploy gate
Zero-positive-regression rule fails on ONE real clip:
wake_positive_20261031_070007 (the 7 AM alarm-stop "hey Lyra")
scores v52 0.999 -> v53 0.0002. The only other holdout miss is the
quarantined TV mislabel (both models ~0, expected). All other real
holdout positives: v53 >= 0.994. pos_test losses are mostly
synthetic Kokoro voices (35 of 36 misses) + that same real clip.
v53's 6 surviving neg_pool fires are all ex-0.99 v52 captures
(Oct 31 daytime / Nov 1 morning TV).
Status: NOT deployed. v52 remains live. Artifacts: output/
hey_lyra_v53_oww.onnx (+ .data, .pt) — smoke 300-step artifacts were
overwritten by the full run.

## DEPLOYED 2026-11-01 11:20 EST (Daniel's order: "implement 53 original")
Model copied to modules/wakeword/models/hey_lyra_v53_oww.onnx;
Local Satellite/fedora/wake_gate.py WAKE_MODEL_PATH repointed
(backup wake_gate.py.pre-v53); lyra-localsat restarted, satellite
reconnected in ~3 s (sat6d, vol 0.1 / alarm vol 0.2, 6 AM alarm
intact). Verified through the deployed gate host-side: real positive
0.999, the 070007 clip 0.0002 (the known v53 signature miss),
survivor TV negative 0.9985 — scores match eval exactly.
Known accepted trade (Daniel's call): the quiet 7 AM alarm-stop
rendition class may not wake the box; TV false fires should drop
~100x. v52 remains in models/ as fallback; rollback = repoint +
restart. v53b was NOT deployed.
