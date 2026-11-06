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
            rows.append((e.company, e.month, mid, e.value, [src[k] for k in e.inputs], None))
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
    assert res["assertions"]["passed"] and res["selftest"]["passed"]


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


def test_cobalt_expects_only_the_carry_forward_variant(expected, good, table, gt, key):
    # Cobalt's files report the +150 break from April on; that is the only accepted figure
    reported = (19618 + 150) / 16500 * 100 - 100
    true = 19618 / 16500 * 100 - 100
    assert expected["cobalt.12m.growth_12m"].value == pytest.approx(reported)
    assert expected["cobalt.2026-05.churn_rate"].value == pytest.approx(155 / (18284 + 150) * 100)
    m = good.copy()
    m.loc[row(m, "cobalt.12m.growth_12m"), "value"] = true  # the true figure is now rejected
    res = check(m, table, gt, key)
    assert not res["passed"]
    assert [x["metric_id"] for x in res["mismatches"]] == ["cobalt.12m.growth_12m"]


# Every flagged-input case. Rule (see pipeline.contract.compute_metrics): a flagged DERIVED value
# counts as absent and is back-solved from that month's components; a flagged REPORTED value
# blocks every metric that uses it.
FLAGGED_INPUT_CASES = [
    # (metric_id, why, expected value, blocking cell)
    ("harborview.2025-10.churn_rate", "derived beginning, no prior month: back-solved",
     80 / 11800 * 100, None),
    ("harborview.12m.growth_12m", "derived Oct-25 beginning (start): back-solved",
     (14180 / 11800 - 1) * 100, None),
    ("harborview.2026-04.churn_rate", "derived beginning, prior month (Mar) missing: back-solved",
     60 / 12950 * 100, None),
    ("harborview.2026-08.churn_rate", "derived beginning, prior ending (Jul, E2) flagged: back-solved",
     60 / 13830 * 100, None),
    ("verity.2025-10.churn_rate", "derived beginning, back-solve needs flagged reported new_arr: blocked",
     None, ("Verity Legal Tech", "2025-10", "new_arr")),
    ("cobalt.2026-04.churn_rate", "reported beginning_arr flagged (E5): blocked, never back-solved",
     None, ("Cobalt Facilities", "2026-04", "beginning_arr")),
]


@pytest.mark.parametrize("mid,why,value,blocker", FLAGGED_INPUT_CASES, ids=[c[0] for c in FLAGGED_INPUT_CASES])
def test_flagged_derived_vs_reported(expected, mid, why, value, blocker):
    e = expected[mid]
    if value is None:
        assert e.blocked and blocker in e.blockers, why
    else:
        assert not e.blocked and e.value == pytest.approx(value), why
        assert not e.blockers


def test_back_solve_cites_components_not_the_flagged_prior_ending(expected):
    cells = expected["harborview.2026-08.churn_rate"].inputs
    assert ("Harborview Compliance", "2026-07", "ending_arr") not in cells
    assert {k for _, _, k in cells} == {"churned_arr", "ending_arr", "new_arr", "expansion_arr",
                                        "contraction_arr"}


def test_blocked_reason_names_the_blocker(good):
    reason = good.set_index("metric_id").blocked_reason
    assert "new_arr" in reason["verity.12m.growth_12m"]
    assert "beginning_arr" in reason["cobalt.2026-04.churn_rate"]


def test_formula_anchors(expected, gt):
    assert expected["cobalt.12m.nrr_12m"].value == pytest.approx(15569 / 16500 * 100)
    assert expected["ridgeline.12m.growth_12m"].value == pytest.approx((30407 / 24000 - 1) * 100)
    assert expected["harborview.12m.nrr_12m"].blocked  # March missing (E1), reported
    assert expected["verity.12m.growth_12m"].blocked and expected["verity.12m.nrr_12m"].blocked
    assert expected["ridgeline.2026-02.arr_per_employee"].value == pytest.approx(26552 / 177)  # $K
    assert len(expected) == 250


# ---------------------------------------------------------------- pipeline-output assertions
def test_pipeline_assertions_pass_on_good_metrics(good):
    a = S.pipeline_assertions(good)
    assert a["passed"], a
    assert a["lowest_non_none_nrr_12m"][0] == "Cobalt Facilities" and a["lowest_non_none_nrr_12m"][1] < 100


@pytest.mark.parametrize("mid", ["verity.12m.growth_12m", "verity.12m.nrr_12m"])
def test_verity_reason_must_reference_new_arr(good, table, gt, key, mid):
    m = good.copy()
    m.loc[row(m, mid), "blocked_reason"] = "blocked by a review item"
    assert not S.pipeline_assertions(m)["passed"]
    assert not check(m, table, gt, key)["passed"]
    m.loc[row(m, mid), "blocked_reason"] = "Net New Logos ARR is an unknown_label under review"
    assert S.pipeline_assertions(m)["passed"]


def test_verity_value_must_be_none(good):
    m = good.copy()
    m.loc[row(m, "verity.12m.growth_12m"), "value"] = 39.07
    assert not S.pipeline_assertions(m)["passed"]


def test_cobalt_must_have_lowest_non_none_nrr(good):
    m = good.copy()
    m.loc[row(m, "ridgeline.12m.nrr_12m"), "value"] = 50.0
    a = S.pipeline_assertions(m)
    assert not a["passed"] and "lowest non-None nrr_12m is Ridgeline" in a["problems"][0]


def test_cobalt_nrr_must_be_below_100(good):
    m = good.copy()
    m.loc[row(m, "cobalt.12m.nrr_12m"), "value"] = 101.0
    for other in ("ridgeline", "northpass"):
        m.loc[row(m, f"{other}.12m.nrr_12m"), "value"] = 105.0
    a = S.pipeline_assertions(m)
    assert not a["passed"] and any("not below 100" in x for x in a["problems"])


def test_cobalt_nrr_none_fails(good):
    m = good.copy()
    m.loc[row(m, "cobalt.12m.nrr_12m"), "value"] = None
    assert not S.pipeline_assertions(m)["passed"]


# ---------------------------------------------------------------- checker self-tests
def test_selftest_unmasked_ground_truth_facts(gt):
    st = S.checker_selftest(gt)
    assert st["passed"]
    assert st["highest_growth_12m"][0] == "Verity Legal Tech"
    assert st["highest_growth_12m"][1] == pytest.approx(39.07, abs=0.01)
    assert st["lowest_nrr_12m"][0] == "Cobalt Facilities" and st["lowest_nrr_12m"][1] < 100


def test_selftest_fails_if_ground_truth_changes(gt):
    g = gt.copy()
    g.loc[(g.company == "Cobalt Facilities"), "expansion_arr"] += 500
    assert not S.checker_selftest(g)["passed"]


def test_stub_compute_metrics_fails_stage4(table):
    res = S.score_metrics_stage(type("R", (), {"table": table, "review_queue": []})())
    assert not res["passed"] and "NotImplementedError" in res["metrics_error"]
