"""Session helpers. They take the session-state mapping as an argument, so no Streamlit import is needed."""
from __future__ import annotations

from pipeline import review


def get_cfg(state) -> review.Config:
    cfg = state.get("cfg")
    return cfg if cfg is not None else review.Config.default()


def ensure_portfolio(state):
    """The session portfolio; on a fresh session, reload saved uploads (replaying decisions) if any exist."""
    p = state.get("portfolio")
    if p is not None:
        return p
    cfg = get_cfg(state)
    uploads = cfg.state_dir / "uploads"
    paths = sorted(f for f in uploads.glob("*") if f.is_file()) if uploads.exists() else []
    if not paths:
        return None
    p = review.load(cfg, paths)
    state["portfolio"] = p
    return p
