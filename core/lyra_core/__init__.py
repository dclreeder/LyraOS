"""Lyra core — the Fedora-native voice stack (Lyra homelab plan, 2026-11-11).

The session's client is the localmic transport (a PipeWire capture
of Fedora's default audio source); a Wyoming pipeline driver
(stdlib-only wire client) and an HTTP audio endpoint complete the
stack. The speaker box and its API clients were removed 2026-11-16.
"""

__version__ = "0.1.0"
