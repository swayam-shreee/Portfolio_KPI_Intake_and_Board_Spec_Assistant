import streamlit as st

from dashboard import session, views
from ui import nav

st.set_page_config(page_title="Portfolio", page_icon=":material/space_dashboard:", layout="wide")
nav.sidebar()
st.title("Portfolio")

p = session.ensure_portfolio(st.session_state)
if p is None:
    st.info("No data loaded yet. Open the Upload page to add KPI files or load the sample data.")
    nav.link("pages/1_Upload.py", "Go to Upload", ":material/upload_file:")
else:
    st.caption("Each company's latest twelve months, computed only from values that passed every check.")
    st.text(f"Period: {p.months[0]} to {p.months[-1]}")
    st.space("small")
    with st.container(border=True):
        st.subheader("Companies")
        st.caption("A company reads 'Needs review' while any of its values waits in the review queue.")
        st.dataframe(views.overview(p), hide_index=True)
    st.space("small")
    with st.container(border=True):
        st.subheader("Metric detail")
        st.caption("Every metric for every company, with its source file and cell. "
                   "Values blocked by an open review item read 'under review'.")
        st.dataframe(views.metric_detail(p), hide_index=True)
