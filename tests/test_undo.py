"""Undo of review decisions: an appended ``undo`` line reverses the latest decision in force.

Uses tmp state dirs only.
"""
import json
from pathlib import Path

import pytest

from dashboard import views
from pipeline import review

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
VERITY = "Verity Legal Tech"


@pytest.fixture
def cfg(tmp_path):
    state = tmp_path / "state"
    return review.Config(data_dir=RAW_DIR, state_dir=state, aliases_path=state / "aliases.yaml",
                         seed_aliases_path=ROOT / "pipeline" / "aliases.yaml")


def lines(cfg):
    f = cfg.state_dir / "decisions.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines() if x.strip()] if f.exists() else []


def ids(p):
    return sorted(review.item_id(i) for i in p.review_queue)


def find(p, error_type, company=None, suggestion=None):
    return next(i for i in p.review_queue if i.error_type == error_type
                and (company is None or i.company == company)
                and (suggestion is None or i.suggestion == suggestion))


def test_undo_reject_clears_mark(cfg):
    p0 = review.load(cfg)
    iid = review.item_id(p0.review_queue[0])
    p1 = review.resolve(p0, iid, "reject", cfg)
    assert iid in views.marked_as_question(p1)
    p2 = review.undo(p1, iid, cfg)
    assert iid not in views.marked_as_question(p2)
    assert ids(p2) == ids(p0) and views.fingerprint(p2) == views.fingerprint(p0)
    assert [d["action"] for d in lines(cfg)] == ["reject", "undo"]
    assert lines(cfg)[1]["undoes_ts"] == lines(cfg)[0]["ts"]
    assert views.decision_rows(p2) == []


def test_undo_accept_reopens_value(cfg):
    p0 = review.load(cfg)
    item = find(p0, "impossible_value")
    iid = review.item_id(item)
    p1 = review.resolve(p0, iid, "confirm", cfg)
    assert iid not in ids(p1)
    p2 = review.undo(p1, iid, cfg)
    assert iid in ids(p2)
    assert ids(p2) == ids(p0) and views.fingerprint(p2) == views.fingerprint(p0)


def test_undo_confirm_alias_unlearns(cfg):
    p0 = review.load(cfg)
    seed = cfg.aliases_path.read_bytes()
    item = find(p0, "unknown_label", VERITY, suggestion="new_arr")
    iid = review.item_id(item)
    p1 = review.resolve(p0, iid, "confirm", cfg)
    assert cfg.aliases_path.read_bytes() != seed and iid not in ids(p1)

    p2 = review.undo(p1, iid, cfg)
    assert cfg.aliases_path.read_bytes() == seed
    assert ids(p2) == ids(p0) and views.fingerprint(p2) == views.fingerprint(p0)
    reopened = next(i for i in p2.review_queue if review.item_id(i) == iid)
    assert reopened.suggestion == "new_arr" and review.raw_label(p2, reopened)
    row = next(r for r in views.queue_rows(p2) if r["item_id"] == iid)
    assert f"confirm:{iid}" in dict(row["actions"]), "Confirm alias is offered again"

    # a reload replays the log the same way, and the label can be learned again
    p3 = review.load(cfg)
    assert ids(p3) == ids(p2) and views.fingerprint(p3) == views.fingerprint(p2)
    assert cfg.aliases_path.read_bytes() == seed
    p4 = review.resolve(p3, iid, "confirm", cfg)
    assert iid not in ids(p4)


def test_undo_close_survives_reload(cfg):
    p0 = review.load(cfg)
    iid = review.item_id(find(p0, "duplicate_submission"))
    p1 = review.resolve(p0, iid, "confirm", cfg)
    review.undo(p1, iid, cfg)
    p2 = review.load(cfg)
    assert iid in ids(p2) and views.decision_rows(p2) == []


def test_undo_without_decision_raises_and_writes_nothing(cfg):
    p0 = review.load(cfg)
    before = sorted(f.relative_to(cfg.state_dir) for f in cfg.state_dir.rglob("*"))
    with pytest.raises(KeyError):
        review.undo(p0, review.item_id(p0.review_queue[0]), cfg)
    assert sorted(f.relative_to(cfg.state_dir) for f in cfg.state_dir.rglob("*")) == before


def test_page_undo_button(cfg):
    from streamlit.testing.v1 import AppTest
    p0 = review.load(cfg)
    iid = review.item_id(find(p0, "duplicate_submission"))
    at = AppTest.from_file(str(ROOT / "pages" / "2_Review_queue.py"), default_timeout=60)
    at.session_state["cfg"] = cfg
    at.session_state["portfolio"] = p0
    at.run()
    at.button(key=f"confirm:{iid}").click().run()
    assert not at.exception
    assert not any(b.key == f"confirm:{iid}" for b in at.button)
    assert any(t.value.startswith("Resolved") for t in at.toast)

    at.button(key=f"undo:{iid}").click().run()
    assert not at.exception
    assert any(b.key == f"confirm:{iid}" for b in at.button), "the item is back in the queue"
    assert not any(b.key == f"undo:{iid}" for b in at.button)
    assert any(t.value.startswith("Decision undone") for t in at.toast)
