import streamlit as st

from dashboard import session, uploads
from pipeline import review

st.title("Upload KPI files")
cfg = session.get_cfg(st.session_state)

files = st.file_uploader("Upload KPI files", type=["xlsx", "csv"], accept_multiple_files=True, key="uploads")
if files:
    result = uploads.save_uploads(cfg, [(f.name, f.getvalue()) for f in files])
    st.session_state["upload_result"] = result
    if result.saved:
        paths = sorted(f for f in (cfg.state_dir / "uploads").glob("*") if f.is_file())
        st.session_state["portfolio"] = review.load(cfg, paths)

if st.button("Load sample data", key="load_sample"):
    st.session_state["portfolio"] = review.load(cfg)

result = st.session_state.get("upload_result")
if result is not None:
    st.subheader("Upload result")
    for label, names in (("Saved", result.saved), ("Skipped", result.skipped)):
        for name in names:
            st.text(f"{label}: {name}")
    for name, reason in result.refused:
        st.text(f"Refused: {name} ({reason})")
if st.session_state.get("portfolio") is not None:
    st.text("A portfolio is loaded. Open the overview, review queue or board draft pages.")
