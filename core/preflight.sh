#!/bin/sh
# Pre-window checklist for the seat-swap proof. Never contacts the box.
exec env PYTHONPATH="$HOME/Documents/LyraOS/core" "$HOME/Documents/LyraOS/core/venv/bin/python" -m lyra_core --preflight "$@"
