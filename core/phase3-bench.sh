#!/bin/sh
# Phase 3 bench: LLM router (scripted stub LLM) + tools against mocks/copies + the one
# allowed real light round trip. Never touches the production
# service, the production alarm state, or the speaker box.
exec env PYTHONPATH="$HOME/lyra-core" "$HOME/Documents/esphome-probe/venv/bin/python" -m lyra_core.phase3_bench "$@"
