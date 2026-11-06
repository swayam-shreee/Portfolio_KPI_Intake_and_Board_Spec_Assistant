"""The real Claude client. Importing this module builds nothing; the client is created on demand.

Reads ANTHROPIC_API_KEY and CLAUDE_MODEL (default claude-sonnet-5-5) from the process environment,
falling back to a `.env` file in the repo root. The real environment wins over `.env`.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_DOTENV = Path(__file__).resolve().parents[1] / ".env"


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse simple KEY=VALUE lines. Missing file -> {}. Never touches os.environ."""
    try:
        text = Path(path).read_text()
    except OSError:
        return {}
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


class AnthropicClient:
    def __init__(self) -> None:
        import anthropic
        env = _read_dotenv(_DOTENV)
        key = os.environ.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_API_KEY")
        self.model = os.environ.get("CLAUDE_MODEL") or env.get("CLAUDE_MODEL") or "claude-sonnet-5-5"
        self._client = anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Add it to .env in the project root "
                               "or export it before running streamlit.")

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
