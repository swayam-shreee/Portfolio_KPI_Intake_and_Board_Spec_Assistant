"""Stage 6 dashboard checker (locked). Tests 1-25 from docs/stage6_dashboard_checker_spec.md (rev 3).

Runs the real pipeline on data/raw/ through pipeline.review and drives the dashboard: the pure functions
in dashboard/ (views, uploads, export, approvals) and the four Streamlit pages through AppTest. Claude is
replaced by a fake client that returns canned fixtures from tests/fixtures/commentary/. Every count and
every expected value comes from live data; nothing about the queue or the metrics is hardcoded.

``dashboard.*`` and ``pipeline.llm`` are imported lazily inside tests and fixtures, so before the build each
test fails on its own instead of the whole file failing at collection. No test here is conditional: a
missing module is always a failure.

Where the spec cannot be tested exactly as written against streamlit 1.67.0 / pandas 3.1.1, the reading used
here is:
* ``FileUploader.upload()`` appends, so multi-file uploads (and replacing a list) use ``set_value([...])``.
* Test 8's ``.CSV`` copy is saved in a fresh state dir, where its bytes are new (a rename keeps the bytes).
* The docx sources table has a header row ``metric_id | display | source_file | source_cell``; the rest of
  the rows are the values of ``sources_rows``.
* "One st.text per reason" means: for each reason, the number of text elements containing it equals the
  number of times the draft rejected for it.
* The widget labels and help of every element are checked together with the listed element types (test 22).

Guards: pipeline/aliases.yaml and data/raw/* are restored in a ``finally`` after every test; ROOT/outputs/
is compared before and after every test (and across the module in test 25).
"""
from __future__ import annotations

import dataclasses
import glob
import hashlib
import importlib
import io
import json
import random
import re
import socket
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from pipeline import commentary as commentary_mod
from pipeline.commentary import Figure, Paragraph, format_value
from pipeline.contract import COMPANIES, ReviewItem

ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
REAL_ALIASES = ROOT / "pipeline" / "aliases.yaml"
FIXTURES = ROOT / "tests" / "fixtures" / "commentary"
RAW = sorted(glob.glob(str(RAW_DIR / "*.xlsx")) + glob.glob(str(RAW_DIR / "*.csv")))
RAW_NAMES = sorted(Path(p).name for p in RAW)
RAW_R = [p for p in RAW if "Ridgeline" not in Path(p).name]
VERITY, NORTHPASS, HARBORVIEW, COBALT, RIDGELINE = (
    "Verity Legal Tech", "Northpass Health IT", "Harborview Compliance", "Cobalt Facilities",
    "Ridgeline Field Software")
GUARDED = [REAL_ALIASES] + [Path(p) for p in RAW]
ATTEMPTS: list = []      # every refused connection attempt; a build that swallows the error is still caught

PAGES = ["app.py", "pages/1_Upload.py", "pages/2_Review_queue.py", "pages/3_Board_draft.py"]
OVERVIEW_COLS = ["company", "ending_arr", "growth_12m", "nrr_12m", "churn_rate", "gross_margin",
                 "open_items", "status"]
DETAIL_COLS = ["metric_id", "display", "status", "sources"]
SRC_COLS = ["metric_id", "display", "source_file", "source_cell"]
QUEUE_KEYS = {"item_id", "company", "month", "metric", "error_type", "source_file", "source_cell",
              "reason", "raw_label", "suggestion", "actions"}
D12_FIELDS = {"ts", "action", "draft_sha256", "draft_status", "model", "fingerprint",
              "open_question_count", "decision_count"}
D8_LABEL = {"unknown_label": "Confirm alias", "impossible_value": "Accept as reported",
            "duplicate_submission": "Close", "prompt_injection": "Close"}
NON_CONFIRMABLE = ("missing_month", "rollforward_mismatch", "continuity_break")
DASH = "—"
UNDER_REVIEW = "under review"
S = "**bold** <b>x</b> $1$ ![i](http://a/b.png) https://evil.example :smile:"
S_PARTS = ("**bold**", "<b>", "![i]", "evil.example", ":smile:")
COBALT_ONLY = json.dumps({"sections": [{"company": "cobalt", "paragraphs": [
    "Cobalt's net retention was {cobalt.12m.nrr_12m}."]}]})
ALLOWED_TYPES = {"text", "dataframe", "code"}


def fx(name: str) -> str:
    return (FIXTURES / name).read_text()


F1, F2 = fx("good.json"), fx("made_up_number.json")


# --------------------------------------------------------------------------- infrastructure
@pytest.fixture(scope="module", autouse=True)
def _block_network():
    """Refuse every real connection (AF_UNIX stays open: asyncio/pytest use it internally)."""
    def _refuse(*_a, **_k):
        ATTEMPTS.append("blocked network attempt")
        raise RuntimeError("network access is blocked in test_dashboard_checker")

    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def connect(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect(self, address)
        _refuse()

    def connect_ex(self, address):
        if self.family == socket.AF_UNIX:
            return real_connect_ex(self, address)
        _refuse()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(socket.socket, "connect", connect)
        mp.setattr(socket.socket, "connect_ex", connect_ex)
        mp.setattr(socket, "create_connection", _refuse)
        mp.setattr(socket, "getaddrinfo", _refuse)
        try:
            import anthropic
            mp.setattr(anthropic.Anthropic, "__init__", _refuse)
        except ImportError:
            pass
        yield


def outputs_snapshot():
    out = ROOT / "outputs"
    if not out.exists():
        return "absent"
    return {str(f.relative_to(out)): hashlib.sha256(f.read_bytes()).hexdigest()
            for f in sorted(out.rglob("*")) if f.is_file()}


def _restore(snap: dict) -> list:
    """Put back changed guarded files and remove anything added to data/raw; return what was wrong."""
    problems = [p for p, b in snap["bytes"].items() if not p.exists() or p.read_bytes() != b]
    for p in problems:
        p.write_bytes(snap["bytes"][p])
    for extra in [f for f in RAW_DIR.iterdir() if f not in snap["raw_listing"]]:
        problems.append(extra)
        if extra.is_dir():
            import shutil
            shutil.rmtree(extra)
        else:
            extra.unlink()
    return problems


@pytest.fixture(scope="module")
def real_files():
    return {"bytes": {p: p.read_bytes() for p in GUARDED}, "raw_listing": sorted(RAW_DIR.iterdir()),
            "outputs": outputs_snapshot()}


@pytest.fixture(autouse=True)
def _guard(real_files):
    """After every test: restore real files first (in a finally), then fail if anything was touched, if the
    default outputs/ folder changed, or if a refused network attempt was made that the test did not clear."""
    ATTEMPTS.clear()
    out_before = outputs_snapshot()
    try:
        yield
    finally:
        problems = _restore(real_files)
        attempts = list(ATTEMPTS)
        ATTEMPTS.clear()
        out_after = outputs_snapshot()
    assert not problems, f"a test changed real files: {problems}"
    assert not attempts, "a network attempt was made (and possibly swallowed)"
    assert out_after == out_before, "the default ROOT/outputs/ folder changed: the session cfg was not used"


def make_cfg(root):
    from pipeline.review import Config
    state = Path(root) / "state"
    return Config(data_dir=RAW_DIR, state_dir=state, aliases_path=state / "aliases.yaml",
                  seed_aliases_path=REAL_ALIASES)


@pytest.fixture
def cfg(tmp_path):
    return make_cfg(tmp_path)


@pytest.fixture
def port(cfg):
    from pipeline import review
    return review.load(cfg)


class FakeClient:
    """Returns canned raw text (a str repeats forever; a list is one per call). Records every call."""
    model = "fake-model"

    def __init__(self, responses):
        self._seq = None if isinstance(responses, str) else list(responses)
        self._one = responses if isinstance(responses, str) else None
        self.calls = []

    def create(self, system: str, user: str) -> str:
        self.calls.append((system, user))
        if self._seq is None:
            return self._one
        assert len(self.calls) <= len(self._seq), "fake client called more often than it has responses"
        return self._seq[len(self.calls) - 1]


# --------------------------------------------------------------------------- plain helpers
def slug(company: str) -> str:
    return str(company).split()[0].lower()


def blank(v) -> bool:
    return v is None or v != v


def find(p, error_type, company=None, metric=None):
    for it in p.review_queue:
        if (it.error_type == error_type and (company is None or it.company == company)
                and (metric is None or it.metric == metric)):
            return it
    raise AssertionError(f"no {error_type} item for {company} in the live queue")


def find_e7(p):
    for it in p.review_queue:
        if it.error_type == "unknown_label" and it.suggestion == "new_arr" and it.company == VERITY:
            return it
    raise AssertionError("E7 (Verity unknown_label with suggestion new_arr) is not in the live queue")


def find_e6(p):
    for it in p.review_queue:
        if (it.error_type == "impossible_value" and it.company == VERITY and it.metric == "gross_margin_pct"
                and it.month == "2026-06"):
            return it
    raise AssertionError("E6 (Verity impossible_value gross_margin_pct 2026-06) is not in the live queue")


def jsonl(path: Path) -> list:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()] if path.exists() else []


def decisions_lines(cfg) -> list:
    return jsonl(cfg.state_dir / "decisions.jsonl")


def approvals_lines(cfg) -> list:
    return jsonl(cfg.state_dir / "approvals.jsonl")


def frames_equal(a, b) -> bool:
    """The spec's Type-rule comparison: equal columns and equal string records, whatever the dtype."""
    return (list(a.columns) == list(b.columns)
            and a.astype(str).to_dict("records") == b.astype(str).to_dict("records"))


def assert_metrics_equal(a, b):
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True), check_dtype=False)


def records(df) -> list:
    return df.astype(str).to_dict("records")


def expected_sources(paragraph) -> list:
    """D10 rows, computed independently of dashboard.views."""
    rows = []
    for fig in paragraph.figures:
        if not fig.sources:
            rows.append({"metric_id": fig.metric_id, "display": fig.display, "source_file": DASH, "source_cell": DASH})
        for f, c in fig.sources:
            rows.append({"metric_id": fig.metric_id, "display": fig.display, "source_file": f, "source_cell": c})
    return rows


def sources_frame(paragraph) -> pd.DataFrame:
    return pd.DataFrame(expected_sources(paragraph), columns=SRC_COLS)


def expect_confirm(p, it) -> bool:
    from pipeline import review
    if it.error_type not in review.CONFIRMABLE:
        return False
    if it.error_type == "unknown_label":
        return bool(it.suggestion) and bool(review.raw_label(p, it))
    return True


def expected_actions(p) -> dict:
    """item_id -> {action key: label}, from the D8 table and the live queue."""
    from pipeline import review
    out = {}
    for it in p.review_queue:
        iid = review.item_id(it)
        d = {f"reject:{iid}": "Mark as question"}
        if expect_confirm(p, it):
            d[f"confirm:{iid}"] = D8_LABEL[it.error_type]
        out[iid] = d
    return out


def make_draft(p, log_path, responses=F1):
    return commentary_mod.draft_commentary(p.metrics, p.review_queue, FakeClient(responses), Path(log_path))


def draft_texts(draft) -> list:
    return [pa.rendered for s in draft.sections for pa in s.paragraphs] + [q.text for q in draft.questions]


def files_of(paths) -> list:
    return [(Path(x).name, Path(x).read_bytes()) for x in paths]


def mime_of(name: str) -> str:
    return "text/csv" if name.lower().endswith(".csv") else \
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def upload_paths(cfg) -> list:
    return sorted((cfg.state_dir / "uploads").glob("*"))


# --------------------------------------------------------------------------- AppTest helpers
_MISSING = object()


def make_app(page, cfg, client=None, portfolio=None):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / page), default_timeout=60)
    at.session_state["cfg"] = cfg
    if client is not None:
        at.session_state["claude_client"] = client
    if portfolio is not None:
        at.session_state["portfolio"] = portfolio
    return at


def ss(at, key, default=_MISSING):
    try:
        return at.session_state[key]
    except (KeyError, AttributeError):
        if default is _MISSING:
            raise AssertionError(f"session_state[{key!r}] is not set") from None
        return default


def widget(at, kind, key):
    found = [w for w in at.get(kind) if getattr(w, "key", None) == key]
    assert len(found) == 1, f"{kind} {key!r}: found {len(found)}; have {[getattr(w, 'key', None) for w in at.get(kind)]}"
    return found[0]


def has_widget(at, kind, key) -> bool:
    return any(getattr(w, "key", None) == key for w in at.get(kind))


def click(at, key):
    widget(at, "button", key).click()
    at.run()
    return at


def no_exception(at):
    assert not at.exception, f"the app raised: {[e.value for e in at.exception]}"


def leaves(at) -> list:
    """Every non-container node under main and sidebar, depth first, in page order."""
    nodes = list(at.main) + list(at.sidebar)
    return [n for n in nodes if not hasattr(n, "children")]


def containers(at) -> list:
    """Every block container (expander, tab, column, popover, form, ...) under main and sidebar."""
    return [n for n in list(at.main) + list(at.sidebar) if hasattr(n, "children")]


def text_values(at) -> list:
    return [n.value for n in leaves(at) if n.type == "text"]


def node_blob(n) -> str:
    parts = []
    v = getattr(n, "value", None)
    if isinstance(v, str):
        parts.append(v)
    elif isinstance(v, pd.DataFrame):
        parts.append(v.to_csv())
    if getattr(n, "proto", None) is not None:
        parts.append(str(n.proto))
    return "\n".join(parts)


def stale_shown(at) -> bool:
    return any(w.value.startswith("Out of date") for w in at.warning)


def open_page(at, page):
    at.switch_page(page).run()
    no_exception(at)
    return at


def parse_docx(data: bytes) -> list:
    import docx
    from docx.table import Table
    doc = docx.Document(io.BytesIO(data))
    out = []
    for blk in doc.iter_inner_content():
        if isinstance(blk, Table):
            out.append(("table", [[c.text for c in row.cells] for row in blk.rows]))
        else:
            out.append((blk.style.name, blk.text))
    return out


def expected_doc(draft, p) -> list:
    from dashboard import views
    out = [("Title", "Board draft")]
    for s in draft.sections:
        out.append(("Heading 1", views.section_name(s.company, p)))
        for pa in s.paragraphs:
            out.append(("Normal", pa.rendered))
            out.append(("table", [SRC_COLS] + [[r[c] for c in SRC_COLS] for r in expected_sources(pa)]))
    out.append(("Heading 1", "Open questions"))
    out += [("List Bullet", q.text) for q in draft.questions]
    return out


def with_s_item(p):
    """A copy of the portfolio with one extra open item whose text fields all carry S."""
    from pipeline import review
    it = ReviewItem(company=COBALT, month="2026-05", metric=None, error_type="unknown_label",
                    source_file="f_" + S + ".xlsx", source_cell="A1", reason="reason " + S,
                    suggestion="suggestion " + S)
    return dataclasses.replace(p, review_queue=list(p.review_queue) + [it],
                               raw_labels={**p.raw_labels, review.item_id(it): "label " + S})


def assert_s_only_in_text(at):
    """S may appear only in text / dataframe / code element values (labels and help included in the blob)."""
    for c in containers(at):                                        # expander / tab / column / popover / form labels
        blob = node_blob(c) + "\n" + str(getattr(c, "label", ""))
        for part in S_PARTS:
            assert part not in blob, f"{c.type} container label contains {part!r}: {blob[:200]!r}"
    allowed_blobs = []
    for n in leaves(at):
        if n.type in ALLOWED_TYPES:
            allowed_blobs.append(node_blob(n))
            continue
        blob = node_blob(n)
        for part in S_PARTS:
            assert part not in blob, f"{n.type} element contains {part!r}: {blob[:200]!r}"
    joined = "\n".join(allowed_blobs)
    assert "evil.example" in joined, "non-vacuity: the untrusted text must reach the page through text elements"


# =========================================================================== 1-11 pure functions
def expected_overview(p) -> dict:
    latest = p.months[-1]
    m = p.metrics.set_index("metric_id")["value"]

    def cell(mid):
        if mid not in m.index:
            return DASH
        v = m[mid]
        return UNDER_REVIEW if blank(v) else format_value(mid, float(v))

    out = {}
    for company in sorted(set(p.metrics["company"])):
        s = slug(company)
        t = p.table[(p.table["company"] == company) & (p.table["month"] == latest)
                    & (p.table["metric"] == "ending_arr")]
        arr = format_value(f"{s}.{latest}.ending_arr", float(t["value_usd_k"].iloc[0])) if len(t) else DASH
        n_open = sum(1 for i in p.review_queue if i.company == company)
        out[company] = {"company": company, "ending_arr": arr,
                        "growth_12m": cell(f"{s}.12m.growth_12m"), "nrr_12m": cell(f"{s}.12m.nrr_12m"),
                        "churn_rate": cell(f"{s}.{latest}.churn_rate"),
                        "gross_margin": cell(f"{s}.{latest}.gross_margin"),
                        "open_items": str(n_open), "status": "Needs review" if n_open > 0 else "Ready"}
    return out


def test_01_overview(port):
    from dashboard import views
    df = views.overview(port)
    assert list(df.columns) == OVERVIEW_COLS
    recs = df.to_dict("records")
    assert all(isinstance(v, str) for r in recs for v in r.values()), "every cell is a str (no None / NaN)"
    exp = expected_overview(port)
    assert len(recs) == len(exp) == len(set(port.metrics["company"]))
    assert {r["company"] for r in recs} == set(exp) and set(exp) == set(COMPANIES)
    for r in recs:
        assert r == exp[r["company"]], r["company"]
        assert (r["status"] == "Needs review") == (r["open_items"] != "0")
        assert re.fullmatch(r"\$\d+\.\dM", r["ending_arr"]), r["ending_arr"]
    assert exp[RIDGELINE]["status"] == "Ready" and exp[RIDGELINE]["open_items"] == "0"
    assert any(v == UNDER_REVIEW for r in recs for v in r.values()), "a blocked metric must read 'under review'"
    assert any(r["status"] == "Needs review" for r in recs)


def expected_detail(p) -> list:
    from pipeline import review
    accepted = review._accepted(p.decisions)
    cells = {(r.source_file, r.source_cell) for r in p.table.itertuples()
             if (r.company, r.month, r.metric) in accepted}
    out = []
    for r in p.metrics.itertuples():
        blocked = blank(r.value)
        display = UNDER_REVIEW if blocked else format_value(r.metric_id, float(r.value))
        if blocked:
            status = UNDER_REVIEW
        elif any(tuple(i) in cells for i in r.inputs):
            status = "accepted as reported"
        else:
            status = "ok"
        out.append({"metric_id": r.metric_id, "display": display, "status": status,
                    "sources": "; ".join(f"{f} {c}" for f, c in r.inputs) or DASH})
    return out


def check_detail(p, df):
    assert list(df.columns) == DETAIL_COLS
    recs = df.to_dict("records")
    assert all(isinstance(v, str) and v.strip() and v not in ("None", "nan", "NaN")
               for r in recs for v in r.values()), "every cell is a non-empty str, never None / nan"
    assert [r["metric_id"] for r in recs] == list(p.metrics["metric_id"]), "one row per metrics row, same order"
    assert recs == expected_detail(p)
    for r, row in zip(recs, p.metrics.itertuples()):
        assert (r["sources"] == DASH) == (len(row.inputs) == 0)
        if blank(row.value):
            assert r["display"] == UNDER_REVIEW and r["status"] == UNDER_REVIEW
    assert any(len(x) == 0 for x in p.metrics["inputs"]), "non-vacuity: a live row with empty inputs exists"
    assert any(blank(v) for v in p.metrics["value"])


def test_02_metric_detail(port, cfg):
    from dashboard import views
    from pipeline import review
    check_detail(port, views.metric_detail(port))
    p6 = review.resolve(port, review.item_id(find_e6(port)), "confirm", cfg)
    check_detail(p6, views.metric_detail(p6))                    # the same rules with an accepted value present
    assert any(r["status"] == "accepted as reported" for r in views.metric_detail(p6).to_dict("records"))


def test_03_queue_rows(port):
    from dashboard import views
    from pipeline import review
    rows = views.queue_rows(port)
    assert len(rows) == len(port.review_queue) > 0
    by_id = {r["item_id"]: r for r in rows}
    assert sorted(by_id) == sorted(review.item_id(i) for i in port.review_queue) and len(by_id) == len(rows)
    exp = expected_actions(port)
    for it in port.review_queue:
        iid = review.item_id(it)
        r = by_id[iid]
        assert QUEUE_KEYS <= set(r), f"queue row lacks {QUEUE_KEYS - set(r)}"
        assert (r["company"], r["error_type"], r["source_file"], r["source_cell"], r["reason"]) == (
            it.company, it.error_type, it.source_file, it.source_cell, it.reason)
        assert len(r["actions"]) == len(exp[iid]) and dict(r["actions"]) == exp[iid]
        assert r["month"] == (it.month if it.month else DASH) and r["metric"] == (it.metric if it.metric else DASH)
        rl = review.raw_label(port, it)
        assert r["raw_label"] == (rl if rl else DASH)
        assert r["suggestion"] == (it.suggestion if it.suggestion else DASH)
    e7 = find_e7(port)
    assert by_id[review.item_id(e7)]["raw_label"] == review.raw_label(port, e7) != DASH
    assert any(r["raw_label"] == DASH for r in rows) and any(r["suggestion"] == DASH for r in rows)
    assert any(i.month is None for i in port.review_queue) or any(i.metric is None for i in port.review_queue), \
        "non-vacuity: a live item has a missing month or metric"
    assert any(r["month"] != DASH for r in rows) and any(r["metric"] != DASH for r in rows)

    # an unknown_label that cannot be learned (no suggestion, or no raw label) never gets a Confirm
    def extra(source_cell, suggestion):
        return ReviewItem(company=VERITY, month=None, metric=None, error_type="unknown_label",
                          source_file="x.csv", source_cell=source_cell, reason="r", suggestion=suggestion)

    no_sugg, no_label, learnable = extra("Z1", None), extra("Z2", "new_arr"), extra("Z3", "new_arr")
    fake = dataclasses.replace(
        port, review_queue=list(port.review_queue) + [no_sugg, no_label, learnable],
        raw_labels={**port.raw_labels, review.item_id(no_sugg): "Some label", review.item_id(learnable): "Other label"})
    got = {r["item_id"]: dict(r["actions"]) for r in views.queue_rows(fake)}
    assert set(got[review.item_id(no_sugg)]) == {f"reject:{review.item_id(no_sugg)}"}
    assert set(got[review.item_id(no_label)]) == {f"reject:{review.item_id(no_label)}"}
    assert set(got[review.item_id(learnable)]) == {f"reject:{review.item_id(learnable)}",
                                                   f"confirm:{review.item_id(learnable)}"}
    rows2 = {r["item_id"]: r for r in views.queue_rows(fake)}
    assert rows2[review.item_id(no_sugg)]["suggestion"] == DASH
    assert rows2[review.item_id(no_label)]["raw_label"] == DASH


def test_04_labels(port):
    from dashboard import views
    from pipeline import review
    assert set(review.CONFIRMABLE) == set(D8_LABEL)
    rows = views.queue_rows(port)
    types_with_confirm, types_without = set(), set()
    for r in rows:
        acts = dict(r["actions"])
        assert acts[f"reject:{r['item_id']}"] == "Mark as question"
        confirm = [k for k in acts if k.startswith("confirm:")]
        if r["error_type"] in review.CONFIRMABLE:
            assert confirm == [f"confirm:{r['item_id']}"], r["error_type"]
            assert acts[confirm[0]] == D8_LABEL[r["error_type"]]
            types_with_confirm.add(r["error_type"])
        else:
            assert not confirm, f"{r['error_type']} must not offer Confirm"
            types_without.add(r["error_type"])
    assert types_with_confirm == set(D8_LABEL), "all four confirmable types occur in the live queue"
    assert types_without == set(NON_CONFIRMABLE), "all three non-confirmable types occur in the live queue"


def test_05_confirm_e7(port, cfg):
    from dashboard import views
    from pipeline import review
    e7 = find_e7(port)
    before = views.overview(port).set_index("company")
    assert before.loc[VERITY, "growth_12m"] == UNDER_REVIEW
    under0 = (views.metric_detail(port)["display"].astype(str) == UNDER_REVIEW).sum()
    p1 = review.resolve(port, review.item_id(e7), "confirm", cfg)
    value = p1.metrics.set_index("metric_id")["value"]["verity.12m.growth_12m"]
    assert not blank(value)
    after = views.overview(p1).set_index("company")
    assert after.loc[VERITY, "growth_12m"] == format_value("verity.12m.growth_12m", float(value))
    assert review.item_id(e7) not in [r["item_id"] for r in views.queue_rows(p1)]
    under1 = (views.metric_detail(p1)["display"].astype(str) == UNDER_REVIEW).sum()
    assert under1 < under0


def test_06_accept_e6(port, cfg):
    from dashboard import views
    from pipeline import review
    mid = "verity.2026-06.gross_margin"
    d0 = views.metric_detail(port).set_index("metric_id")
    assert d0.loc[mid, "status"] == UNDER_REVIEW
    p1 = review.resolve(port, review.item_id(find_e6(port)), "confirm", cfg)
    d1 = views.metric_detail(p1).set_index("metric_id")
    changed = [m for m in d0.index if d0.loc[m, "status"] != d1.loc[m, "status"]]
    assert changed == [mid], f"only {mid} may change status, got {changed}"
    value = p1.metrics.set_index("metric_id")["value"][mid]
    assert not blank(value)
    assert d1.loc[mid, "status"] == "accepted as reported"
    assert d1.loc[mid, "display"] == format_value(mid, float(value))


def test_07_section_names_and_sources_rows(port, tmp_path):
    from dashboard import views
    assert views.section_name("portfolio", port) == "Portfolio"
    assert views.section_name("cobalt", port) == COBALT
    assert views.section_name(S, port) is None
    assert views.section_name("nosuchcompany", port) is None
    draft = make_draft(port, tmp_path / "log.jsonl")
    assert draft.status == "ok"
    total = 0
    for s in draft.sections:
        for pa in s.paragraphs:
            rows = views.sources_rows(pa)
            assert rows == expected_sources(pa) and all(set(r) == set(SRC_COLS) for r in rows)
            total += len(rows)
    assert total >= 7, "every F1 figure has at least one source row"
    bare = Paragraph("{x.12m.y}", "1.0%", [Figure("x.12m.y", "1.0%", [])])
    assert views.sources_rows(bare) == [{"metric_id": "x.12m.y", "display": "1.0%",
                                         "source_file": DASH, "source_cell": DASH}]


def test_08_uploads(cfg, tmp_path):
    from dashboard import uploads
    from pipeline import review
    up = cfg.state_dir / "uploads"
    originals = dict(files_of(RAW))
    res = uploads.save_uploads(cfg, files_of(RAW))
    assert sorted(res.saved) == RAW_NAMES and len(res.saved) == len(RAW) and not res.skipped and not res.refused
    for name, data in originals.items():
        assert (up / name).read_bytes() == data
    res = uploads.save_uploads(cfg, files_of(RAW))
    assert sorted(res.skipped) == RAW_NAMES and not res.saved and not res.refused

    first = RAW_NAMES[0]
    xlsx = next(n for n in RAW_NAMES if n.endswith(".xlsx"))
    junk = random.Random(0).randbytes(2048)
    batch = [(first, originals[first] + b"x"),                      # same name, different bytes
             ("renamed_copy.xlsx", originals[xlsx]),                # different name, same bytes
             ("notes.txt", b"hello"),
             ("../evil.txt", b"hello"),                              # bad_name is checked before bad_type
             ("../x.csv", b"a,b\n"), ("a/b.csv", b"a,b\n"), (".hidden.csv", b"a,b\n"),
             ("a\\b.csv", b"a,b\n"), ("x..csv", b"a,b\n"), ("", b"a,b\n"),
             ("junk.xlsx", junk), ("junk.csv", b"a,b\n1,2\n")]
    res = uploads.save_uploads(cfg, batch)
    assert not res.saved and not res.skipped
    got = dict(res.refused)
    assert len(got) == len(res.refused) == len(batch)
    assert got == {first: "name_conflict", "renamed_copy.xlsx": "duplicate_content", "notes.txt": "bad_type",
                   "../evil.txt": "bad_name", "../x.csv": "bad_name", "a/b.csv": "bad_name",
                   ".hidden.csv": "bad_name", "a\\b.csv": "bad_name", "x..csv": "bad_name", "": "bad_name",
                   "junk.xlsx": "unreadable", "junk.csv": "unreadable"}
    assert (up / first).read_bytes() == originals[first], "a same-name file must not be overwritten"
    assert not (up / "renamed_copy.xlsx").exists() and not (up / "notes.txt").exists()
    for name in ("junk.xlsx", "junk.csv"):
        assert not (up / name).exists() and not (cfg.state_dir / "incoming" / name).exists()
    incoming = cfg.state_dir / "incoming"
    assert not incoming.exists() or not list(incoming.iterdir()), "incoming/ is left empty"
    assert not list(tmp_path.rglob("x.csv")) and not list(tmp_path.rglob("evil.txt")) \
        and not list(tmp_path.rglob("b.csv")) and not list(tmp_path.rglob(".hidden.csv"))
    assert sorted(f.name for f in up.iterdir()) == RAW_NAMES, "uploads/ holds exactly the accepted files"

    p = review.load(cfg, upload_paths(cfg))                          # an unreadable file never reaches uploads/
    assert set(p.table["company"]) == set(COMPANIES)

    # a batch with a duplicate inside it, in a fresh state; and an upper-case extension
    cfg_b = make_cfg(tmp_path / "b")
    verity = next(n for n in RAW_NAMES if n.endswith(".csv"))
    assert "verity" in verity.lower()
    res = uploads.save_uploads(cfg_b, [("verity_a.csv", originals[verity]), ("verity_b.csv", originals[verity])])
    assert res.saved == ["verity_a.csv"] and dict(res.refused) == {"verity_b.csv": "duplicate_content"}
    cfg_d = make_cfg(tmp_path / "d")                                 # a stem without a company name is unreadable
    res = uploads.save_uploads(cfg_d, [("a.csv", originals[verity])])
    assert not res.saved and dict(res.refused) == {"a.csv": "unreadable"}
    cfg_c = make_cfg(tmp_path / "c")
    res = uploads.save_uploads(cfg_c, [("verity_copy.CSV", originals[verity])])
    assert dict(res.refused).get("verity_copy.CSV") != "bad_type"
    assert res.saved == ["verity_copy.CSV"] and not res.refused
    assert (cfg_c.state_dir / "uploads" / "verity_copy.CSV").read_bytes() == originals[verity]


def test_09_export(port, tmp_path):
    from dashboard import export
    draft = make_draft(port, tmp_path / "log.jsonl")
    assert draft.status == "ok" and draft.questions
    data = export.draft_to_docx(draft, port)
    assert isinstance(data, (bytes, bytearray))
    parsed = parse_docx(bytes(data))
    assert parsed == expected_doc(draft, port)
    assert sum(1 for k, _ in parsed if k == "Title") == 1
    assert sum(1 for k, _ in parsed if k == "table") == sum(len(s.paragraphs) for s in draft.sections)
    for kind, payload in parsed:
        cells = [c for row in payload for c in row] if kind == "table" else [payload]
        assert not any("{" in c or "}" in c for c in cells), "no unresolved reference in the document"


def test_10_approvals(port, cfg, tmp_path):
    from dashboard import approvals
    from pipeline import review
    p1 = review.resolve(port, review.item_id(find_e7(port)), "confirm", cfg)
    assert len(p1.decisions) == 1
    draft = make_draft(p1, tmp_path / "log.jsonl")
    assert draft.questions
    before = approvals_lines(cfg)
    r1 = approvals.record(cfg, "approved", draft, p1, "fake-model", "fp-123")
    r2 = approvals.record(cfg, "rejected", draft, p1, "fake-model", "fp-123")
    lines = approvals_lines(cfg)
    assert len(lines) == len(before) + 2 == 2
    assert [l["action"] for l in lines] == ["approved", "rejected"]
    assert lines == [r1, r2], "record() returns the line it wrote"
    expected_sha = hashlib.sha256(json.dumps(
        [[s.company, [pa.rendered for pa in s.paragraphs]] for s in draft.sections]
        + [q.text for q in draft.questions]).encode()).hexdigest()
    for l in lines:
        assert set(l) == D12_FIELDS
        assert l["open_question_count"] == len(draft.questions) and l["decision_count"] == len(p1.decisions) == 1
        assert l["model"] == "fake-model" and l["fingerprint"] == "fp-123" and l["draft_status"] == draft.status
        assert l["draft_sha256"] == expected_sha
        ts = datetime.fromisoformat(l["ts"])
        assert ts.tzinfo is not None and ts.utcoffset() == timedelta(0), "ts is offset-aware UTC ISO (a naive ts fails)"
    other = commentary_mod.draft_commentary(p1.metrics, p1.review_queue, FakeClient(COBALT_ONLY),
                                            tmp_path / "log2.jsonl")
    approvals.record(cfg, "approved", other, p1, "fake-model", "fp-123")
    assert approvals_lines(cfg)[-1]["draft_sha256"] != expected_sha, "the hash follows the draft content"
    text = (cfg.state_dir / "approvals.jsonl").read_text()
    for s in [pa.rendered for sec in draft.sections for pa in sec.paragraphs] \
            + [pa.template for sec in draft.sections for pa in sec.paragraphs] \
            + [q.text for q in draft.questions] + [i.reason for i in p1.review_queue]:
        assert s not in text, f"approvals.jsonl must not store text: {s[:40]!r}"


def test_11_fingerprint(cfg, tmp_path):
    from dashboard import views
    from pipeline import review
    p0 = review.load(cfg)
    fp0 = views.fingerprint(p0)
    assert isinstance(fp0, str) and re.fullmatch(r"[0-9a-f]{64}", fp0)
    assert views.fingerprint(review.load(cfg)) == fp0, "stable across two loads"
    p1 = review.resolve(p0, review.item_id(find_e7(p0)), "confirm", cfg)
    assert views.fingerprint(p1) != fp0
    assert views.fingerprint(review.load(cfg)) == views.fingerprint(p1), "a reload replays to the same fingerprint"

    cfg_r = make_cfg(tmp_path / "r")
    q0 = review.load(cfg_r)
    q1 = review.resolve(q0, review.item_id(find(q0, "missing_month", HARBORVIEW)), "reject", cfg_r)
    assert views.fingerprint(q1) == views.fingerprint(q0), "a reject changes neither metrics nor open items"

    for error_type in ("prompt_injection", "duplicate_submission"):
        c = make_cfg(tmp_path / error_type)
        a = review.load(c)
        b = review.resolve(a, review.item_id(find(a, error_type)), "confirm", c)
        assert_metrics_equal(a.metrics, b.metrics)                   # nothing in the metrics moved...
        assert len(b.review_queue) == len(a.review_queue) - 1
        assert views.fingerprint(a) != views.fingerprint(b), f"...but closing a {error_type} item is stale"

    c2 = make_cfg(tmp_path / "rawr")
    assert views.fingerprint(review.load(c2, paths=RAW_R)) != fp0, "RAW-R and RAW differ"


# =========================================================================== 12-22 AppTest
@pytest.mark.parametrize("with_portfolio", [False, True], ids=["no-portfolio", "portfolio"])
@pytest.mark.parametrize("page", PAGES)
def test_12_every_page_runs(page, with_portfolio, cfg):
    from pipeline import review
    p = review.load(cfg) if with_portfolio else None
    at = make_app(page, cfg, portfolio=p)                           # no claude_client: it must not be built eagerly
    at.run()
    no_exception(at)
    assert leaves(at), f"{page} rendered nothing (an empty page does not count as running)"
    if page == "pages/1_Upload.py":
        assert has_widget(at, "file_uploader", "uploads") and has_widget(at, "button", "load_sample")
    elif not with_portfolio:
        assert len(at.info) >= 1, "a literal st.info is shown without a portfolio"
        if page == "app.py":
            assert any("Upload" in i.value for i in at.info)
        else:
            assert not list(at.button) and not at.get("download_button"), "no buttons without a portfolio"
    else:
        if page == "app.py":
            assert len(at.dataframe) >= 2
        elif page == "pages/2_Review_queue.py":
            assert any((b.key or "").startswith("reject:") for b in at.button)
        else:
            assert has_widget(at, "button", "generate")
    if with_portfolio:
        assert ss(at, "portfolio") is p


def test_12b_switch_page_smoke(cfg, port):
    at = make_app("app.py", cfg, client=FakeClient(F1), portfolio=port)
    at.run()
    no_exception(at)
    assert leaves(at)
    for page in PAGES[1:]:
        open_page(at, page)
        assert leaves(at), f"{page} rendered nothing"
    open_page(at, "app.py")
    assert len(at.dataframe) >= 2
    assert ss(at, "portfolio") is port


def test_12c_reload_from_uploads(cfg):
    from dashboard import uploads
    from pipeline import review
    assert uploads.save_uploads(cfg, files_of(RAW)).saved
    paths = upload_paths(cfg)
    p0 = review.load(cfg, paths)
    e7_id = review.item_id(find_e7(p0))
    review.resolve(p0, e7_id, "confirm", cfg)                        # the reload must replay this decision
    at = make_app("app.py", cfg)                                     # a fresh session: no portfolio
    at.run()
    no_exception(at)
    got = ss(at, "portfolio")
    expected = review.load(cfg, paths)
    assert_metrics_equal(got.metrics, expected.metrics)
    assert e7_id not in {review.item_id(i) for i in got.review_queue}
    assert e7_id not in {review.item_id(i) for i in expected.review_queue}
    assert len(got.decisions) == len(expected.decisions) == 1
    assert len(at.dataframe) >= 2


def test_13_overview_wiring(cfg, port):
    from dashboard import views
    at = make_app("app.py", cfg, portfolio=port)
    at.run()
    no_exception(at)
    dfs = list(at.dataframe)
    assert len(dfs) >= 2
    assert frames_equal(dfs[0].value, views.overview(port))
    assert frames_equal(dfs[1].value, views.metric_detail(port))
    assert any(port.months[0] in t and port.months[-1] in t for t in text_values(at)), "the period is shown as text"


def test_14_review_buttons(cfg, port, tmp_path):
    from pipeline import review
    cfg2 = make_cfg(tmp_path / "expected")
    p2 = review.load(cfg2)
    expected = review.resolve(p2, review.item_id(find_e7(p2)), "confirm", cfg2)
    e7_id = review.item_id(find_e7(port))
    assert e7_id == review.item_id(find_e7(p2))

    at = make_app("pages/2_Review_queue.py", cfg, portfolio=port)
    at.run()
    no_exception(at)
    exp = {k: v for d in expected_actions(port).values() for k, v in d.items()}
    got = {b.key: b.label for b in at.button if b.key and b.key.startswith(("confirm:", "reject:"))}
    assert got == exp, "buttons are exactly the D8 actions with the D8 labels"
    assert not any(f"confirm:{review.item_id(i)}" in got for i in port.review_queue
                   if i.error_type in NON_CONFIRMABLE)
    assert decisions_lines(cfg) == []

    click(at, f"confirm:{e7_id}")
    no_exception(at)
    lines = decisions_lines(cfg)
    assert len(lines) == 1 and lines[0]["action"] == "confirm" and lines[0]["item_id"] == e7_id
    assert_metrics_equal(ss(at, "portfolio").metrics, expected.metrics)
    assert not has_widget(at, "button", f"confirm:{e7_id}") and not has_widget(at, "button", f"reject:{e7_id}")


def test_14b_resolve_error_wrapper(cfg, port):
    from pipeline import review
    # a company the Portfolio doesn't know: D8 still offers Confirm (impossible_value), resolve raises KeyError
    ghost = ReviewItem(company="Nonexistent Co", month="2026-05", metric="gross_margin_pct",
                       error_type="impossible_value", source_file="nowhere.xlsx", source_cell="Z99",
                       reason="r", suggestion=None)
    gid = review.item_id(ghost)
    assert gid not in {review.item_id(i) for i in port.review_queue}
    with pytest.raises(KeyError):
        review.resolve(dataclasses.replace(port, review_queue=list(port.review_queue) + [ghost]), gid, "confirm", cfg)
    assert decisions_lines(cfg) == []
    ps = dataclasses.replace(port, review_queue=list(port.review_queue) + [ghost])
    at = make_app("pages/2_Review_queue.py", cfg, portfolio=ps)
    at.run()
    no_exception(at)
    assert widget(at, "button", f"confirm:{gid}").label == "Accept as reported"
    assert not at.error
    click(at, f"confirm:{gid}")
    no_exception(at)
    assert [e.value for e in at.error] == ["This item can't be resolved here."]
    assert ss(at, "portfolio") is ps, "the session portfolio is unchanged"
    assert decisions_lines(cfg) == []


def test_15_mark_as_question(cfg, port):
    from pipeline import review
    e7_id = review.item_id(find_e7(port))
    mm_id = review.item_id(find(port, "missing_month", HARBORVIEW))
    at = make_app("pages/2_Review_queue.py", cfg, portfolio=port)
    at.run()
    click(at, f"reject:{e7_id}")
    no_exception(at)
    lines = decisions_lines(cfg)
    assert [(l["action"], l["item_id"]) for l in lines] == [("reject", e7_id)]
    assert has_widget(at, "button", f"reject:{e7_id}"), "the item is still listed after Mark as question"
    assert e7_id in {review.item_id(i) for i in ss(at, "portfolio").review_queue}
    click(at, f"reject:{mm_id}")                                     # also valid on an item Confirm refuses
    assert [(l["action"], l["item_id"]) for l in decisions_lines(cfg)] == [("reject", e7_id), ("reject", mm_id)]
    assert has_widget(at, "button", f"reject:{mm_id}")


def test_16_upload_page(cfg, tmp_path):
    from pipeline import review
    at = make_app("pages/1_Upload.py", cfg)
    at.run()
    no_exception(at)
    widget(at, "file_uploader", "uploads").set_value([(n, b, mime_of(n)) for n, b in files_of(RAW)])
    at.run()
    no_exception(at)
    res = ss(at, "upload_result")
    assert sorted(res.saved) == RAW_NAMES and not res.skipped and not res.refused
    expected = review.load(make_cfg(tmp_path / "expected"), upload_paths(cfg))
    got = ss(at, "portfolio")
    assert_metrics_equal(got.metrics, expected.metrics)
    assert sorted(got.review_queue, key=repr) == sorted(expected.review_queue, key=repr)
    at.run()                                                        # same uploader files again
    no_exception(at)
    res2 = ss(at, "upload_result")
    assert sorted(res2.skipped) == RAW_NAMES and not res2.saved and not res2.refused
    assert_metrics_equal(ss(at, "portfolio").metrics, expected.metrics)


def test_16b_load_sample(cfg, tmp_path):
    from pipeline import review
    at = make_app("pages/1_Upload.py", cfg)
    at.run()
    no_exception(at)
    click(at, "load_sample")
    no_exception(at)
    expected = review.load(make_cfg(tmp_path / "expected"))
    got = ss(at, "portfolio")
    assert_metrics_equal(got.metrics, expected.metrics)
    assert sorted(got.review_queue, key=repr) == sorted(expected.review_queue, key=repr)
    up = cfg.state_dir / "uploads"
    assert not up.exists() or not list(up.iterdir()), "Load sample data saves nothing into uploads/"


def generate(at):
    click(at, "generate")
    no_exception(at)
    return at


def draft_page(cfg, client, port):
    at = make_app("pages/3_Board_draft.py", cfg, client=client, portfolio=port)
    at.run()
    no_exception(at)
    return at


def test_17_generate(cfg, port, tmp_path):
    from dashboard import views
    fake = FakeClient(F1)
    at = draft_page(cfg, fake, port)
    assert has_widget(at, "button", "generate") and not has_widget(at, "button", "regenerate")
    assert len(fake.calls) == 0, "nothing is generated before the click"
    generate(at)
    assert len(fake.calls) == 1
    log = jsonl(cfg.state_dir / "commentary_log.jsonl")
    assert len(log) == 1 and log[0]["model"] == "fake-model"
    draft = ss(at, "draft")
    assert draft.status == "ok"
    ref = make_draft(port, tmp_path / "ref.jsonl")
    assert draft_texts(draft) == draft_texts(ref), "the page drafts from the session portfolio's metrics and queue"
    assert f"Draft status: ok · attempts {draft.attempts}" in text_values(at)
    seq = leaves(at)
    pos = 0
    for s in draft.sections:
        for pa in s.paragraphs:
            idx = next((i for i in range(pos, len(seq)) if seq[i].type == "text" and seq[i].value == pa.rendered), None)
            assert idx is not None, f"paragraph is not a text element: {pa.rendered[:50]!r}"
            nxt = seq[idx + 1]
            assert nxt.type == "dataframe" and frames_equal(nxt.value, sources_frame(pa)), "sources follow the paragraph"
            pos = idx + 2
    texts = text_values(at)
    for q in draft.questions:
        assert q.text in texts
    assert [s.value for s in at.subheader] == [views.section_name(s.company, port) or "Other"
                                               for s in draft.sections] + ["Open questions"]
    assert has_widget(at, "button", "regenerate") and not has_widget(at, "button", "generate")


def test_18_rejected_draft(cfg, port):
    fake = FakeClient(F2)
    at = draft_page(cfg, fake, port)
    generate(at)
    draft = ss(at, "draft")
    assert draft.status == "rejected" and list(draft.reject_reasons) == ["made_up_number"] * 3
    texts = text_values(at)
    assert f"Draft status: rejected · attempts {draft.attempts}" in texts
    assert any(e.value == "Draft rejected" for e in at.error)
    for reason in set(draft.reject_reasons):
        assert sum(1 for t in texts if reason in t) == draft.reject_reasons.count(reason), \
            "one text element per reason"
    pieces = []
    for sec in json.loads(F2)["sections"]:
        for para in sec["paragraphs"]:
            pieces += [para] + [x.strip() for x in re.split(r"\{[^{}]*\}", para) if len(x.strip()) >= 8]
    assert pieces
    blobs = [node_blob(n) for n in list(at.main) + list(at.sidebar)]
    for piece in pieces:
        assert not any(piece in b for b in blobs), f"rejected text leaked into the page: {piece!r}"
    assert widget(at, "button", "approve").proto.disabled
    for d in at.get("download_button"):
        assert d.proto.disabled


def test_19_regenerate_approve_reject(cfg, port):
    from dashboard import views
    fake = FakeClient(F1)
    at = draft_page(cfg, fake, port)
    generate(at)
    assert len(fake.calls) == 1
    assert not widget(at, "button", "approve").proto.disabled
    click(at, "approve")
    no_exception(at)
    lines = approvals_lines(cfg)
    assert len(lines) == 1 and lines[0]["action"] == "approved"
    assert lines[0]["fingerprint"] == ss(at, "draft_fingerprint") == views.fingerprint(ss(at, "portfolio"))
    assert lines[0]["model"] == ss(at, "draft_model") == "fake-model"
    assert ss(at, "approved") is True
    assert widget(at, "button", "approve").proto.disabled, "Approve is disabled after one click"
    at.run()                                                        # a further rerun must not approve again
    no_exception(at)
    assert [l["action"] for l in approvals_lines(cfg)] == ["approved"], "exactly one approved line after a rerun"

    click(at, "regenerate")
    no_exception(at)
    assert len(fake.calls) == 2, "Regenerate makes exactly one more call"
    assert ss(at, "approved") is False
    assert len(approvals_lines(cfg)) == 1

    click(at, "reject_draft")
    no_exception(at)
    lines = approvals_lines(cfg)
    assert [l["action"] for l in lines] == ["approved", "rejected"]
    assert lines[1]["fingerprint"] == ss(at, "draft_fingerprint") and lines[1]["model"] == "fake-model"
    assert ss(at, "draft") is None and ss(at, "approved") is False
    assert has_widget(at, "button", "generate") and not has_widget(at, "button", "regenerate")
    assert len(fake.calls) == 2


def _generate_and_approve_from_app(cfg, port, fake):
    at = make_app("app.py", cfg, client=fake, portfolio=port)
    at.run()
    no_exception(at)
    open_page(at, "pages/3_Board_draft.py")
    generate(at)
    assert not stale_shown(at) and not widget(at, "button", "approve").proto.disabled
    click(at, "approve")
    no_exception(at)
    assert ss(at, "approved") is True
    return at


def test_20a_stale_after_confirm(cfg, port):
    from pipeline import review
    e7_id = review.item_id(find_e7(port))
    at = _generate_and_approve_from_app(cfg, port, FakeClient(F1))
    open_page(at, "pages/2_Review_queue.py")
    click(at, f"confirm:{e7_id}")
    no_exception(at)
    open_page(at, "pages/3_Board_draft.py")
    assert stale_shown(at), "Out of date warning after a confirm"
    assert widget(at, "button", "approve").proto.disabled
    assert widget(at, "download_button", "export").proto.disabled
    click(at, "regenerate")
    no_exception(at)
    assert not stale_shown(at)
    assert not widget(at, "button", "approve").proto.disabled


def test_20b_stale_after_upload(cfg):
    from pipeline import review
    fake = FakeClient(COBALT_ONLY)
    at = make_app("app.py", cfg, client=fake)
    at.run()
    no_exception(at)
    open_page(at, "pages/1_Upload.py")
    widget(at, "file_uploader", "uploads").set_value([(n, b, mime_of(n)) for n, b in files_of(RAW_R)])
    at.run()
    no_exception(at)
    assert sorted(ss(at, "upload_result").saved) == sorted(Path(x).name for x in RAW_R)
    open_page(at, "pages/3_Board_draft.py")
    generate(at)
    assert ss(at, "draft").status == "ok"
    assert not stale_shown(at) and not widget(at, "button", "approve").proto.disabled
    open_page(at, "pages/1_Upload.py")
    widget(at, "file_uploader", "uploads").set_value([(n, b, mime_of(n)) for n, b in files_of(RAW)])
    at.run()
    no_exception(at)
    assert {Path(x).name for x in ss(at, "upload_result").saved} == set(RAW_NAMES) - {Path(x).name for x in RAW_R}
    assert RIDGELINE in set(ss(at, "portfolio").metrics["company"])
    open_page(at, "pages/3_Board_draft.py")
    assert stale_shown(at), "the draft was made before Ridgeline was uploaded"
    assert widget(at, "button", "approve").proto.disabled


def test_20c_reject_is_not_stale(cfg, port):
    from pipeline import review
    e7_id = review.item_id(find_e7(port))
    at = _generate_and_approve_from_app(cfg, port, FakeClient(F1))
    open_page(at, "pages/2_Review_queue.py")
    click(at, f"reject:{e7_id}")
    no_exception(at)
    assert [l["action"] for l in decisions_lines(cfg)] == ["reject"]
    open_page(at, "pages/3_Board_draft.py")
    assert not stale_shown(at), "Mark as question changes neither metrics nor open items"
    assert not widget(at, "download_button", "export").proto.disabled, "approved and not stale: export is enabled"


def test_21_export_button(cfg, port, monkeypatch):
    import streamlit
    from dashboard import export
    captured = []
    orig = streamlit.download_button

    def capture(*a, **k):
        captured.append((k["data"] if "data" in k else (a[1] if len(a) > 1 else None),
                         k["file_name"] if "file_name" in k else (a[2] if len(a) > 2 else None), k.get("key")))
        return orig(*a, **k)

    monkeypatch.setattr(streamlit, "download_button", capture)
    at = draft_page(cfg, FakeClient(F1), port)
    generate(at)
    assert widget(at, "download_button", "export").proto.disabled, "Export is disabled before Approve"
    click(at, "approve")
    no_exception(at)
    assert not widget(at, "download_button", "export").proto.disabled
    assert captured and isinstance(captured[-1][0], (bytes, bytearray))
    assert captured[-1][1] == "board_draft.docx" and captured[-1][2] == "export", captured[-1][1:]
    expected = parse_docx(export.draft_to_docx(ss(at, "draft"), ss(at, "portfolio")))
    assert parse_docx(bytes(captured[-1][0])) == expected
    assert any(k == "List Bullet" for k, _ in expected), "the open questions are in the exported file"


def test_22_untrusted_text_whole_tree(cfg, port):
    ps = with_s_item(port)
    at = make_app("pages/2_Review_queue.py", cfg, portfolio=ps)
    at.run()
    no_exception(at)
    assert_s_only_in_text(at)                                       # (i) the queue page

    raw = json.dumps({"sections": [{"company": S, "paragraphs": [
        "Ridgeline's annual growth was {ridgeline.12m.growth_12m}."]}]})
    at = draft_page(cfg, FakeClient(raw), ps)
    generate(at)
    draft = ss(at, "draft")
    assert draft.status == "ok" and draft.sections[0].company == S
    assert_s_only_in_text(at)                                       # (ii) the draft page after Generate
    assert [s.value for s in at.subheader][0] == "Other"
    assert S in text_values(at), "the raw section.company follows the 'Other' heading as plain text"
    assert any("evil.example" in q.text for q in draft.questions)


# =========================================================================== 23-25 safety
def test_23_no_network_no_real_client(cfg, port, monkeypatch):
    with pytest.raises(RuntimeError, match="network access is blocked"):
        socket.socket(socket.AF_INET, socket.SOCK_STREAM).connect(("api.anthropic.com", 443))
    with pytest.raises(RuntimeError, match="network access is blocked"):
        socket.create_connection(("api.anthropic.com", 443))
    assert len(ATTEMPTS) == 2
    ATTEMPTS.clear()                                                # these two were deliberate

    sys.modules.pop("pipeline.llm", None)
    llm = importlib.import_module("pipeline.llm")                   # importing builds nothing
    assert hasattr(llm, "AnthropicClient") and ATTEMPTS == []
    with pytest.raises(RuntimeError, match="network access is blocked"):
        llm.AnthropicClient()                                       # proves the real client is blocked here
    assert len(ATTEMPTS) == 1
    ATTEMPTS.clear()
    demo = (ROOT / "scripts" / "demo_commentary.py").read_text()
    assert "pipeline.llm" in demo and "class AnthropicClient" not in demo, "the demo imports the client from pipeline.llm"

    built = []

    def lazy_client():
        built.append(1)
        return FakeClient(F1)

    monkeypatch.setattr(llm, "AnthropicClient", lazy_client)
    for page in PAGES:                                              # no click: no client is built
        at = make_app(page, cfg, portfolio=port)
        at.run()
        no_exception(at)
    assert built == [], "the real client must not be built at import or on a plain page run"
    at = make_app("pages/3_Board_draft.py", cfg, portfolio=port)    # no session client: built in the handler only
    at.run()
    generate(at)
    assert built == [1] and ss(at, "draft").status == "ok"
    assert ATTEMPTS == []


def _scan(paths, patterns):
    hits = []
    for f in paths:
        text = f.read_text()
        for pat in patterns:
            m = re.search(pat, text)
            if m:
                hits.append(f"{f.relative_to(ROOT)}: /{pat}/ at {text[max(0, m.start() - 20):m.end() + 20]!r}")
    return hits


def test_24_static_scan():
    cache_like = [r"st\." + "cache", "cache" + "_data", "cache" + "_resource", "lru" + "_cache",
                  r"functools\." + "cache", r"import\s+" + "cache", "experimental" + "_memo",
                  "experimental" + "_singleton", "unsafe_allow_html" + r"\s*=\s*" + "True", r"st\." + r"html\s*\("]
    scorer = ["answer" + "_key", "ground" + "_truth", r"(?<![\w.])" + "ev" + "al/",
              r"\bfrom\s+" + "ev" + r"al\b", r"\bimport\s+" + "ev" + r"al\b"]
    required = [ROOT / "app.py"] + [ROOT / "pages" / n for n in
                                    ("1_Upload.py", "2_Review_queue.py", "3_Board_draft.py")] \
        + [ROOT / "dashboard" / f"{n}.py" for n in ("views", "uploads", "export", "approvals")] \
        + [ROOT / "pipeline" / "llm.py"]
    for f in required:
        assert f.exists() and f.read_text().strip(), f"{f.relative_to(ROOT)} must exist and not be empty"
    scanned = sorted(set(required) | set((ROOT / "pages").glob("*.py")) | set((ROOT / "dashboard").glob("*.py")))
    assert not _scan(scanned, cache_like + scorer)
    reduced = [ROOT / "pipeline" / "review.py", ROOT / "scripts" / "demo_commentary.py"]
    assert all(f.exists() for f in reduced)
    assert not _scan(reduced, scorer)


def test_25_tracked_and_default_state_untouched(real_files):
    assert not _restore(real_files), "pipeline/aliases.yaml or data/raw/ changed during this module"
    assert outputs_snapshot() == real_files["outputs"], "ROOT/outputs/ changed during this module"
