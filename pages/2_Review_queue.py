import pandas as pd
import streamlit as st

from dashboard import session, views
from pipeline import review

st.title("Review queue")
cfg = session.get_cfg(st.session_state)
p = session.ensure_portfolio(st.session_state)

if p is None:
    st.info("No data loaded yet. Open the Upload page first.")
else:
    rows = views.queue_rows(p)
    if not rows:
        st.text("No open review items.")
    else:
        shown = ["item_id", "company", "month", "metric", "error_type", "source_file", "source_cell",
                 "reason", "raw_label", "suggestion"]
        st.dataframe(pd.DataFrame([{k: r[k] for k in shown} for r in rows], columns=shown))
        for row in rows:
            st.text(f"{row['company']} | {row['error_type']} | {row['month']} | {row['metric']} | "
                    f"{row['source_file']} {row['source_cell']}")
            for key, label in row["actions"]:
                if st.button(label, key=key):
                    action = "confirm" if key.startswith("confirm:") else "reject"
                    try:
                        new = review.resolve(p, row["item_id"], action, cfg)
                    except (ValueError, KeyError):
                        st.error("This item can't be resolved here.")
                    else:
                        st.session_state["portfolio"] = new
                        st.rerun()
