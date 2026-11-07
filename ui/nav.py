"""Sidebar navigation in demo order: Upload, Portfolio, Review queue, Board draft.

Streamlit's own page menu is turned off in .streamlit/config.toml, because it always lists the
main script (app.py) first and names it after the file. The page files keep their names, as the
dashboard checker runs them by path.
"""
from __future__ import annotations

import streamlit as st
from streamlit.errors import StreamlitPageNotFoundError

PAGES = [
    ("pages/1_Upload.py", "Upload", ":material/upload_file:"),
    ("app.py", "Portfolio", ":material/space_dashboard:"),
    ("pages/2_Review_queue.py", "Review queue", ":material/fact_check:"),
    ("pages/3_Board_draft.py", "Board draft", ":material/article:"),
]


def link(page: str, label: str, icon: str | None = None) -> None:
    """st.page_link that skips a page Streamlit hasn't registered.

    In the real app every page resolves. AppTest runs a single page file as the main script,
    where the other pages aren't registered.
    """
    try:
        st.page_link(page, label=label, icon=icon)
    except StreamlitPageNotFoundError:
        pass


def sidebar() -> None:
    with st.sidebar:
        st.markdown("**Portfolio KPI assistant**")
        for page, label, icon in PAGES:
            link(page, label, icon)
