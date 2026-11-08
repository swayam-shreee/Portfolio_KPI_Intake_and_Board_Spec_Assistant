"""Pure view helpers: the overview and detail tables, queue rows, section names, source rows, fingerprint."""
from __future__ import annotations

import hashlib
import json

import pandas as pd

from pipeline import review
from pipeline.commentary import format_value

DASH = "—"
UNDER_REVIEW = "under review"
_CONFIRM_LABELS = {"unknown_label": "Confirm alias", "impossible_value": "Accept as reported",
                   "duplicate_submission": "Close", "prompt_injection": "Close"}


def _blank(v) -> bool:
    return v is None or v != v


def slug(company: str) -> str:
    return str(company).split()[0].lower()


def _latest(p) -> str:
    return p.months[-1]


# --------------------------------------------------------------------------- overview
# Which period each overview figure covers, so the page can say so (the columns mix periods).
OVERVIEW_PERIODS = {"ending_arr": "latest", "growth_12m": "12m", "nrr_12m": "12m",
                    "churn_rate": "latest", "gross_margin": "latest"}


def overview_caption(p) -> str:
    latest = [c for c, k in OVERVIEW_PERIODS.items() if k == "latest"]
    trailing = [c for c, k in OVERVIEW_PERIODS.items() if k == "12m"]
    return (f"{', '.join(latest)} are for the latest month ({_latest(p)}); "
            f"{', '.join(trailing)} cover the trailing twelve months. "
            "A company reads 'Needs review' while any of its values waits in the review queue.")


def overview(p) -> pd.DataFrame:
    cols = ["company", "ending_arr", "growth_12m", "nrr_12m", "churn_rate", "gross_margin",
            "open_items", "status"]
    latest = _latest(p)
    values = {r.metric_id: r.value for r in p.metrics.itertuples()}

    def cell(metric_id: str) -> str:
        if metric_id not in values:
            return DASH
        v = values[metric_id]
        return UNDER_REVIEW if _blank(v) else format_value(metric_id, float(v))

    rows = []
    for company in sorted(set(p.metrics["company"])):
        s = slug(company)
        t = p.table[(p.table["company"] == company) & (p.table["month"] == latest)
                    & (p.table["metric"] == "ending_arr")]
        arr = DASH
        if len(t) and not _blank(t["value_usd_k"].iloc[0]):
            arr = format_value(f"{s}.{latest}.ending_arr", float(t["value_usd_k"].iloc[0]))
        n_open = sum(1 for i in p.review_queue if i.company == company)
        rows.append({"company": company, "ending_arr": arr,
                     "growth_12m": cell(f"{s}.12m.growth_12m"), "nrr_12m": cell(f"{s}.12m.nrr_12m"),
                     "churn_rate": cell(f"{s}.{latest}.churn_rate"),
                     "gross_margin": cell(f"{s}.{latest}.gross_margin"),
                     "open_items": str(n_open), "status": "Needs review" if n_open > 0 else "Ready"})
    return pd.DataFrame(rows, columns=cols, dtype=object)


def metric_detail(p) -> pd.DataFrame:
    accepted = review._accepted(p.decisions)
    cells = {(r.source_file, r.source_cell) for r in p.table.itertuples()
             if (r.company, r.month, r.metric) in accepted}
    rows = []
    for r in p.metrics.itertuples():
        if _blank(r.value):
            display = status = UNDER_REVIEW
        else:
            display = format_value(r.metric_id, float(r.value))
            status = "accepted as reported" if any(tuple(i) in cells for i in r.inputs) else "ok"
        sources = "; ".join(f"{f} {c}" for f, c in r.inputs) or DASH
        rows.append({"metric_id": r.metric_id, "display": display, "status": status, "sources": sources})
    return pd.DataFrame(rows, columns=["metric_id", "display", "status", "sources"], dtype=object)


# --------------------------------------------------------------------------- review queue
def _can_confirm(p, item) -> bool:
    if item.error_type not in review.CONFIRMABLE:
        return False
    if item.error_type == "unknown_label":
        return bool(item.suggestion) and bool(review.raw_label(p, item))
    return True


def marked_as_question(p) -> set[str]:
    """Item ids whose latest decision still in force is a reject ("Mark as question")."""
    return {iid for iid, d in review.latest_decisions(p.decisions).items() if d["action"] == "reject"}


def _decision_text(d: dict) -> str:
    if d["action"] == "reject":
        return "Marked as question"
    if d["error_type"] == "unknown_label":
        return f"Confirmed alias -> {d.get('learned_metric') or DASH}"
    if d["action"] == "correct":
        was = d.get("original_value_usd_k")
        was = f" (was {was:,.0f} $K)" if isinstance(was, (int, float)) else ""
        return f"Corrected to {float(d['corrected_value_usd_k']):,.0f} $K{was}"
    if d["error_type"] == "impossible_value":
        return "Accepted as reported"
    return "Closed"


def decision_rows(p) -> list[dict]:
    """One row per item whose latest decision is still in force, newest first; each can be undone."""
    rows = [{"item_id": iid, "ts": d["ts"], "company": d["company"], "month": d.get("month") or DASH,
             "metric": d.get("metric") or DASH, "error_type": d["error_type"],
             "source_file": d["source_file"], "source_cell": d["source_cell"], "what": _decision_text(d)}
            for iid, d in review.latest_decisions(p.decisions).items()
            if d["company"] in p.files_by_company]
    return sorted(rows, key=lambda r: r["ts"], reverse=True)


def queue_rows(p) -> list[dict]:
    marked = marked_as_question(p)
    rows = []
    for it in p.review_queue:
        iid = review.item_id(it)
        actions = []
        if _can_confirm(p, it):
            actions.append((f"confirm:{iid}", _CONFIRM_LABELS[it.error_type]))
        actions.append((f"reject:{iid}", "Mark as question"))
        g = p.label_guesses.get(iid) if it.error_type == "unknown_label" else None
        by_claude = bool(g) and g.get("metric") is not None and g["metric"] == it.suggestion
        fix = review.proposed_value(it)
        suggested_by = "claude" if by_claude else "bridge check" if fix is not None else \
            ("fuzzy" if it.suggestion else DASH)
        # a separate action, kept out of ``actions`` (those are exactly the confirm/reject pair)
        correction = (f"correct:{iid}", f"Use {fix:,.0f} $K (the bridge ties)") if fix is not None else None
        rows.append({"item_id": iid, "company": it.company, "month": it.month or DASH,
                     "metric": it.metric or DASH, "error_type": it.error_type,
                     "source_file": it.source_file, "source_cell": it.source_cell,
                     "reason": it.reason, "raw_label": review.raw_label(p, it) or DASH,
                     "suggestion": it.suggestion or DASH, "suggested_by": suggested_by,
                     "confidence": f"{g['confidence']:.2f}" if by_claude else DASH, "actions": actions,
                     "correction": correction,
                     "status": "Marked as question" if iid in marked else "Needs decision"})
    return rows


# --------------------------------------------------------------------------- draft helpers
def section_name(section_company: str, p) -> str | None:
    """A code-owned heading: "Portfolio", a full company name, or None when nothing matches."""
    if section_company == "portfolio":
        return "Portfolio"
    for company in sorted(set(p.metrics["company"])):
        if slug(company) == section_company:
            return company
    return None


def sources_rows(paragraph) -> list[dict]:
    rows = []
    for fig in paragraph.figures:
        if not fig.sources:
            rows.append({"metric_id": fig.metric_id, "display": fig.display,
                         "source_file": DASH, "source_cell": DASH})
        for f, c in fig.sources:
            rows.append({"metric_id": fig.metric_id, "display": fig.display,
                         "source_file": f, "source_cell": c})
    return rows


def fingerprint(p) -> str:
    """SHA-256 over the metrics rows and the open item ids: it changes whenever a draft would."""
    rows = sorted(
        (r.metric_id, None if _blank(r.value) else round(float(r.value), 6),
         None if _blank(r.blocked_reason) else str(r.blocked_reason))
        for r in p.metrics.itertuples())
    open_ids = sorted(review.item_id(i) for i in p.review_queue)
    return hashlib.sha256(json.dumps([rows, open_ids]).encode()).hexdigest()
