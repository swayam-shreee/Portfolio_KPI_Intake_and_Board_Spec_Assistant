"""Demo UI: the archive-and-start-over reset, and the sidebar order. Uses tmp state dirs only."""
from pathlib import Path

import pytest

from dashboard import reset, uploads
from pipeline import review

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw"
RAW = sorted(RAW_DIR.glob("*.xlsx")) + sorted(RAW_DIR.glob("*.csv"))


@pytest.fixture
def cfg(tmp_path):
    state = tmp_path / "state"
    return review.Config(data_dir=RAW_DIR, state_dir=state, aliases_path=state / "aliases.yaml",
                         seed_aliases_path=ROOT / "pipeline" / "aliases.yaml")


def save_raw(cfg):
    assert uploads.save_uploads(cfg, [(p.name, p.read_bytes()) for p in RAW]).saved


def test_archive_state_moves_everything(cfg):
    save_raw(cfg)
    p = review.load(cfg, sorted((cfg.state_dir / "uploads").iterdir()))
    review.resolve(p, review.item_id(p.review_queue[0]), "reject", cfg)
    before = sorted(f.relative_to(cfg.state_dir) for f in cfg.state_dir.rglob("*") if f.is_file())
    assert reset.has_saved_state(cfg)

    dest = reset.archive_state(cfg)

    assert dest is not None and dest.parent == cfg.state_dir.parent / "archive"
    assert list(cfg.state_dir.iterdir()) == []
    assert sorted(f.relative_to(dest) for f in dest.rglob("*") if f.is_file()) == before
    assert not reset.has_saved_state(cfg)
    # a fresh load after the reset starts clean and re-seeds the learned aliases
    fresh = review.load(cfg)
    assert fresh.decisions == [] and cfg.aliases_path.exists()


def test_archive_state_noop_when_empty(cfg):
    assert reset.archive_state(cfg) is None                    # missing dir
    cfg.state_dir.mkdir(parents=True)
    assert reset.archive_state(cfg) is None                    # empty dir
    assert not (cfg.state_dir.parent / "archive").exists()


def test_archive_state_never_overwrites(cfg):
    first, second = [], []
    for out in (first, second):
        cfg.state_dir.mkdir(parents=True, exist_ok=True)
        (cfg.state_dir / "decisions.jsonl").write_text("{}\n")
        out.append(reset.archive_state(cfg))
    assert first[0] != second[0] and first[0].exists() and second[0].exists()


def make_app(page, cfg, portfolio=None):
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_file(str(ROOT / page), default_timeout=60)
    at.session_state["cfg"] = cfg
    if portfolio is not None:
        at.session_state["portfolio"] = portfolio
    return at


def test_start_over_empties_the_app(cfg):
    save_raw(cfg)
    at = make_app("pages/1_Upload.py", cfg)
    at.session_state["draft"] = object()
    at.run()
    assert not at.exception
    assert [b for b in at.button if b.key == "start_over"], "Start over shows while uploads are saved"
    next(b for b in at.button if b.key == "start_over").click()
    at.run()
    assert not at.exception
    for key in ("portfolio", "draft", "upload_result"):
        assert key not in at.session_state
    up = cfg.state_dir / "uploads"
    assert not up.exists() or not list(up.iterdir())
    assert not [b for b in at.button if b.key == "start_over"], "nothing left to start over from"
    assert any(b.key == "load_sample" for b in at.button)


def test_welcome_without_state_has_no_start_over(cfg):
    at = make_app("pages/1_Upload.py", cfg)
    at.run()
    assert not at.exception
    assert not [b for b in at.button if b.key == "start_over"]


def test_sidebar_order(cfg):
    at = make_app("app.py", cfg, portfolio=review.load(cfg))
    at.run()
    assert not at.exception
    links = [n.proto.label for n in at.sidebar if getattr(n, "type", None) == "page_link"]
    assert links == ["Upload", "Portfolio", "Review queue", "Board draft"]
