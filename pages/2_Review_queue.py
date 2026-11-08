import pandas as pd
import streamlit as st

from dashboard import session, views
from pipeline import label_match, llm, review
from ui import nav

st.set_page_config(page_title="Review queue", page_icon=":material/fact_check:", layout="wide")
nav.sidebar()
st.title("Review queue")
cfg = session.get_cfg(st.session_state)
p = session.ensure_portfolio(st.session_state)

_NOTICES = {
    "reject": ("Marked as a question. It will appear under Open questions in the board draft.",
               ":material/help:"),
    "confirm": ("Resolved. Portfolio and board draft updated.", ":material/check_circle:"),
    "undo": ("Decision undone. The item is back in the queue.", ":material/undo:"),
    "correct": ("Corrected. Portfolio and board draft updated.", ":material/check_circle:"),
}
notice = st.session_state.pop("review_notice", None)
if notice in _NOTICES:
    msg, icon = _NOTICES[notice]
    st.toast(msg, icon=icon)

if p is None:
    st.info("No data loaded yet. Open the Upload page first.")
    nav.link("pages/1_Upload.py", "Go to Upload", ":material/upload_file:")
else:
    st.caption("Values that failed a check wait here. Confirm or reject each one, "
               "and the Portfolio and board draft update right away.")
    if label_match.eligible(p) and st.button("Ask Claude to match labels", key="suggest_labels",
                                             icon=":material/auto_awesome:"):
        try:
            client = st.session_state.get("label_client")
            if client is None:
                client = llm.AnthropicClient()
            new = label_match.suggest_labels(p, cfg, client)
        except Exception:
            st.error("Claude could not suggest labels.")
        else:
            st.session_state["portfolio"] = new
            st.rerun()
    rows = views.queue_rows(p)
    if not rows:
        st.text("No open review items.")
    else:
        st.space("small")
        with st.container(border=True):
            st.subheader("Open items")
            shown = ["status", "item_id", "company", "month", "metric", "error_type", "source_file",
                     "source_cell", "reason", "raw_label", "suggestion", "suggested_by", "confidence"]
            st.dataframe(pd.DataFrame([{k: r[k] for k in shown} for r in rows], columns=shown),
                         hide_index=True)
        st.space("small")
        st.subheader("Decide")
        for row in rows:
            with st.container(border=True):
                st.text(f"{row['company']} | {row['error_type']} | {row['month']} | {row['metric']} | "
                        f"{row['source_file']} {row['source_cell']}")
                marked = row["status"] == "Marked as question"
                if marked:
                    st.text("Marked as question · shown in the board draft's Open questions")
                with st.container(horizontal=True):
                    if row.get("correction"):
                        ckey, clabel = row["correction"]
                        if st.button(clabel, key=ckey, type="primary", icon=":material/build:"):
                            try:
                                new = review.correct(p, row["item_id"], cfg)
                            except (ValueError, KeyError):
                                st.error("This item can't be corrected here.")
                            else:
                                st.session_state["portfolio"] = new
                                st.session_state["review_notice"] = "correct"
                                st.rerun()
                    for key, label in row["actions"]:
                        primary = key.startswith("confirm:")
                        if primary:
                            extra = {}
                        else:
                            extra = {"icon": ":material/check:" if marked else ":material/help:",
                                     "disabled": marked}
                        if st.button(label, key=key, type="primary" if primary else "secondary", **extra):
                            action = "confirm" if primary else "reject"
                            try:
                                new = review.resolve(p, row["item_id"], action, cfg)
                            except (ValueError, KeyError):
                                st.error("This item can't be resolved here.")
                            else:
                                st.session_state["portfolio"] = new
                                st.session_state["review_notice"] = action
                                st.rerun()
    done = views.decision_rows(p)
    if done:
        st.space("small")
        st.subheader("Your decisions")
        st.caption("Clicked the wrong button? Undo puts the item back in the queue.")
        for row in done:
            with st.container(border=True, horizontal=True, vertical_alignment="center"):
                st.text(f"{row['what']} | {row['company']} | {row['error_type']} | {row['month']} | "
                        f"{row['metric']} | {row['source_file']} {row['source_cell']}")
                if st.button("Undo", key=f"undo:{row['item_id']}", icon=":material/undo:"):
                    try:
                        new = review.undo(p, row["item_id"], cfg)
                    except (ValueError, KeyError):
                        st.error("This decision can't be undone here.")
                    else:
                        st.session_state["portfolio"] = new
                        st.session_state["review_notice"] = "undo"
                        st.rerun()
