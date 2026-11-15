# Module: Wakeword ("hey Lyra")

The custom wake-word module: trained gate models, the gate's feature
pipeline, the field-clip datasets, and the training pipeline. The bridge
consumes this module; it no longer keeps copies inside the project.

## Layout
- `models/` — trained gate models. `hey_lyra_v51.onnx` is the live model
  (loaded by the bridge's `wake_gate.py`); `hey_lyra_v4.onnx` is the kept
  fallback. (`.onnx.data` files are part of each model — keep together.)
- `data/wake/` — gate feature models: `melspectrogram.onnx`,
  `embedding_model.onnx` (openWakeWord-style front end used by the gate).
- `data/clips/` — field clip pools (see the contract below).
- `training/` — `train.py`, `training_config*.yaml`, `background/` noise,
  `output/` staged runs, `curation/` scoring/curation tooling, and `venv/`
  — the module venv, which is also the Lyra Box bridge's runtime venv
  (the bridge's `start.sh` points at `training/venv/bin/python`).

## Clip-save contract (bridge -> this module)
The bridge files every wake clip here as it runs:
- verify pass -> `data/clips/positives/`
- verify fail -> `data/clips/negatives/` (inbox; curated before retrains)
- verify maybe -> `data/clips/wake_maybe/` (human review)
- Filenames carry the gate score; every save appends one JSON line to
  `data/clips/clips_ledger.jsonl` (verdict, score, transcript, path).
- `data/clips/curated_negatives_v5/` = verified hard negatives for
  retrains. `data/clips/suspect_positives/` = quarantined clips that
  must never be trained as negatives.

## Consumers
- Lyra Box bridge: `bridge/wake_gate.py` (gate model + feature models),
  `bridge/bridge_pipeline.py` (clip routing above).

Moved here 2026-10-22 (modules reorganization; previously
`Lyra Box/wakeword/` + `Lyra Box/bridge/models/` + `Lyra Box/bridge/data/`).
