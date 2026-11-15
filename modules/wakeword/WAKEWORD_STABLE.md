# WAKEWORD STABLE V1 - declared 2026-11-01 (Daniel)

The current wakeword configuration is Wakeword Stable V1. There is one
live model and one training-data estate; everything else was removed
2026-11-01 at Daniel's order ("clean everything only keep our
positives and feature set").

## The model
- hey_lyra_stable_v1 - modules/wakeword/models/hey_lyra_stable_v1.onnx
  (sha256 ea330767...) + .onnx.data. The ONLY live model in models/.
- CURRENT WEIGHTS = the 2026-11-11 retrain (Daniel's order, swapped in
  live 2026-11-11 ~23:10): trained on fresh features from 257 real
  positives (218 train / 39 holdout) + the downloaded openWakeWord
  features negatives (ACAV100M) + 30 of Daniel's real negatives
  folded in (9 held out) -- the first run to train on his real
  negatives. Eval vs the original weights: holdout positives fired
  37/39 vs 35/39; held-out real negatives 0 fires (max 0.014) vs
  2 fires (max 0.954); the quarantined TV mislabel dropped from
  0.987 (fired) to 0.718. Provenance: training/output/
  hey_lyra_candidate_20261111/.
- The ORIGINAL Stable V1 weights (sha256 30ecf668..., trained per
  Daniel's recipe: his real positives + ACAV100M negatives only) are
  preserved as the fallback in models/fallback_stable_v1_orig/
  (and mirrored in wyoming-services/models/). Rollback = copy the
  pair back + restart lyra-wyoming-wake.
- Eval vs v52 (run-dir summaries): 733-clip negative pool - v52 fired
  >=0.8 on 654, v53 fires on 6 (mean 0.015); 7-clip sleep holdout -
  v53 max 0.0002, mean ~0.0. One known miss signature: the quiet 7 AM
  alarm-stop clip class (v53 scores it ~0.0002).
- Runs in: Local Satellite (lyra-localsat.service), whose
  fedora/wake_gate.py WAKE_MODEL_PATH points at this file. Rolled out
  2026-11-01 ~11:20; the satgadget-era wake_gate still points at the
  (now deleted) v52 - that service is disabled rollback only.
- Training config kept: training/training_config_v53.yaml (all older
version configs deleted 2026-11-01). Provenance kept: training/output/hey_lyra_stable_v1/ (staging +
  features + eval summaries) and the loose stable_v1 artifacts beside it.

## The data estate (all that remains)
- data/clips/positives/ - his positive clips (135 at declaration) +
  data/clips/enrollment_positives/ (2). New verified wakes keep
  filing here.
- training/external/openwakeword_features/ - the downloaded features
  set (17 GB, HF davidscripka/openwakeword_features). KEPT at
  Daniel's explicit word (he stopped a first cleanup pass over this
  set, then confirmed: positives + feature set stay).
- data/clips/negatives/ - EMPTY at declaration; this is where newly
  generated failed clips land. data/clips/wake_maybe/ likewise empty.
- Small quarantine folders kept: missed triggers/ (miner catches,
  adjudication pending), suspect_positives/ (incl. the proven TV-
  narration mislabel - never train as a negative), clips_ledger.jsonl.

## Removed 2026-11-01
Models v4/v51/v52; training run dirs + loose artifacts for
v2/v3/v4/v5/v51/v52/v53b; clips/curated_negatives_v5/ (155 files).
There are NO fallback models left - by design. A future retrain
starts from the positives + feature set above.
