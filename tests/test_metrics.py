"""compute_metrics on small synthetic tables: derived vs reported beginning_arr."""
import pandas as pd

from pipeline.contract import TABLE_COLUMNS, ReviewItem, compute_metrics

CO = "Acme Co"


def _table(rows):
    """rows: (month, metric, value, derived)"""
    return pd.DataFrame([(CO, m, k, v, "acme.csv", f"{k[:2]}{i}", d)
                         for i, (m, k, v, d) in enumerate(rows)], columns=list(TABLE_COLUMNS))


def _metric(df, suffix):
    return df[df.metric_id.str.endswith(suffix)].iloc[0]


def _components(month, end=1100.0):
    return [(month, "ending_arr", end, False), (month, "new_arr", 200.0, False),
            (month, "expansion_arr", 50.0, False), (month, "contraction_arr", 30.0, False),
            (month, "churned_arr", 20.0, False)]


def test_single_month_no_beginning_row_is_back_solved():
    # 1100 - 200 - 50 + 30 + 20 = 900 -> churn 20 / 900
    m = compute_metrics(_table(_components("2025-10")), [])
    r = _metric(m, "acme.2025-10.churn_rate")
    assert r.value is not None and abs(r.value - 20 / 900 * 100) < 1e-9
    assert pd.isna(r.blocked_reason)


def test_derived_company_with_every_derived_row_dropped_is_still_back_solved():
    # derived rows for 2025-11 and 2025-12 were removed (e.g. prior ending flagged)
    rows = _components("2025-10") + _components("2025-11", 1200.0) + _components("2025-12", 1300.0)
    m = compute_metrics(_table(rows), [])
    for month, end in (("2025-10", 1100.0), ("2025-11", 1200.0), ("2025-12", 1300.0)):
        r = _metric(m, f"acme.{month}.churn_rate")
        assert abs(r.value - 20 / (end - 200 - 50 + 30 + 20) * 100) < 1e-9


def test_reported_company_with_missing_beginning_is_blocked_with_reason():
    rows = ([("2025-10", "beginning_arr", 900.0, False)] + _components("2025-10")
            + _components("2025-11", 1200.0))          # 2025-11 beginning_arr cell missing
    review = [ReviewItem(CO, "2025-11", "beginning_arr", "continuity_break", "acme.csv", "B2",
                         "beginning does not match prior ending")]
    m = compute_metrics(_table(rows), review)
    r = _metric(m, "acme.2025-11.churn_rate")
    assert r.value is None
    assert isinstance(r.blocked_reason, str) and "continuity_break" in r.blocked_reason
    assert abs(_metric(m, "acme.2025-10.churn_rate").value - 20 / 900 * 100) < 1e-9
