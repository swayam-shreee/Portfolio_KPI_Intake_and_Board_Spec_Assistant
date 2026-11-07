import streamlit as st

from dashboard import reset, session, uploads
from pipeline import review
from ui import nav

SESSION_KEYS = ("portfolio", "upload_result", "draft", "draft_model", "draft_fingerprint", "approved",
                "uploads")

st.set_page_config(page_title="Upload", page_icon=":material/upload_file:", layout="wide")
nav.sidebar()
cfg = session.get_cfg(st.session_state)


def start_over():
    """Archive the saved state and clear the session, so the app is empty again."""
    reset.archive_state(cfg)
    for key in SESSION_KEYS:
        st.session_state.pop(key, None)


_, mid, _ = st.columns([1, 3, 1])
with mid:
    st.title("Bring in this month's KPI packs")
    st.markdown("Drop the spreadsheets each portfolio company sent. Every label is mapped to a "
                "standard metric, every value is checked, and the board draft cites the file and "
                "cell behind each number.")

    with st.container(border=True):
        files = st.file_uploader("Upload KPI files", type=["xlsx", "csv"], accept_multiple_files=True,
                                 key="uploads", label_visibility="collapsed")
        st.caption("Excel or CSV, one or more files per company. A file you've already uploaded is skipped.")
    if files:
        result = uploads.save_uploads(cfg, [(f.name, f.getvalue()) for f in files])
        st.session_state["upload_result"] = result
        if result.saved:
            paths = sorted(f for f in (cfg.state_dir / "uploads").glob("*") if f.is_file())
            st.session_state["portfolio"] = review.load(cfg, paths)

    if st.button("Use the sample portfolio instead", key="load_sample", type="tertiary",
                 icon=":material/dataset:"):
        st.session_state["portfolio"] = review.load(cfg)

    result = st.session_state.get("upload_result")
    loaded = st.session_state.get("portfolio") is not None
    if result is not None:
        with st.container(border=True):
            st.markdown("**Upload result**")
            for name in result.saved:
                st.text(f"Saved: {name}")
            for name in result.skipped:
                st.text(f"Skipped: {name} ({uploads.SKIPPED_TEXT})")
            for name, reason in result.refused:
                st.text(f"Refused: {name} ({uploads.REFUSED_TEXT.get(reason, reason)})")
            if result.skipped and not result.saved and not result.refused:
                st.text("No new files; the saved portfolio is loaded.")

    if loaded or reset.has_saved_state(cfg):
        if loaded:
            st.text("A portfolio is loaded. Open the Portfolio, Review queue or Board draft pages.")
        with st.container(horizontal=True, vertical_alignment="center", gap="medium"):
            nav.link("app.py", "Open Portfolio", ":material/space_dashboard:")
            st.button("Start over", key="start_over", on_click=start_over, icon=":material/restart_alt:",
                      type="tertiary",
                      help="Moves the saved uploads and review decisions to outputs/archive/ and empties the app.")
    else:
        st.space("medium")
        steps = st.columns(3, gap="large")
        with steps[0]:
            st.markdown("**1. Upload**")
            st.caption("Each file is read, its labels are matched to standard metrics, and every value is checked.")
        with steps[1]:
            st.markdown("**2. Review**")
            st.caption("Anything that fails a check waits for you in the review queue. Nothing is guessed.")
        with steps[2]:
            st.markdown("**3. Draft**")
            st.caption("Claude writes the board commentary. Every figure links back to its source cell.")
