"""Lyra core entry point.

This module's surface is `--preflight` only -- there is
intentionally no run mode HERE. The production core is a different
module, run by lyra-core.service since the cutover on 2026-11-11:

    python -m lyra_core.production_main

It composes the session, supervisor, alarm, phrase router and core
API against the real speaker box (Home Assistant was retired the
same day). Anything but --preflight prints the usage and exits
non-zero, so an accidental bare invocation can never take the
box's seat.
"""
from __future__ import annotations

import asyncio
import sys

USAGE = (
    "usage: python -m lyra_core --preflight\n"
    "  --preflight   run the preflight checklist (never contacts the box)\n"
    "Production runs under lyra-core.service as\n"
    "python -m lyra_core.production_main; there is intentionally\n"
    "no run mode in this module."
)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["--preflight"]:
        from .preflight import run_preflight

        return asyncio.run(run_preflight())
    print(USAGE)
    return 2


if __name__ == "__main__":
    sys.exit(main())
