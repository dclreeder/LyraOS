"""Shared turn-text helpers for the core's router.

This module used to BE the router: the caged LLM classifier
(intent enum, JSON-schema decision, slot validators, canned
confirmations) lived here until 2026-11-11, when Daniel ordered
the cage removed -- "All I want is the voice pipeline, the model
itself, and its tools." The classifier (Router, LlmClient,
INTENTS, the parse_* validators) is deleted, preserved in
router.py.bak-20261111-cageremoval. What remains is only what the
free router (lyra_core.free_router) still shares: the spoken
fallback lines, the LLM transport error type, and the D5 garble
hygiene (normalize / looks_garbled) that runs before the model.
"""
from __future__ import annotations

import re


DIDNT_CATCH = "Sorry, I didn't catch that."
NOT_UNDERSTOOD = "I don't understand."

_FILLERS = {"um", "uh", "uhm", "hmm", "mm", "mhm", "er", "ah", "oh"}


def normalize(text: str) -> str:
    """Light input hygiene for the garbled check: lowercase, '%'
    spelled out, sentence punctuation stripped (apostrophes and the
    time colon survive), whitespace collapsed, a leading/trailing
    'please' dropped. The model sees the raw command; only the D5
    garbled test uses this view."""
    t = (text or "").lower().replace("\u2019", "'").replace("%", " percent ")
    t = t.replace("-", " ")
    t = re.sub(r"[.,!?;:\"]", lambda m: ":" if m.group(0) == ":" else " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if t.startswith("please "):
        t = t[len("please "):]
    if t.endswith(" please"):
        t = t[:-len(" please")]
    return t.strip()


def looks_garbled(normalized: str) -> bool:
    """D5's trigger: no parseable speech at all -- empty, symbols
    only, or nothing but filler sounds. Real words (even unroutable
    ones) are NOT garbled; they go to the model and, failing that,
    get the no-match line."""
    tokens = [t for t in normalized.split() if any(c.isalpha() for c in t)]
    if not tokens:
        return True
    return all(t in _FILLERS for t in tokens)


class LlmError(RuntimeError):
    """The LLM could not produce a usable decision (transport,
    timeout, HTTP status, unparseable body). The router degrades
    these to the no-match line -- loudly logged, never a guess."""
