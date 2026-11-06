"""The real Claude client. Importing this module builds nothing; the client is created on demand.

Reads ANTHROPIC_API_KEY and CLAUDE_MODEL (default claude-sonnet-5-5).
"""
from __future__ import annotations

import os
import sys


class AnthropicClient:
    def __init__(self) -> None:
        import anthropic
        self.model = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
        self._client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY

    def create(self, system: str, user: str) -> str:
        msg = self._client.messages.create(
            model=self.model, max_tokens=8192, system=system,
            messages=[{"role": "user", "content": user}],
        )
        print(f"[claude] stop_reason={msg.stop_reason} "
              f"input_tokens={msg.usage.input_tokens} output_tokens={msg.usage.output_tokens}",
              file=sys.stderr)
        if msg.stop_reason == "max_tokens":
            print("[claude] WARNING: response truncated at max_tokens; output is likely incomplete.",
                  file=sys.stderr)
        return "".join(b.text for b in msg.content if b.type == "text")
