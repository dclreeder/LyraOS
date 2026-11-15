#!/bin/sh
# Phase 1c session bench: composed session vs a MOCK box + supervisor fakes.
# Never contacts the real speaker box; installs nothing.
exec env PYTHONPATH="$HOME/lyra-core" "$HOME/Documents/esphome-probe/venv/bin/python" -m lyra_core.session_bench "$@"
