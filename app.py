import streamlit as st

from dashboard import session, views

st.set_page_config(page_title="Portfolio KPI assistant", layout="wide")
st.title("Portfolio overview")

p = session.ensure_portfolio(st.session_state)
if p is None:
    st.info("No data loaded yet. Open the Upload page to add KPI files or load the sample data.")
else:
    st.text(f"Period: {p.months[0]} to {p.months[-1]}")
    st.dataframe(views.overview(p))
    st.dataframe(views.metric_detail(p))
