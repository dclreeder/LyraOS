#!/bin/sh
# Phase 1b bench: runs the Wyoming-loop proof under the esphome venv python
# (the interpreter the whole core targets; see README.md). Bench only —
# nothing here drives a live device or installs anything.
exec env PYTHONPATH="$HOME/Documents/LyraOS/core" "$HOME/Documents/esphome-probe/venv/bin/python" -m lyra_core.bench "$@"
