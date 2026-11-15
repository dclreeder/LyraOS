#!/bin/sh
# Pre-window checklist for the seat-swap proof. Never contacts the box.
exec env PYTHONPATH="$HOME/lyra-core" "$HOME/Documents/esphome-probe/venv/bin/python" -m lyra_core --preflight "$@"
