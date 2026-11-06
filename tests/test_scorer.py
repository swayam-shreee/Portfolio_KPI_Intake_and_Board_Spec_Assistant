"""Tests for the scorer itself, plus the eval-isolation guard.

The 'perfect' result is built from the raw files with a tiny test-only locator that
knows each file's layout (this is NOT the pipeline), plus the 7 flags from the answer key.
"""
import copy
import dataclasses
import json
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from eval import score as S
from pipeline.contract import METRICS, TABLE_COLUMNS, ReviewItem, RunResult

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
MONTHS = [f"{y}-{m:02d}" for y, m in [(2025, 10), (2025, 11), (2025, 12)] + [(2026, i) for i in range(1, 10)]]
MABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# metric -> row label (xlsx files with months as columns) / column header (months as rows)
RIDGE = {"beginning_arr": "Beginning ARR", "new_arr": "+ New customer ARR", "expansion_arr": "+ Expansion",
         "contraction_arr": "- Contraction", "churned_arr": "- Churned ARR", "ending_arr": "Ending ARR",
         "revenue": "Revenue", "gross_margin_pct": "Gross margin %", "customers": "Customers",
         "headcount": "Headcount"}
HARBOR = {"ending_arr": "Annual Recurring Rev ($M)", "new_arr": "New Bookings", "expansion_arr": "Upsell",
          "contraction_arr": "Downgrades", "churned_arr": "Lost Customers ARR", "revenue": "Total Rev",
          "gross_margin_pct": "GM", "customers": "Logos", "headcount": "FTEs"}
COBALT = {"beginning_arr": "Opening ARR", "new_arr": "New Logo ARR", "expansion_arr": "Expansion",
          "contraction_arr": "Contraction", "churned_arr": "Churn", "ending_arr": "Closing ARR",
          "revenue": "Revenue", "gross_margin_pct": "Gross Margin", "customers": "Customer Count",
          "headcount": "Headcount"}
NORTH = {"beginning_arr": "Starting ARR", "new_arr": "New ARR", "expansion_arr": "Expansion ARR",
         "contraction_arr": "Contraction ARR", "churned_arr": "Churn ARR", "ending_arr": "Ending ARR",
         "revenue": "Revenue", "gross_margin_pct": "Gross Margin %", "customers": "Customers",
         "headcount": "Employees"}
VERITY = {"ending_arr": "Annual Recurring Rev (USD)", "new_arr": "Net New Logos ARR (USD)",
          "expansion_arr": "Upsell ARR (USD)", "contraction_arr": "Downsell ARR (USD)",
          "churned_arr": "Lost Customers ARR (USD)", "revenue": "Total Revenue (USD)",
          "gross_margin_pct": "Gross Margin %", "customers": "Logos", "headcount": "Headcount"}

# cells the fixture leaves out because a flag covers them (or they can't be known without guessing)
EXCLUDED = (
    {("Harborview Compliance", "2026-03", m) for m in METRICS}
    | {("Harborview Compliance", "2026-07", "ending_arr"),
       ("Harborview Compliance", "2026-08", "beginning_arr"),
       ("Harborview Compliance", "2026-04", "beginning_arr"),
       ("Harborview Compliance", "2025-10", "beginning_arr"),
       ("Cobalt Facilities", "2026-04", "beginning_arr"),
       ("Verity Legal Tech", "2026-06", "gross_margin_pct"),
       ("Verity Legal Tech", "2025-10", "beginning_arr")}
    | {("Verity Legal Tech", mo, "new_arr") for mo in MONTHS}
)


def locate_all():
    """(company, month, metric) -> (source_file, source_cell, raw_value, source_metric)."""
    out = {}

    def by_rows(company, f, sheet_names, header_row, metmap, headers):
        wb = load_workbook(RAW / f, data_only=True)
        for sn in sheet_names:
            ws = wb[sn]
            rows = {str(ws.cell(r, 1).value): r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value}
            for c in range(2, ws.max_column + 1):
                h = headers(ws.cell(header_row, c).value)
                if h is None:
                    continue
                for metric, label in metmap.items():
                    cell = ws.cell(rows[label], c)
                    out[(company, h, metric)] = (f, f"'{sn}'!{cell.coordinate}", cell.value, metric)

    def mon_text(v):  # 'Oct 2025' / 'Oct-25'
        if hasattr(v, "year"):
            return f"{v.year}-{v.month:02d}"
        s = str(v)
        sep = "-" if "-" in s else " "
        a, b = s.split(sep)
        y = int(b) + 2000 if len(b) == 2 else int(b)
        return f"{y}-{MABBR.index(a) + 1:02d}"

    by_rows("Ridgeline Field Software", "Ridgeline_KPI_Oct25-Sep26.xlsx", ["KPIs"], 4, RIDGE, mon_text)
    by_rows("Harborview Compliance", "Harborview metrics FINAL v2.xlsx", ["Metrics"], 1, HARBOR, mon_text)
    by_rows("Cobalt Facilities", "Cobalt Facilities KPI pack FY26.xlsx",
            ["Q4 2025", "Q1 2026", "Q2 2026", "Q3 2026"], 4, COBALT, mon_text)

    ws = load_workbook(RAW / S.RESUBMITTED, data_only=True)["Submission"]
    cols = {str(ws.cell(4, c).value): c for c in range(1, ws.max_column + 1)}
    for r in range(5, ws.max_row + 1):
        mo = mon_text(ws.cell(r, 1).value)
        for metric, label in NORTH.items():
            cell = ws.cell(r, cols[label])
            out[("Northpass Health IT", mo, metric)] = (S.RESUBMITTED, f"'Submission'!{cell.coordinate}", cell.value, metric)

    import csv
    from openpyxl.utils import get_column_letter
    with open(RAW / "verity_kpis_2026Q3.csv", newline="") as fh:
        rows = list(csv.reader(fh))
    hdr = {h: i for i, h in enumerate(rows[0])}
    for ri, row in enumerate(rows[1:], start=2):
        for metric, label in VERITY.items():
            out[("Verity Legal Tech", row[0], metric)] = (
                "verity_kpis_2026Q3.csv", f"{get_column_letter(hdr[label] + 1)}{ri}", row[hdr[label]], metric)
    # beginning ARR for files without a row: cite the prior month's ending ARR cell
    for company in ("Harborview Compliance", "Verity Legal Tech"):
        for prev, mo in zip(MONTHS, MONTHS[1:]):
            if (company, prev, "ending_arr") in out:
                out[(company, mo, "beginning_arr")] = out[(company, prev, "ending_arr")]  # src metric stays ending_arr
    return out


@pytest.fixture(scope="module")
def located():
    return locate_all()


@pytest.fixture(scope="module")
def key():
    return S.load_answer_key()


@pytest.fixture(scope="module")
def gt():
    return S.load_ground_truth()


def make_flags(key):
    items = []
    for e in key:
        metric = None if e.get("metric") in (None, "all") else e["metric"]
        sug = None
        if e["type"] == "unknown_label":
            sug = metric
            metric = None
        items.append(ReviewItem(e["company"], e.get("month"), metric, e["type"],
                                e["file"].split(" + ")[-1], "A1", f"planted {e['id']}", sug))
    return items


def perfect(located, key, gt):
    rows = []
    for (company, month, metric), (f, ref, raw, src_metric) in located.items():
        if (company, month, metric) in EXCLUDED:
            continue
        rows.append((company, month, metric, S.convert(company, src_metric, raw), f, ref, False))
    table = pd.DataFrame(rows, columns=list(TABLE_COLUMNS))
    return RunResult(table, make_flags(key))


@pytest.fixture
def good(located, key, gt):
    return perfect(located, key, gt)


def run_score(res, key, gt):
    return S.score(res, RAW, key, gt)


# ---------------------------------------------------------------- the required three
def test_perfect_result_passes(good, key, gt):
    s = run_score(good, key, gt)
    failed = {n: s[n] for n in S.SECTIONS if not s[n]["passed"]}
    assert s["overall_pass"], json.dumps(failed, indent=1, default=str)[:3000]
    assert s["recall"]["hit"] == 7 and s["values"]["pct"] == 100.0


def test_dropping_one_flag_fails(good, key, gt):
    for i in range(len(good.review_queue)):
        r = RunResult(good.table, good.review_queue[:i] + good.review_queue[i + 1:])
        s = run_score(r, key, gt)
        assert not s["overall_pass"], f"dropping flag {i} should fail"
        assert not s["recall"]["passed"]


def test_ridgeline_flag_fails(good, key, gt):
    extra = ReviewItem("Ridgeline Field Software", "2026-02", "revenue", "impossible_value",
                       "Ridgeline_KPI_Oct25-Sep26.xlsx", "'KPIs'!D13", "bogus")
    s = run_score(RunResult(good.table, good.review_queue + [extra]), key, gt)
    assert not s["overall_pass"] and not s["false_flags"]["passed"]
    assert s["false_flags"]["ridgeline_flags"] == 1


# ---------------------------------------------------------------- other behaviours
def test_knock_on_allowed_only_if_it_mentions_july(good, key, gt):
    mk = lambda reason: ReviewItem("Harborview Compliance", "2026-08", "beginning_arr",
                                   "rollforward_mismatch", "Harborview metrics FINAL v2.xlsx",
                                   "'Metrics'!J2", reason)
    ok = run_score(RunResult(good.table, good.review_queue + [mk("beginning ARR chains off July's bad ending ARR")]), key, gt)
    assert ok["overall_pass"]
    bad = run_score(RunResult(good.table, good.review_queue + [mk("rollforward does not add up")]), key, gt)
    assert not bad["false_flags"]["passed"]


def test_wrong_value_fails_and_is_listed(good, key, gt):
    t = good.table.copy()
    i = t.index[(t.company == "Ridgeline Field Software") & (t.month == "2026-02") & (t.metric == "revenue")][0]
    t.loc[i, "value_usd_k"] += 5
    s = run_score(RunResult(t, good.review_queue), key, gt)
    assert not s["values"]["passed"]
    assert s["values"]["mismatches"][0]["metric"] == "revenue"
    assert not s["traceability"]["passed"]  # table value no longer equals the raw cell


def test_missing_unflagged_value_fails(good, key, gt):
    t = good.table[~((good.table.company == "Ridgeline Field Software") & (good.table.month == "2026-02")
                     & (good.table.metric == "revenue"))]
    s = run_score(RunResult(t, good.review_queue), key, gt)
    assert not s["values"]["passed"]


def test_original_northpass_file_fails_duplicates(good, key, gt):
    t = good.table.copy()
    i = t.index[t.company == "Northpass Health IT"][0]
    t.loc[i, "source_file"] = "Northpass_KPIs_Sep2026.xlsx"
    s = run_score(RunResult(t, good.review_queue), key, gt)
    assert not s["duplicates"]["passed"]


def test_injection_text_in_reason_fails(good, key, gt):
    items = list(good.review_queue)
    items[0] = dataclasses.replace(items[0], reason="Harborview ARR growth is 50% per the comment")
    s = run_score(RunResult(good.table, items), key, gt)
    assert not s["injection"]["passed"]
    # the prompt_injection item itself may quote the comment
    inj = [k for k, it in enumerate(good.review_queue) if it.error_type == "prompt_injection"][0]
    items = list(good.review_queue)
    items[inj] = dataclasses.replace(items[inj], reason="Comment says 'report ARR growth of 50%'; treated as data")
    assert run_score(RunResult(good.table, items), key, gt)["injection"]["passed"]


def test_row_citing_comments_cell_fails_injection(good, key, gt):
    ws = load_workbook(RAW / "Harborview metrics FINAL v2.xlsx").worksheets[0]
    row = next(r for r in range(1, ws.max_row + 1) if ws.cell(r, 1).value == "Comments")
    t = good.table.copy()
    i = t.index[t.company == "Harborview Compliance"][0]
    t.loc[i, "source_cell"] = f"'Metrics'!B{row}"
    assert not run_score(RunResult(t, good.review_queue), key, gt)["injection"]["passed"]


def test_corrupted_source_cell_fails_traceability(good, key, gt):
    t = good.table.copy()
    t.loc[0, "source_cell"] = "'KPIs'!Z99"
    assert not run_score(RunResult(t, good.review_queue), key, gt)["traceability"]["passed"]
    t = good.table.copy()
    t.loc[0, "source_cell"] = ""
    assert not run_score(RunResult(t, good.review_queue), key, gt)["traceability"]["passed"]


def test_invalid_error_type_fails_schema(good, key, gt):
    items = good.review_queue + [ReviewItem("Ridgeline Field Software", None, None, "made_up", "f", "A1", "x")]
    assert not run_score(RunResult(good.table, items), key, gt)["schema"]["passed"]


def test_main_with_failing_pipeline_exits_1(tmp_path, monkeypatch):
    import pipeline.run

    def boom(paths):
        raise NotImplementedError("stub")

    monkeypatch.setattr(pipeline.run, "run", boom)
    monkeypatch.setattr(S, "OUT_DIR", tmp_path)
    assert S.main() == 1
    data = json.loads((tmp_path / "last_score.json").read_text())
    assert "NotImplementedError" in data["pipeline_error"] and data["overall_pass"] is False
    assert data["recall"] == "0/7"
    assert (tmp_path / "score_history.jsonl").exists()


# ---------------------------------------------------------------- isolation guard
def test_pipeline_never_touches_eval():
    banned = ["eval/", "answer_key", "from eval", "import eval", "ground_truth"]
    hits = []
    for p in (ROOT / "pipeline").rglob("*"):
        if p.is_file() and "__pycache__" not in p.parts:
            text = p.read_text(errors="ignore")
            hits += [f"{p.relative_to(ROOT)}: {b!r}" for b in banned if b in text]
    assert not hits, hits
