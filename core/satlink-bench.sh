#!/bin/sh
# Stage 1 satlink bench: fake box vs the real SatBoxClient + session
# on a bench port (never 9876, never the real box).
exec env PYTHONPATH="$HOME/lyra-core" "$HOME/Documents/esphome-probe/venv/bin/python" -m lyra_core.satlink_bench "$@"
