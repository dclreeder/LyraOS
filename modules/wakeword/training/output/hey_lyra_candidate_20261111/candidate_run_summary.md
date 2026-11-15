# hey_lyra_candidate_20261111 — run summary (2026-11-11 night)

Trained per Daniel's order; NOT deployed. Live model remains Wakeword
Stable V1 (gate untouched, no service restarted).

## Recipe (mirror of training_config_v53.yaml + documented deviations)
- Positives: CURRENT pool data/clips/positives (257). Seeded split
  (seed 20261111, split.json): 218 train / 39 holdout (bench fixture
  forced into holdout). Fresh features via stock oWW augment+embed
  (background x3, 3 rounds train / 1 round holdout), total_length
  pinned to 32000 = v53's effective value (keeps (16,96) geometry).
- Negatives: ACAV100M features (symlink, v53 pattern) PLUS the fold-in:
  Daniel's real negatives 30 train (own feature file, batch share
  5/100 vs ACAV 45/100, positives 50/100) / 9 holdout.
- Trainer: train_candidate.py = curation/train_fork_oww.py (the v53
  fork) + 3 patches: total_length pin; per-key batch counts;
  torch.set_num_threads(4) — at the default 20 threads, barrier sync
  on this tiny model costs ~50 ms/step (~16 it/s); 4 threads gives
  ~1360 it/s. (Two runs at 16 it/s were stopped before this fix.)
  TFLite conversion failure tolerated (onnx_tf broken; not needed).
- In-training FP validation: oWW validation stream frames[::2]
  (240,673 frames ≈ 5.3 h) against the stock 11.3 h divisor — the
  trainer's internal FP/hr is optimistic ~2x; the clip eval below
  is the evidence, not the trainer's internal numbers (INFO logs
  aren't captured by the trainer's logging setup anyway).
- Wall clock: prep ~10 s; training ~3 min (22:57:10 -> ~23:00).

## Eval (eval_candidate.py; production gate front end, streaming
## 1280-sample chunks, 1.92 s silence pre-roll, max score, thr 0.8)
Protocol validated against the known reference: bench fixture under
Stable V1 first-crosses at 0.9216 (expected 0.922).

| set | n | V1 fires | V1 max/mean | Cand fires | Cand max/mean |
|---|---|---|---|---|---|
| holdout positives | 39 | 35 | 0.999/0.927 | 37 | 0.999/0.946 |
| holdout real negatives | 9 | 2 | 0.954/0.301 | 0 | 0.014/0.005 |
| suspect_positives | 0 | - | dir empty | - | - |
| train negatives (sanity) | 3 | 3 | 0.995/0.981 | 1 | 0.995/0.348 |
| v52 holdout pos | 11 | 9 | 0.999/0.816 | 8 | 0.999/0.792 |
| v52 holdout neg (sleep) | 7 | 0 | 0.0004/0.0004 | 0 | 0.0004/0.0003 |
| bench fixture | 1 | fires (0.9216 first-x) | - | fires (0.9969 first-x, 0.9982 max) | - |

Notes: candidate's 2 holdout-positive misses are clips V1 misses too
(scores lower than candidate's). Zero holdout positives lost vs V1;
2 gained. v52 holdout pos delta is the quarantined TV mislabel
(sat_wake_negative_20261020_225322): V1 fires 0.987, candidate 0.718
(below threshold — correct). The known 7 AM alarm-stop clip
(wake_positive_20261031_070007) still scores ~0 for both — the
accepted Stable V1 trade is unchanged. One trained-on negative
(20261106_033304) still fires at 0.995 for the candidate.

Artifacts: training/output/hey_lyra_candidate_20261111.onnx (+ .data,
.pt); run dir has split.json, all feature files, logs, eval CSV/JSON.
