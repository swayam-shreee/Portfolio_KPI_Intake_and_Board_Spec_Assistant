"""Read raw submissions into layout-neutral series; no business logic.

Layouts are detected from content only:
* months as columns: a row with several month-like values is the header row;
* months as rows: a column with several month-like values, labels in the row above;
* one workbook tab per period is handled by reading every sheet and merging.

Units come from tags in a label (``($M)``, ``(USD)``), else from a note above the
header row, else from the single unit tag the sheet's labels agree on.
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import re
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Optional

import pandas as pd
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from pipeline.contract import COMPANIES, ReviewItem

MIN_MONTHS = 3
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# --------------------------------------------------------------------------- data model
@dataclass
class Series:
    label: str
    label_cell: str
    values: dict            # month -> (number, cell)
    unit_factor: Optional[float]   # $K per raw unit, from a tag in the label
    sheet_unit: Optional[float]    # $K per raw unit, from a sheet-level note or consensus
    decimals: int = 0


@dataclass
class FileData:
    source_file: str
    company: Optional[str]
    submitted: float                       # POSIX timestamp used for newest-wins
    sha256: str
    series: list = field(default_factory=list)
    declared_months: dict = field(default_factory=dict)   # month -> header cell
    text_cells: list = field(default_factory=list)        # (cell, text) of every string cell


# --------------------------------------------------------------------------- parsing helpers
def parse_month(v) -> Optional[str]:
    """Month-like value -> 'YYYY-MM', else None. Plain numbers are never months."""
    if isinstance(v, (dt.datetime, dt.date)):
        return f"{v.year:04d}-{v.month:02d}"
    if not isinstance(v, str):
        return None
    s = v.strip()
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})(?:[-/]\d{1,2})?", s)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    m = re.fullmatch(r"(\d{1,2})[-/](\d{4})", s)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{int(m.group(2)):04d}-{int(m.group(1)):02d}"
    m = re.fullmatch(r"([A-Za-z]{3,9})[\s\-'/.,]*(\d{4}|\d{2})", s)
    if m and m.group(1)[:3].lower() in _MONTHS:
        y = int(m.group(2))
        y += 2000 if y < 100 else 0
        return f"{y:04d}-{_MONTHS[m.group(1)[:3].lower()]:02d}"
    return None


def parse_number(v) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if not isinstance(v, str):
        return None
    s = v.strip().replace(",", "").replace("$", "")
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()")
    pct = s.endswith("%")
    s = s.rstrip("%").strip()
    if not re.fullmatch(r"[-+]?\d+(\.\d+)?", s):
        return None
    x = float(s) / (100.0 if pct else 1.0)
    return -x if neg else x


def decimals_of(v) -> int:
    if isinstance(v, str):
        m = re.search(r"\.(\d+)", v)
        return len(m.group(1)) if m else 0
    return max(0, -Decimal(repr(round(float(v), 9))).normalize().as_tuple().exponent)


def parse_unit(text: str) -> Optional[float]:
    """$K per one raw unit, from a unit tag or note; None if the text says nothing."""
    t = str(text).lower()
    if re.search(r"\$\s*m\b|\bmillions?\b|\bin\s*mm?\b|\(\s*mm?\s*\)|\$\s*mm", t):
        return 1000.0
    if re.search(r"\$\s*k\b|\bthousands?\b|\b000s?\b|\$\s*000|\(\s*000\s*\)|\(\s*k\s*\)", t):
        return 1.0
    # "dollars" counts only as a tag ("(dollars)", "in dollars"), never as a word inside a
    # metric name such as "Net Dollar Gains", which would wrongly scale the row by 1/1000.
    if re.search(r"\(\s*\$\s*\)|\busd\b|\(\s*(?:in\s+)?dollars?\s*\)|\bin\s+dollars?\b|\bwhole\b", t):
        return 0.001
    return None


# --------------------------------------------------------------------------- grids
def _quote(sheet: str) -> str:
    return "'" + sheet.replace("'", "''") + "'"


def _read_grids(path: Path):
    """Yield (sheet_title or None, {(row, col): value}) for each sheet."""
    if path.suffix.lower() == ".csv":
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        yield None, {(r, c): (v if v != "" else None)
                     for r, row in enumerate(rows, 1) for c, v in enumerate(row, 1)}
        return
    wb = load_workbook(path, data_only=True)
    for ws in wb.worksheets:
        yield ws.title, {(c.row, c.column): c.value for row in ws.iter_rows()
                         for c in row if c.value is not None}


def _ref(sheet: Optional[str], r: int, c: int) -> str:
    a1 = f"{get_column_letter(c)}{r}"
    return f"{_quote(sheet)}!{a1}" if sheet is not None else a1


def _detect_company(candidates: list[str]) -> Optional[str]:
    words: set[str] = set()
    for c in candidates:
        words |= set(re.findall(r"[a-z0-9]+", c.lower()))
    best, best_score = None, 0
    for name in COMPANIES:
        toks = re.findall(r"[a-z0-9]+", name.lower())
        if toks[0] in words:
            score = sum(t in words for t in toks)
            if score > best_score:
                best, best_score = name, score
    return best


def _submitted_ts(top_text: list[str], path: Path) -> float:
    for t in top_text:
        m = re.search(r"(?:submitted|sent|date)\D{0,12}(\d{4}-\d{2}-\d{2})", t, re.I)
        if m:
            return dt.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
    return path.stat().st_mtime


# --------------------------------------------------------------------------- layout detection
def _month_cells(grid):
    by_row: dict[int, list] = {}
    by_col: dict[int, list] = {}
    for (r, c), v in grid.items():
        m = parse_month(v)
        if m:
            by_row.setdefault(r, []).append((c, m))
            by_col.setdefault(c, []).append((r, m))
    return by_row, by_col


def _extract_sheet(sheet, grid, fd: FileData) -> list[str]:
    """Add the sheet's series to fd; return top-of-sheet text (titles, notes)."""
    by_row, by_col = _month_cells(grid)
    row_best = max(by_row.items(), key=lambda kv: len(kv[1]), default=(None, []))
    col_best = max(by_col.items(), key=lambda kv: len(kv[1]), default=(None, []))
    raw_series: list[Series] = []
    top_text: list[str] = []

    if len(row_best[1]) >= MIN_MONTHS and len(row_best[1]) >= len(col_best[1]):
        hr, mcols = row_best[0], sorted(row_best[1])
        label_col = max(1, min(c for c, _ in mcols) - 1)
        for c, m in mcols:
            fd.declared_months.setdefault(m, _ref(sheet, hr, c))
        max_row = max(r for r, _ in grid)
        for r in range(hr + 1, max_row + 1):
            label = grid.get((r, label_col))
            vals = {}
            for c, m in mcols:
                x = parse_number(grid.get((r, c)))
                if x is not None:
                    vals[m] = (x, _ref(sheet, r, c), grid[(r, c)])
            if isinstance(label, str) and vals:
                raw_series.append(_mk_series(label, _ref(sheet, r, label_col), vals))
        top_text = [str(v) for (r, c), v in sorted(grid.items()) if r < hr and isinstance(v, str)]
    elif len(col_best[1]) >= MIN_MONTHS:
        mc, mrows = col_best[0], sorted(col_best[1])
        first = mrows[0][0]
        hr = first - 1
        while hr > 1 and sum(isinstance(v, str) for (r, c), v in grid.items() if r == hr) < 2:
            hr -= 1
        for r, m in mrows:
            fd.declared_months.setdefault(m, _ref(sheet, r, mc))
        for (r, c), label in sorted(grid.items(), key=lambda kv: (kv[0][1], kv[0][0])):
            if r != hr or c == mc or not isinstance(label, str):
                continue
            vals = {}
            for rr, m in mrows:
                x = parse_number(grid.get((rr, c)))
                if x is not None:
                    vals[m] = (x, _ref(sheet, rr, c), grid[(rr, c)])
            if vals:
                raw_series.append(_mk_series(label, _ref(sheet, hr, c), vals))
        top_text = [str(v) for (r, c), v in sorted(grid.items()) if r < hr and isinstance(v, str)]
    else:
        return []

    note_unit = next((u for t in top_text if (u := parse_unit(t)) is not None), None)
    tags = {s.unit_factor for s in raw_series if s.unit_factor is not None}
    sheet_unit = note_unit if note_unit is not None else (tags.pop() if len(tags) == 1 else None)
    for s in raw_series:
        s.sheet_unit = sheet_unit
    fd.series.extend(raw_series)
    return top_text


def _mk_series(label: str, label_cell: str, vals: dict) -> Series:
    return Series(
        label=label, label_cell=label_cell,
        values={m: (x, cell) for m, (x, cell, _raw) in vals.items()},
        unit_factor=parse_unit(label), sheet_unit=None,
        decimals=max(decimals_of(raw) for _x, _c, raw in vals.values()))


def read_file(path: str | Path) -> FileData:
    path = Path(path)
    fd = FileData(source_file=path.name, company=None, submitted=0.0,
                  sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    candidates, top = [path.stem], []
    for sheet, grid in _read_grids(path):
        fd.text_cells += [(_ref(sheet, r, c), v) for (r, c), v in sorted(grid.items())
                          if isinstance(v, str)]
        t = _extract_sheet(sheet, grid, fd)
        top += t
        candidates += t[:3]
    fd.company = _detect_company(candidates)
    fd.submitted = _submitted_ts(top, path)
    return fd


# --------------------------------------------------------------------------- cross-file steps
def resolve_duplicates(df: pd.DataFrame) -> tuple[pd.DataFrame, list[ReviewItem]]:
    """Newest submission wins for a repeated company-month-metric. Changed values are
    flagged per cell; a repeated file with no changed value is flagged once."""
    items: list[ReviewItem] = []
    if df.empty:
        return df, items
    keys = ["company", "month", "metric"]
    keep_idx = df.sort_values(["file_ts", "source_file"]).drop_duplicates(keys, keep="last").index
    keep, losers = df.loc[keep_idx], df.drop(keep_idx)
    changed: set = set()
    for _, lo in losers.iterrows():
        win = keep[(keep.company == lo.company) & (keep.month == lo.month)
                   & (keep.metric == lo.metric)].iloc[0]
        if abs(win.value_usd_k - lo.value_usd_k) > 1e-6:
            changed.add((lo.company, lo.source_file))
            items.append(ReviewItem(
                company=win.company, month=win.month, metric=win.metric,
                error_type="duplicate_submission", source_file=win.source_file,
                source_cell=win.source_cell,
                reason=(f"{win.source_file} repeats a month already submitted in {lo.source_file} "
                        f"with a different value ({lo.value_usd_k:g} vs {win.value_usd_k:g} $K); "
                        f"the newer submission was used.")))
    for (company, fname), grp in losers.groupby(["company", "source_file"]):
        if (company, fname) not in changed:
            items.append(ReviewItem(
                company=company, month=None, metric=None, error_type="duplicate_submission",
                source_file=fname, source_cell=grp.iloc[0].source_cell,
                reason=f"{fname} repeats months already submitted in a newer file; the newer file was used."))
    return keep.sort_index(), items


def derive_beginning(df: pd.DataFrame) -> pd.DataFrame:
    """Companies that report no beginning-ARR row get it from the prior month's ending
    ARR; the new row cites the cell it was taken from."""
    rows = []
    for company, g in df.groupby("company"):
        if (g.metric == "beginning_arr").any():
            continue
        end = g[g.metric == "ending_arr"].set_index("month")
        for month in sorted(end.index):
            y, m = map(int, month.split("-"))
            prior = f"{y - (m == 1):04d}-{(m - 2) % 12 + 1:02d}"
            if prior in end.index:
                p = end.loc[prior]
                rows.append({**p.to_dict(), "month": month, "metric": "beginning_arr", "derived": True})
    if rows:
        df = pd.concat([df, pd.DataFrame(rows)], ignore_index=True)
    return df
