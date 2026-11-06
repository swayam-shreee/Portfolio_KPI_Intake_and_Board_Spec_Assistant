"""Manual demo: run the pipeline on data/raw/ and draft commentary with the real Claude API.

Not part of the checker. Reads ANTHROPIC_API_KEY and CLAUDE_MODEL (default claude-sonnet-5-5).
"""
from __future__ import annotations

import glob
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pipeline.commentary import draft_commentary  # noqa: E402
from pipeline.contract import compute_metrics  # noqa: E402
from pipeline.llm import AnthropicClient  # noqa: E402
from pipeline.run import run  # noqa: E402


def main() -> None:
    paths = sorted(glob.glob(str(ROOT / "data/raw/*.xlsx")) + glob.glob(str(ROOT / "data/raw/*.csv")))
    res = run(paths)
    metrics = compute_metrics(res.table, res.review_queue)
    d = draft_commentary(metrics, res.review_queue, AnthropicClient(), ROOT / "outputs" / "commentary_log.jsonl")
    print(f"status={d.status} attempts={d.attempts} reasons={d.reject_reasons}")
    for s in d.sections:
        print(f"\n## {s.company}")
        for p in s.paragraphs:
            print(p.rendered)
    print("\n## Open questions")
    for q in d.questions:
        print("-", q.text)


if __name__ == "__main__":
    main()
