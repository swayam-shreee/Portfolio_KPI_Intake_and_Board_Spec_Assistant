"""Validation checks. Failing values go to the review queue and out of the table."""
from __future__ import annotations

import dataclasses
import statistics

import pandas as pd

from pipeline.contract import ReviewItem
from pipeline.mapping import looks_like_instruction

ARR_TOL = 1.0   # $K, plus half a rounding step of every figure that was rounded in the file


def _prev(month: str) -> str:
    y, m = map(int, month.split("-"))
    return f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"


def _months_between(a: str, b: str) -> list[str]:
    out, cur = [], a
    while cur <= b:
        out.append(cur)
        y, m = map(int, cur.split("-"))
        cur = f"{y + (m == 12):04d}-{m % 12 + 1:02d}"
    return out


def _share(gap: float, row) -> str:
    """|gap| as a share of the row's value, e.g. '0.15%' (materiality context for a reviewer)."""
    base = abs(float(row["value_usd_k"]))
    return f"{abs(gap) / base * 100:.2f}%" if base else "n/a"


_BRIDGE = ("beginning_arr", "new_arr", "expansion_arr", "contraction_arr", "churned_arr", "ending_arr")


def _unit_fix(cell: dict, company: str, month: str, metric: str, value: float, arr_med: float):
    """The one value (value x1000 or /1000) that is back in the company's range AND makes this
    month's ARR bridge tie within tolerance, else None. Code proposes (as the impossible_value
    item's ``suggestion``, a $K number in text); only a person applies it, via review.correct."""
    keys = [(company, month, x) for x in _BRIDGE]
    if metric not in ("beginning_arr", "ending_arr") or not all(k in cell for k in keys):
        return None
    terms = {x: cell[k] for x, k in zip(_BRIDGE, keys)}
    tol = ARR_TOL + sum(0.5 * r["step_k"] for x, r in terms.items() if x != metric and r["step_k"] > 1)
    fits = []
    for factor in (1 / 1000, 1000):
        cand = value * factor
        if not (arr_med / 10 <= cand <= arr_med * 10):
            continue
        v = {x: (cand if x == metric else float(r["value_usd_k"])) for x, r in terms.items()}
        gap = v["beginning_arr"] + v["new_arr"] + v["expansion_arr"] - v["contraction_arr"] \
            - v["churned_arr"] - v["ending_arr"]
        if abs(gap) <= tol:
            fits.append(round(cand, 6))
    return fits[0] if len(fits) == 1 else None


def _norm_text(text: str) -> str:
    return " ".join(str(text).split()).casefold()


def injection_items(files) -> list[ReviewItem]:
    """One item per distinct instruction-like text per company. The same text repeated in a
    resubmission (possibly at another cell) is one item, cited from the newest file, using the
    same newest-wins order as ``ingest.resolve_duplicates`` (submitted date, then file name)."""
    newest: dict = {}     # (company, normalized text) -> (sort key, ReviewItem)
    order: list = []
    for fd in files:
        company = fd.company or fd.source_file
        for cell, text in fd.text_cells:
            if not looks_like_instruction(text):
                continue
            key = (company, _norm_text(text))
            rank = (fd.submitted, fd.source_file)
            if key in newest and newest[key][0] >= rank:
                continue
            if key not in newest:
                order.append(key)
            newest[key] = (rank, ReviewItem(
                company=company, month=None, metric=None,
                error_type="prompt_injection", source_file=fd.source_file, source_cell=cell,
                reason=("This cell contains instruction-like text aimed at an automated "
                        "reader. It was treated as data, not followed, and no value was "
                        "taken from it.")))
    return [newest[k][1] for k in order]


def _item(row, error_type, reason, metric=None, month=None) -> ReviewItem:
    return ReviewItem(company=row["company"], month=month or row["month"],
                      metric=metric or row["metric"], error_type=error_type,
                      source_file=row["source_file"], source_cell=row["source_cell"], reason=reason)


def run_checks(df: pd.DataFrame, files, accepted: frozenset = frozenset()) -> tuple[pd.DataFrame, list[ReviewItem]]:
    items: list[ReviewItem] = []
    flagged: set = set()          # (company, month, metric) kept out of the table
    if df.empty:
        return df, items
    cell = {(r.company, r.month, r.metric): r._asdict() for r in df.itertuples(index=False)}

    declared: dict = {}
    for fd in files:
        for m, c in fd.declared_months.items():
            declared.setdefault((fd.company, m), (fd.source_file, c))

    for company, g in df.groupby("company"):
        months_present = set(g.month)
        declared_m = {m for (c, m) in declared if c == company}
        span = _months_between(min(months_present | declared_m), max(months_present | declared_m))
        reported = set(g[~g.derived].metric)

        # completeness: whole months, then single metrics within a present month
        for m in span:
            if m not in months_present:
                sf, c = declared.get((company, m)) or next(
                    (declared[(company, p)] for p in (_prev(m),) if (company, p) in declared),
                    (g.iloc[0].source_file, "A1"))
                items.append(ReviewItem(
                    company=company, month=m, metric=None, error_type="missing_month",
                    source_file=sf, source_cell=c,
                    reason=f"No values were reported for {m}; later figures that depend on it cannot be verified."))
            else:
                for metric in sorted(reported):
                    if (company, m, metric) not in cell:
                        ref = g[g.month == m].iloc[0]
                        items.append(ReviewItem(
                            company=company, month=m, metric=metric, error_type="missing_month",
                            source_file=ref.source_file, source_cell=ref.source_cell,
                            reason=f"{metric} is blank for {m} although it is reported in other months."))

        # plausible ranges and unit sanity
        arr_med = statistics.median(g[g.metric == "ending_arr"].value_usd_k) \
            if (g.metric == "ending_arr").any() else None
        for r in g.to_dict("records"):
            if bool(r["derived"]):   # a derived value is a copy; its source gets these checks itself
                continue
            k, v, why = (company, r["month"], r["metric"]), r["value_usd_k"], None
            if r["metric"] == "gross_margin_pct" and not 0 <= v <= 100:
                why = f"Gross margin of {v:g}% is outside 0-100%."
            elif r["metric"] in ("customers", "headcount") and v <= 0:
                why = f"{r['metric']} must be above zero."
            elif r["metric"] not in ("gross_margin_pct", "customers", "headcount") and v < 0:
                why = f"{r['metric']} must not be negative."
            elif r["metric"] in ("ending_arr", "beginning_arr") and arr_med and \
                    not (arr_med / 10 <= v <= arr_med * 10):
                why = "ARR is more than 10x away from the company's median; possible unit mix-up."
            if why and k not in accepted:     # a person accepted this value as reported
                flagged.add(k)
                item = _item(r, "impossible_value", why)
                if why.startswith("ARR is more than 10x"):
                    fix = _unit_fix(cell, company, r["month"], r["metric"], float(v), arr_med)
                    if fix is not None:     # the proposed $K value, as text; never applied here
                        item = dataclasses.replace(item, suggestion=f"{fix:g}")
                items.append(item)

        # derived values inherit their source's status: a derived beginning copied from a flagged
        # ending is dropped too, with no item of its own (the source's item explains it), and
        # metrics back-solves it from that month's components.
        for r in g[g["derived"].astype(bool) & (g.metric == "beginning_arr")].itertuples():
            if (company, _prev(r.month), "ending_arr") in flagged:
                flagged.add((company, r.month, "beginning_arr"))

        # rollforward and continuity, in month order
        for m in sorted(months_present):
            keys = [(company, m, x) for x in ("beginning_arr", "new_arr", "expansion_arr",
                                              "contraction_arr", "churned_arr", "ending_arr")]
            if not all(k in cell and k not in flagged for k in keys):
                continue
            b, n, e, c, ch, end = (cell[k] for k in keys)
            tol = ARR_TOL + sum(0.5 * x["step_k"] for x in (b, n, e, c, ch, end) if x["step_k"] > 1)
            gap = b["value_usd_k"] + n["value_usd_k"] + e["value_usd_k"] - c["value_usd_k"] \
                - ch["value_usd_k"] - end["value_usd_k"]
            if abs(gap) <= tol:
                continue
            prior_end = (company, _prev(m), "ending_arr")
            if b["derived"] and prior_end in flagged:
                flagged.add(keys[0])
                items.append(_item(b, "rollforward_mismatch",
                                   f"Beginning ARR is taken from {_prev(m)} ending ARR, which is itself "
                                   f"under review, so {m} does not reconcile.", metric="beginning_arr"))
            else:
                flagged.add(keys[5])
                items.append(_item(end, "rollforward_mismatch",
                                   f"Beginning + new + expansion - contraction - churn differs from "
                                   f"ending ARR by {abs(gap):g} $K ({_share(gap, end)} of ending ARR; "
                                   f"tolerance {tol:g})."))
        for m in sorted(months_present):
            kb, ke = (company, m, "beginning_arr"), (company, _prev(m), "ending_arr")
            if kb in cell and ke in cell and not cell[kb]["derived"] and kb not in flagged \
                    and ke not in flagged:
                b, e = cell[kb], cell[ke]
                tol = ARR_TOL + sum(0.5 * x["step_k"] for x in (b, e) if x["step_k"] > 1)
                if abs(b["value_usd_k"] - e["value_usd_k"]) > tol:
                    flagged.add(kb)
                    items.append(_item(b, "continuity_break",
                                       f"Beginning ARR differs from {_prev(m)} ending ARR by "
                                       f"{b['value_usd_k'] - e['value_usd_k']:g} $K."))

    keep = df[[(r.company, r.month, r.metric) not in flagged for r in df.itertuples()]]
    return keep, items
