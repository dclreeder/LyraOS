#!/bin/bash
# LyraOS backup: two archives in ~/backups/lyraos/.
#   code: the tree minus data/, model weights, venvs, builds, state, secrets.
#   data: data/ (GGUFs, alarm tone) PLUS the irreplaceable trained/downloaded
#         weights living inside modules/ (wake models, Kokoro, Whisper).
# Secrets are in NEITHER archive: ~/Documents/APIKeys/.env and
# core/alarm_secrets.json are backed up separately, by hand.
set -e
cd "$(dirname "$0")"
STAMP=$(date +%Y%m%d-%H%M)
OUT=~/backups/lyraos; mkdir -p "$OUT"
tar --exclude='./data' --exclude='**/venv' --exclude='**/build' --exclude='**/__pycache__' --exclude='**/jobs' --exclude='**/collector_spool' --exclude='*secrets*' --exclude='./.git' --exclude='*.gguf' --exclude='*.onnx' --exclude='*.onnx.data' --exclude='*.bin' --exclude='*.npy' -czf "$OUT/lyraos-code-$STAMP.tar.gz" .
tar -czf "$OUT/lyraos-data-$STAMP.tar.gz" data modules/wakeword/models modules/wyoming/models modules/tts/kokoro/kokoro-v1.0.onnx modules/whisper/models
ls -lh "$OUT"/lyraos-*-$STAMP.tar.gz
