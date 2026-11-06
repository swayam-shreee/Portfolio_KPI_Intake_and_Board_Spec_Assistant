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

# ``derived`` is True only for beginning_arr rows that ingest.derive_beginning built from the prior
# month's ending ARR; every cell a company actually submitted is False.
TABLE_COLUMNS = ("company", "month", "metric", "value_usd_k", "source_file", "source_cell", "derived")

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
    """Contract for stage 4 (implemented in pipeline.metrics): validated table + review queue -> metrics.

    Returns a DataFrame with columns ``METRIC_COLUMNS``: company, month (``YYYY-MM`` or
    ``"12m"``), metric_id, value, inputs, blocked_reason.

    * ``metric_id`` = ``"<slug>.<YYYY-MM|12m>.<metric>"``; slug is the first word of the company
      name, lowercased (``harborview.2026-06.churn_rate``). Metric names: ``METRIC_NAMES``.
    * Ratio metrics (mom_growth, growth_12m, nrr_12m, grr_12m, churn_rate, gross_margin) are in
      percent (5.2 = 5.2%). ``arr_per_employee`` is in $K of ARR per employee (ending_arr is in
      $K, so 145.0 means $145K per employee), not percent and not whole dollars.
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

    Flagged inputs: derived vs reported.
      * A flagged DERIVED value counts as absent and is back-solved from that month's components
        (ending - new - expansion + contraction + churned). Derived means beginning_arr for files
        with no beginning row (the pipeline takes it from the prior month's ending ARR). It is
        also absent, and back-solved, when that prior ending ARR is flagged or missing, or when
        there is no prior month.
      * A flagged REPORTED value blocks every metric that uses it. Reported means any cell the
        company submitted, including a beginning_arr row and the components used in a back-solve.
        It is never back-solved, replaced or interpolated.

    A metric whose inputs are under review or missing returns ``value=None`` and a non-empty
    ``blocked_reason`` naming the blocking review item. Never 0, never interpolated.
    """
    from pipeline.metrics import compute  # deferred: pipeline.metrics imports this module

    return compute(table, review_queue)
