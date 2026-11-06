"""Interface contract for the KPI pipeline. Types and constants only, no logic.

``pipeline.run.run(paths)`` takes raw submission file paths and returns a
``RunResult``. ``compute_metrics`` (stage 4) is the metrics contract. Conventions:

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

METRIC_NAMES = (
    "mom_growth", "growth_12m", "nrr_12m", "grr_12m", "churn_rate", "gross_margin",
    "arr_per_employee",
)
METRIC_COLUMNS = ("company", "month", "metric_id", "value", "inputs", "blocked_reason")


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


def compute_metrics(table: pd.DataFrame, review_queue: list[ReviewItem]) -> pd.DataFrame:
    """Contract for stage 4 (not implemented yet): validated table + review queue -> metrics.

    Returns a DataFrame with columns ``METRIC_COLUMNS``: company, month (``YYYY-MM`` or
    ``"12m"``), metric_id, value, inputs, blocked_reason.

    * ``metric_id`` = ``"<slug>.<YYYY-MM|12m>.<metric>"``; slug is the first word of the company
      name, lowercased (``harborview.2026-06.churn_rate``). Metric names: ``METRIC_NAMES``.
    * Ratio metrics are in percent (5.2 = 5.2%); ``arr_per_employee`` is in $K.
    * One row per id: churn_rate, gross_margin, arr_per_employee for each of the 12 months;
      mom_growth for every month except the first; growth_12m, nrr_12m, grr_12m once ("12m").
    * ``inputs`` is a list of ``(source_file, source_cell)`` tuples of the table rows used.
      Commentary links to these.

    Formulas (agreed):
      mom_growth  = ending_arr / prior month ending_arr - 1
      growth_12m  = 2026-09 ending_arr / 2025-10 beginning_arr - 1
      nrr_12m     = (start + sum(expansion) - sum(contraction) - sum(churned)) / start
      grr_12m     = (start - sum(contraction) - sum(churned)) / start
                    where start = 2025-10 beginning_arr
      churn_rate  = churned_arr / beginning_arr (monthly)
      gross_margin = as reported; arr_per_employee = ending_arr / headcount
    Where beginning_arr is absent from the table (and not under review) it is back-solved from
    that month's components: ending - new - expansion + contraction + churned.

    A metric whose inputs are under review or missing returns ``value=None`` and a non-empty
    ``blocked_reason`` naming the blocking review item. Never 0, never interpolated.
    """
    raise NotImplementedError("stage 4: compute_metrics is not implemented yet")
