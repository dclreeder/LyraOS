#!/bin/sh
# Phase 2 bench: verify-then-fire vs a MOCK box, gate in DRY-RUN.
# Never contacts the real speaker box; writes nothing to the pool.
exec env PYTHONPATH="$HOME/lyra-core" "$HOME/Documents/esphome-probe/venv/bin/python" -m lyra_core.phase2_bench "$@"
