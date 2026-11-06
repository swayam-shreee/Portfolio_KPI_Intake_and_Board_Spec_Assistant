"""Independent expected-metric computation for the stage-4 checker.

Computed straight from the ground-truth frame and the answer key. Deliberately shares no code
with ``pipeline`` (formulas are re-implemented here from the agreed definitions).

Ratio metrics are in percent; ``arr_per_employee`` is in $K.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Planted errors whose cells the pipeline must not use. prompt_injection touches no cell, and
# duplicate_submission is resolved by newest-wins (the corrected value is kept), so neither masks.
MASKING_TYPES = ("missing_month", "rollforward_mismatch", "continuity_break",
                 "impossible_value", "unknown_label")
NO_BEGINNING_ROW = ("Harborview Compliance", "Verity Legal Tech")

Cell = tuple  # (company, month, metric)


@dataclass(frozen=True)
class Expected:
    company: str
    month: str          # YYYY-MM or "12m"
    metric: str
    accepted: tuple     # accepted values; empty when blocked
    inputs: tuple       # ground-truth cells used
    blockers: tuple     # masked cells that block it

    @property
    def blocked(self) -> bool:
        return not self.accepted


def slug(company: str) -> str:
    return company.split()[0].lower()


def masked_cells(key: list[dict]) -> set:
    """(company, month|None, metric|None) entries; None = wildcard."""
    out = set()
    for e in key:
        if e["type"] in MASKING_TYPES:
            metric = None if e.get("metric") in (None, "all") else e["metric"]
            out.add((e["company"], e.get("month"), metric))
    return out


class _Mask:
    def __init__(self, entries):
        self.entries = set(entries)

    def __call__(self, company: str, month: str, metric: str) -> bool:
        return any(c == company and m in (None, month) and k in (None, metric)
                   for c, m, k in self.entries)


def _combine(parts, fn):
    inputs = tuple(dict.fromkeys(i for p in parts for i in p[1]))
    blockers = tuple(dict.fromkeys(b for p in parts for b in p[2]))
    if blockers:
        return None, inputs, blockers
    return fn(*[p[0] for p in parts]), inputs, blockers


class _Calc:
    def __init__(self, gt: pd.DataFrame, mask: _Mask):
        self.mask = mask
        self.vals = {}
        for r in gt.to_dict("records"):
            for k, v in r.items():
                if k not in ("company", "month"):
                    self.vals[(r["company"], r["month"], k)] = float(v)
        self.months = {c: sorted(set(g["month"])) for c, g in gt.groupby("company")}

    def get(self, c, m, metric):
        if self.mask(c, m, metric):
            return None, (), ((c, m, metric),)
        return self.vals[(c, m, metric)], ((c, m, metric),), ()

    def beginning(self, c, m):
        if self.mask(c, m, "beginning_arr"):
            return None, (), ((c, m, "beginning_arr"),)
        if c not in NO_BEGINNING_ROW:
            return self.get(c, m, "beginning_arr")
        months = self.months[c]
        i = months.index(m)
        if i > 0 and not self.mask(c, months[i - 1], "ending_arr"):
            return self.get(c, months[i - 1], "ending_arr")
        # back-solve from this month's components
        parts = [self.get(c, m, k) for k in
                 ("ending_arr", "new_arr", "expansion_arr", "contraction_arr", "churned_arr")]
        return _combine(parts, lambda end, new, exp, con, chu: end - new - exp + con + chu)

    def run(self) -> dict:
        out = {}

        def put(c, m, metric, res):
            val, inputs, blockers = res
            out[f"{slug(c)}.{m}.{metric}"] = (c, m, metric, val, inputs, blockers)

        for c, months in self.months.items():
            first, last = months[0], months[-1]
            for i, m in enumerate(months):
                if i > 0:
                    put(c, m, "mom_growth", _combine(
                        [self.get(c, m, "ending_arr"), self.get(c, months[i - 1], "ending_arr")],
                        lambda a, b: (a / b - 1) * 100))
                put(c, m, "churn_rate", _combine(
                    [self.get(c, m, "churned_arr"), self.beginning(c, m)],
                    lambda ch, b: ch / b * 100))
                put(c, m, "gross_margin", self.get(c, m, "gross_margin_pct"))
                put(c, m, "arr_per_employee", _combine(
                    [self.get(c, m, "ending_arr"), self.get(c, m, "headcount")],
                    lambda a, h: a / h))
            put(c, "12m", "growth_12m", _combine(
                [self.get(c, last, "ending_arr"), self.beginning(c, first)],
                lambda e, s: (e / s - 1) * 100))
            n = len(months)
            comp = lambda k: [self.get(c, m, k) for m in months]
            parts = [self.beginning(c, first)] + comp("expansion_arr") + comp("contraction_arr") \
                + comp("churned_arr")

            def nrr(*v, n=n):
                start, exp, con, chu = v[0], sum(v[1:1 + n]), sum(v[1 + n:1 + 2 * n]), sum(v[1 + 2 * n:])
                return (start + exp - con - chu) / start * 100

            def grr(*v, n=n):
                start, con, chu = v[0], sum(v[1 + n:1 + 2 * n]), sum(v[1 + 2 * n:])
                return (start - con - chu) / start * 100

            put(c, "12m", "nrr_12m", _combine(parts, nrr))
            put(c, "12m", "grr_12m", _combine(parts, grr))
        return out


def _cobalt_variant(gt: pd.DataFrame, key: list[dict]) -> pd.DataFrame:
    """Ground truth as the file reports it after the continuity break: the delta is carried
    into ending ARR from the break month on, and into beginning ARR after the break month."""
    e = next(e for e in key if e["type"] == "continuity_break")
    delta = e["reported_value_k"] - e["true_value_k"]
    alt = gt.copy()
    mine = (alt["company"] == e["company"]) & (alt["month"] >= e["month"])
    alt.loc[mine, "ending_arr"] = alt.loc[mine, "ending_arr"] + delta
    later = mine & (alt["month"] > e["month"])
    alt.loc[later, "beginning_arr"] = alt.loc[later, "beginning_arr"] + delta
    return alt


def expected_all(gt: pd.DataFrame, key: list[dict]) -> dict:
    """metric_id -> Expected, masking cells flagged in the answer key. Values from the true
    ground truth and from the break-carrying variant are both accepted."""
    mask = _Mask(masked_cells(key))
    base = _Calc(gt, mask).run()
    alt = _Calc(_cobalt_variant(gt, key), mask).run()
    out = {}
    for mid, (c, m, metric, val, inputs, blockers) in base.items():
        accepted = () if val is None else tuple(dict.fromkeys([val, alt[mid][3]]))
        out[mid] = Expected(c, m, metric, accepted, inputs, blockers)
    return out


def expected_unmasked(gt: pd.DataFrame) -> dict:
    """metric_id -> Expected with nothing masked (used for the sanity assertions)."""
    return {mid: Expected(c, m, metric, (val,), inputs, blockers)
            for mid, (c, m, metric, val, inputs, blockers) in _Calc(gt, _Mask([])).run().items()}
