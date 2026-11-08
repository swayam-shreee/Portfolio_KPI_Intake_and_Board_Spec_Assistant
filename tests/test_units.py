"""Unit tags: "dollars" counts only as a tag, never as a word inside a metric name."""
from datetime import datetime

import openpyxl
import pytest

from pipeline import ingest


@pytest.mark.parametrize("text, factor", [
    ("Net Dollar Gains - Installed Base", None),
    ("Dollar-based net retention", None),
    ("Revenue (dollars)", 0.001),
    ("Revenue (in dollars)", 0.001),
    ("Figures in dollars", 0.001),
    ("Ending ARR (USD)", 0.001),
    ("Revenue ($)", 0.001),
    ("Annual Recurring Rev ($M)", 1000.0),
    ("USD 000s", 1.0),
])
def test_parse_unit(text, factor):
    assert ingest.parse_unit(text) == factor


def test_dollar_word_in_label_keeps_sheet_unit(tmp_path):
    """A 'USD 000s' note governs a column whose name merely contains 'Dollar'."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "KPI Data"
    ws.append(["Testco - Investor KPI Report"])
    ws.append(["Submitted 2026-11-06 | USD 000s"])
    ws.append(["Month", "Beginning ARR", "Net Dollar Gains - Installed Base", "Ending ARR"])
    ws.append([datetime(2026, 9, 30), 1000, 508, 1508])
    ws.append([datetime(2026, 10, 31), 1508, 455, 1963])
    ws.append([datetime(2026, 11, 30), 1963, 400, 2363])
    path = tmp_path / "Testco_KPIs.xlsx"
    wb.save(path)

    fd = ingest.read_file(path)
    s = next(x for x in fd.series if x.label == "Net Dollar Gains - Installed Base")
    assert s.unit_factor is None
    assert s.sheet_unit == 1.0
