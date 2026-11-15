# openWakeWord precomputed features (external, downloaded 2026-11-01)

Source: Hugging Face dataset `davidscripka/openwakeword_features`
(the openWakeWord author's official negative-data release).
Daniel's order: this set only — no AudioSet, no MUSAN.

- `openwakeword_features_ACAV100M_2000_hrs_16bit.npy` (17.3 GB)
  ACAV100M features: shape (5625000, 16, 96) float16 — ~2000 hrs of
  diverse real-world speech/music/noise as 1.28 s windows.
  Intended use: general-purpose NEGATIVE features for a future
  hey_lyra run, sampled to the positives budget (standing rule:
  positives set the budget for negatives) — never dumped in raw.
- `validation_set_features.npy` (185 MB)
  False-positive validation features: shape (481345, 96) float32,
  ~11 hrs (DiPCo + Santa Barbara Corpus + reverberated MUSDB).
  NOTE: frame-level (N, 96); our pipeline's existing fp_validation
  file (v3) is a much smaller processed subset — check windowing
  before swapping it in as false_positive_validation_data_path.
- `SHA256SUMS.txt` — hashes at download time.

Status: downloaded + verified only. NOT staged into any training
run; v52 remains the live model and no retrain is ordered.
