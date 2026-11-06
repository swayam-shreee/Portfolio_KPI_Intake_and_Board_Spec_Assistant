"""Tests for the stage-4 metrics checker (not for any metric implementation).

The 'good' metrics frame is built from ground truth by eval/metrics_expected.py, with inputs
mapped to the cells of the stage-3 perfect table, so the checker is exercised in isolation.
"""
import pandas as pd
import pytest

from eval import metrics_expected as ME
from eval import score as S
from pipeline.contract import METRIC_COLUMNS
from tests.test_scorer import locate_all, perfect


@pytest.fixture(scope="module")
def key():
    return S.load_answer_key()


@pytest.fixture(scope="module")
def gt():
    return S.load_ground_truth()


@pytest.fixture(scope="module")
def table(key, gt):
    return perfect(locate_all(), key, gt).table


@pytest.fixture(scope="module")
def expected(gt, key):
    return ME.expected_all(gt, key)


@pytest.fixture(scope="module")
def good(expected, table):
    src = {(r.company, r.month, r.metric): (r.source_file, r.source_cell) for r in table.itertuples()}
    rows = []
    for mid, e in expected.items():
        if e.blocked:
            rows.append((e.company, e.month, mid, None, [],
                         "blocked by review item: " + ", ".join(f"{c} {m} {k}" for c, m, k in e.blockers)))
        else:
            rows.append((e.company, e.month, mid, e.accepted[0], [src[k] for k in e.inputs], None))
    return pd.DataFrame(rows, columns=list(METRIC_COLUMNS))


def check(metrics, table, gt, key):
    return S.section_metrics(metrics, table, gt, key)


def row(df, mid):
    return df.index[df.metric_id == mid][0]


# ---------------------------------------------------------------- the required three
def test_checker_passes_on_ground_truth_metrics(good, table, gt, key):
    res = check(good, table, gt, key)
    assert res["passed"], res
    assert res["n_correct"] == res["n_expected"] == 250
    assert res["sanity"]["passed"]


def test_wrong_value_fails(good, table, gt, key):
    m = good.copy()
    i = row(m, "ridgeline.12m.nrr_12m")
    m.loc[i, "value"] += 1.0
    res = check(m, table, gt, key)
    assert not res["passed"]
    assert [x["metric_id"] for x in res["mismatches"]] == ["ridgeline.12m.nrr_12m"]
    assert res["mismatches"][0]["why"] == "wrong value"


def test_zero_where_none_expected_fails(good, table, gt, key):
    m = good.copy()
    i = row(m, "harborview.12m.nrr_12m")
    assert pd.isna(m.loc[i, "value"])
    m.loc[i, "value"] = 0.0
    res = check(m, table, gt, key)
    assert not res["passed"]
    assert "must be None" in res["mismatches"][0]["why"]


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_missing_blocked_reason_fails(good, table, gt, key, reason):
    m = good.copy()
    i = row(m, "harborview.12m.nrr_12m")
    m.loc[i, "blocked_reason"] = reason
    res = check(m, table, gt, key)
    assert not res["passed"]
    assert res["mismatches"][0]["why"] == "blocked metric has no blocked_reason"


# ---------------------------------------------------------------- other behaviours
def test_none_where_value_expected_fails(good, table, gt, key):
    m = good.copy()
    m.loc[row(m, "ridgeline.2026-02.churn_rate"), "value"] = None
    assert "None where a value is expected" in check(m, table, gt, key)["mismatches"][0]["why"]


def test_input_not_in_table_fails(good, table, gt, key):
    m = good.copy()
    m.at[row(m, "ridgeline.12m.grr_12m"), "inputs"] = [("nope.xlsx", "A1")]
    res = check(m, table, gt, key)
    assert not res["passed"] and "inputs not in table" in res["mismatches"][0]["why"]


def test_missing_and_extra_ids_fail(good, table, gt, key):
    assert not check(good.iloc[1:], table, gt, key)["passed"]
    extra = good.copy()
    extra.loc[len(extra)] = ("Ridgeline Field Software", "12m", "ridgeline.12m.bogus", 1.0, [], None)
    assert not check(extra, table, gt, key)["passed"]


def test_tolerances(good, table, gt, key):
    m = good.copy()
    m.loc[row(m, "ridgeline.12m.nrr_12m"), "value"] += 0.09
    m.loc[row(m, "ridgeline.2026-02.arr_per_employee"), "value"] += 0.9
    assert check(m, table, gt, key)["passed"]


def test_cobalt_break_carry_is_accepted(expected):
    # the file carries the +150 break forward, so either variant of ending ARR is acceptable
    e = expected["cobalt.12m.growth_12m"]
    assert len(e.accepted) == 2 and abs(e.accepted[0] - e.accepted[1]) > 0.1


def test_formula_anchors(expected, gt):
    assert expected["cobalt.12m.nrr_12m"].accepted[0] == pytest.approx(15569 / 16500 * 100)
    assert expected["ridgeline.12m.growth_12m"].accepted[0] == pytest.approx((30407 / 24000 - 1) * 100)
    assert expected["verity.12m.growth_12m"].blocked  # Oct-25 beginning needs flagged new_arr
    assert ME.expected_unmasked(gt)["verity.12m.growth_12m"].accepted[0] == pytest.approx((13629 / 9800 - 1) * 100)
    assert expected["harborview.12m.nrr_12m"].blocked  # March missing
    assert not expected["harborview.12m.growth_12m"].blocked  # Oct beginning back-solved
    assert expected["cobalt.2026-04.churn_rate"].blocked  # flagged beginning_arr, never back-solved
    assert expected["harborview.2026-04.churn_rate"].accepted[0] == pytest.approx(60 / 12950 * 100)
    assert len(expected) == 250


def test_sanity_fails_if_ground_truth_changes(gt):
    g = gt.copy()
    g.loc[(g.company == "Cobalt Facilities") & (g.month == "2026-05"), "churned_arr"] = 0
    g.loc[(g.company == "Cobalt Facilities"), "expansion_arr"] += 500
    assert not S.metrics_sanity(g)["passed"]


def test_stub_compute_metrics_fails_stage4(table):
    res = S.score_metrics_stage(type("R", (), {"table": table, "review_queue": []})())
    assert not res["passed"] and "NotImplementedError" in res["metrics_error"]
