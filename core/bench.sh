#!/bin/sh
# Phase 1b bench: runs the Wyoming-loop proof under the esphome venv python
# (the interpreter the whole core targets; see README.md). Bench only —
# nothing here connects to the speaker box or installs anything.
exec env PYTHONPATH="$HOME/lyra-core" "$HOME/Documents/esphome-probe/venv/bin/python" -m lyra_core.bench "$@"
