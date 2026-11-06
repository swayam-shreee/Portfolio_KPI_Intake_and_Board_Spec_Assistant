"""Interface contract for the KPI pipeline. Types and constants only, no logic.

``pipeline.run.run(paths)`` takes raw submission file paths and returns a
``RunResult``. Conventions:

* Money metrics are in $K. ``gross_margin_pct`` is in percent (78.4 = 78.4%).
  ``contraction_arr`` and ``churned_arr`` are positive numbers.
* ``month`` is ``YYYY-MM``.
* ``source_file`` is the basename of the input file.
* ``source_cell`` is an A1-style reference. For xlsx it includes the sheet name,
  quoted, e.g. ``'Q2 2026'!C5``. For CSV the header is row 1, e.g. ``C3``.
  A derived value (e.g. beginning ARR taken from the prior month's ending ARR)
  cites the cell it was taken from.
* Values under review are left out of the table, never guessed.
* An unknown label is flagged with ``metric=None`` and ``suggestion=<metric>``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd

METRICS = (
    "beginning_arr", "new_arr", "expansion_arr", "contraction_arr", "churned_arr",
    "ending_arr", "revenue", "gross_margin_pct", "customers", "headcount",
)

ErrorType = Literal[
    "missing_month", "rollforward_mismatch", "prompt_injection",
    "duplicate_submission", "continuity_break", "impossible_value", "unknown_label",
]
ERROR_TYPES = (
    "missing_month", "rollforward_mismatch", "prompt_injection",
    "duplicate_submission", "continuity_break", "impossible_value", "unknown_label",
)

COMPANIES = (
    "Ridgeline Field Software", "Harborview Compliance", "Northpass Health IT",
    "Cobalt Facilities", "Verity Legal Tech",
)

TABLE_COLUMNS = ("company", "month", "metric", "value_usd_k", "source_file", "source_cell")


@dataclass(frozen=True)
class ReviewItem:
    company: str
    month: Optional[str]
    metric: Optional[str]
    error_type: ErrorType
    source_file: str
    source_cell: str
    reason: str
    suggestion: Optional[str] = None


@dataclass
class RunResult:
    table: pd.DataFrame  # long format, columns = TABLE_COLUMNS
    review_queue: list[ReviewItem] = field(default_factory=list)
