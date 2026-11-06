"""Stage 4: SaaS metrics from the validated long table. Plain Python, no model calls.

Inputs are the table (values under review are already left out of it) and the review queue,
which explains why a value is missing. See ``pipeline.contract.compute_metrics`` for the output
format and the derived-vs-reported rule:

* A flagged DERIVED value (beginning_arr built from the prior month's ending ARR) counts as
  absent and is back-solved from that month's components.
* A flagged or missing REPORTED value blocks every metric that uses it.

A blocked metric has ``value=None`` and a ``blocked_reason``. Never 0, never interpolated.
Ratios are in percent; arr_per_employee is in $K.
"""
from __future__ import annotations

import pandas as pd

from pipeline.contract import METRIC_COLUMNS, ReviewItem

_Part = tuple  # (value | None, inputs: tuple[(file, cell)], blockers: tuple[str])
_COMPONENTS = ("ending_arr", "new_arr", "expansion_arr", "contraction_arr", "churned_arr")


def slug(company: str) -> str:
    return company.split()[0].lower()


def _combine(parts: list, fn) -> _Part:
    inputs = tuple(dict.fromkeys(i for p in parts for i in p[1]))
    blockers = tuple(dict.fromkeys(b for p in parts for b in p[2]))
    if blockers:
        return None, inputs, blockers
    return fn(*[p[0] for p in parts]), inputs, ()


class _Calc:
    def __init__(self, table: pd.DataFrame, queue: list[ReviewItem]):
        self.queue = [q for q in queue if q.error_type != "prompt_injection"]
        self.cells = {(r.company, r.month, r.metric): (float(r.value_usd_k), (r.source_file, r.source_cell))
                      for r in table.itertuples()}
        self.months = sorted(table["month"].unique())          # the reporting period
        self.companies = list(dict.fromkeys(table["company"]))
        # A company's beginning_arr is DERIVED when its rows cite an ending_arr cell.
        ending = {(c, v[1]) for (c, _, k), v in self.cells.items() if k == "ending_arr"}
        self.derived = {c for (c, _, k), v in self.cells.items()
                        if k == "beginning_arr" and (c, v[1]) in ending}

    def _blocker(self, c: str, m: str, metric: str) -> str:
        for it in self.queue:
            if it.company != c or it.month not in (None, m):
                continue
            hit = it.metric == metric or it.suggestion == metric
            if it.metric is None and it.suggestion is None:
                hit = it.error_type == "missing_month" and it.month == m
            if hit:
                return (f"{c} {m} {metric} is blocked by review item {it.error_type} "
                        f"at {it.source_file} {it.source_cell}: {it.reason}")
        return f"{c} {m} {metric} is missing from the table (no review item explains it)"

    def get(self, c: str, m: str, metric: str) -> _Part:
        hit = self.cells.get((c, m, metric))
        if hit is None:
            return None, (), (self._blocker(c, m, metric),)
        return hit[0], (hit[1],), ()

    def beginning(self, c: str, m: str) -> _Part:
        if c not in self.derived or (c, m, "beginning_arr") in self.cells:
            return self.get(c, m, "beginning_arr")      # reported, or derived and present
        # derived and absent (flagged, or no usable prior month): back-solve from components
        return _combine([self.get(c, m, k) for k in _COMPONENTS],
                        lambda end, new, exp, con, chu: end - new - exp + con + chu)

    def rows(self) -> list[tuple]:
        out = []

        def put(c: str, m: str, metric: str, part: _Part) -> None:
            value, inputs, blockers = part
            reason = None
            if blockers:
                reason = "; ".join(blockers[:3]) + (f" (+{len(blockers) - 3} more)" if len(blockers) > 3 else "")
            out.append((c, m, f"{slug(c)}.{m}.{metric}", value, list(inputs), reason))

        months = self.months
        first, last = months[0], months[-1]
        n = len(months)
        for c in self.companies:
            for i, m in enumerate(months):
                if i > 0:
                    put(c, m, "mom_growth", _combine(
                        [self.get(c, m, "ending_arr"), self.get(c, months[i - 1], "ending_arr")],
                        lambda a, b: (a / b - 1) * 100))
                put(c, m, "churn_rate", _combine([self.get(c, m, "churned_arr"), self.beginning(c, m)],
                                                 lambda ch, b: ch / b * 100))
                put(c, m, "gross_margin", self.get(c, m, "gross_margin_pct"))
                put(c, m, "arr_per_employee", _combine(
                    [self.get(c, m, "ending_arr"), self.get(c, m, "headcount")], lambda a, h: a / h))
            put(c, "12m", "growth_12m", _combine(
                [self.get(c, last, "ending_arr"), self.beginning(c, first)],
                lambda e, s: (e / s - 1) * 100))
            parts = [self.beginning(c, first)]
            for k in ("expansion_arr", "contraction_arr", "churned_arr"):
                parts += [self.get(c, m, k) for m in months]

            def nrr(*v, n=n):
                return (v[0] + sum(v[1:1 + n]) - sum(v[1 + n:1 + 2 * n]) - sum(v[1 + 2 * n:])) / v[0] * 100

            def grr(*v, n=n):
                return (v[0] - sum(v[1 + n:1 + 2 * n]) - sum(v[1 + 2 * n:])) / v[0] * 100

            put(c, "12m", "nrr_12m", _combine(parts, nrr))
            put(c, "12m", "grr_12m", _combine(parts, grr))
        return out


def compute(table: pd.DataFrame, review_queue: list[ReviewItem]) -> pd.DataFrame:
    df = pd.DataFrame(_Calc(table, list(review_queue)).rows(), columns=list(METRIC_COLUMNS))
    df["value"] = df["value"].astype(object).where(df["value"].notna(), None)   # keep real None
    return df.sort_values(["company", "month", "metric_id"], kind="stable").reset_index(drop=True)
