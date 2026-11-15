#!/bin/sh
# Phase 4 bench: core API (/tool, /announce) against a bench HTTP
# instance on :8199 with a fake announce recorder. Read-only real
# hardware checks (light/monitor state, live weather). Never touches
# the production service, its alarm state, or the speaker box.
exec env PYTHONPATH="$HOME/lyra-core" "$HOME/Documents/esphome-probe/venv/bin/python" -m lyra_core.phase4_bench "$@"
