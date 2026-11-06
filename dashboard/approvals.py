"""Append-only approval log. It stores hashes and counts only, never paragraph, question or reason text."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def record(cfg, action: str, draft, p, model: str, fingerprint: str) -> dict:
    payload = ([[s.company, [pa.rendered for pa in s.paragraphs]] for s in draft.sections]
               + [q.text for q in draft.questions])
    line = {"ts": datetime.now(timezone.utc).isoformat(), "action": action,
            "draft_sha256": hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
            "draft_status": draft.status, "model": model, "fingerprint": fingerprint,
            "open_question_count": len(draft.questions), "decision_count": len(p.decisions)}
    state = Path(cfg.state_dir)
    state.mkdir(parents=True, exist_ok=True)
    with open(state / "approvals.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(line) + "\n")
    return line
