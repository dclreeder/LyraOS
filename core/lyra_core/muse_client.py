"""Muse client for the free router -- the Muse side of the
brain switch (see lyra_core.brain).

Where FreeChatClient calls a chat-completions endpoint, this
client calls Muse through the Muse Gadget SDK's send-user-msg,
into the dedicated "Lyra - Muse brain" side chat. It keeps
FreeChatClient's exact interface (chat / chat_stream returning
an OpenAI-shaped message dict), so the router's turn loop cannot
tell the difference. Three deliberate consequences of the swap:

* Only the newest user turn crosses the seam. The side chat
  holds the conversation history itself, so the router's local
  history copy and its SOUL brief stay on this side (the side
  chat carries her standing instructions, seeded when it was
  created). The router's per-turn time stamp rides along inside
  the user text, exactly as it did for the local model.
* No tool_calls are ever returned. Muse has its own hands --
  HomeLink -- and acts inside its agent turn when a command
  needs the house; the core's tool layer simply never fires
  from the model side on this brain. The deterministic
  machinery (the alarm clock, the modes) never went through
  the model in the first place and is unaffected.
* A Muse turn is a whole agent turn, not a token stream, so
  chat_stream delivers the finished reply as a single piece;
  the router's sentence splitter still chunks it for TTS, but
  speech starts when the reply lands, not before.
"""
from __future__ import annotations

import asyncio
import subprocess

from .router import LlmError

MUSEGADGET = "/usr/local/sbin/musegadget"
# The dedicated side chat titled "Lyra - Muse brain" in
# Daniel's Muse app. Her Muse-brain turns live there, never
# in his main chat.
SESSION_ID = "29bf7a9a-a231-4b21-b938-21e4e343da44"


def _last_user_text(messages: list) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content") or ""
            if isinstance(content, str) and content.strip():
                return content
    raise LlmError("no user turn in the router's messages")


class MuseClient:
    """FreeChatClient-shaped client whose model is Muse."""

    def __init__(self, session_id: str = SESSION_ID,
                 musegadget: str = MUSEGADGET) -> None:
        self._session_id = session_id
        self._musegadget = musegadget

    def _ask(self, text: str, timeout_s: float) -> str:
        wait = max(5, min(600, int(timeout_s)))
        proc = subprocess.run(
            [self._musegadget, "send-user-msg",
             "--session-id", self._session_id,
             "--wait", str(wait),
             text],
            capture_output=True, text=True, timeout=wait + 30)
        if proc.returncode != 0:
            raise LlmError(
                "muse turn failed: "
                f"{proc.stderr.strip() or proc.stdout.strip()}")
        return proc.stdout.strip()

    async def chat(self, messages: list, tools, timeout_s: float) -> dict:
        reply = await asyncio.to_thread(
            self._ask, _last_user_text(messages), timeout_s)
        if not reply:
            raise LlmError("muse returned an empty reply")
        return {"role": "assistant", "content": reply}

    async def chat_stream(self, messages: list, tools, timeout_s: float,
                          on_piece) -> dict:
        message = await self.chat(messages, tools, timeout_s)
        if on_piece is not None and message["content"]:
            await on_piece(message["content"])
        return message
