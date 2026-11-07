import pandas as pd
import streamlit as st

from dashboard import approvals, export, session, views
from pipeline import commentary, llm
from ui import nav

SRC_COLS = ["metric_id", "display", "source_file", "source_cell"]

st.set_page_config(page_title="Board draft", page_icon=":material/article:", layout="wide")
nav.sidebar()
st.title("Board draft")
state = st.session_state
cfg = session.get_cfg(state)
p = session.ensure_portfolio(state)


def generate(p):
    client = state.get("claude_client")
    if client is None:
        client = llm.AnthropicClient()
    draft = commentary.draft_commentary(p.metrics, p.review_queue, client,
                                        cfg.state_dir / "commentary_log.jsonl")
    state["draft"] = draft
    state["draft_model"] = client.model
    state["draft_fingerprint"] = views.fingerprint(p)
    state["approved"] = False


def run_generate(p):
    try:
        generate(p)
    except Exception as e:
        st.error("Could not generate a draft.")
        st.text(f"{type(e).__name__}: {e}")
    else:
        st.rerun()


if p is None:
    st.info("No data loaded yet. Open the Upload page first.")
    nav.link("pages/1_Upload.py", "Go to Upload", ":material/upload_file:")
else:
    st.caption("Claude writes the prose; every figure is filled in by code from the checked metrics "
               "and links back to its source cell. Open review items appear as questions.")
    draft = state.get("draft")
    if draft is None:
        if st.button("Generate draft", key="generate", type="primary", icon=":material/edit_note:"):
            run_generate(p)
    else:
        stale = state.get("draft_fingerprint") != views.fingerprint(p)
        approved = bool(state.get("approved"))
        if st.button("Regenerate", key="regenerate", icon=":material/refresh:"):
            run_generate(p)
        st.text(f"Draft status: {draft.status} · attempts {draft.attempts}")
        if stale:
            st.warning("Out of date: the data or the open review items changed after this draft. "
                       "Regenerate it before approving or exporting.")
        if draft.status == "rejected":
            st.error("Draft rejected")
            for reason in draft.reject_reasons:
                st.text(reason)
        else:
            for section in draft.sections:
                with st.container(border=True):
                    name = views.section_name(section.company, p)
                    st.subheader(name or "Other")
                    if name is None:
                        st.text(section.company)
                    for para in section.paragraphs:
                        st.text(para.rendered)
                        st.dataframe(pd.DataFrame(views.sources_rows(para), columns=SRC_COLS),
                                     hide_index=True)
        with st.container(border=True):
            st.subheader("Open questions")
            for q in draft.questions:
                st.text(q.text)

        st.space("small")
        with st.container(horizontal=True):
            if st.button("Approve", key="approve", type="primary", icon=":material/check:",
                         disabled=draft.status != "ok" or stale or approved):
                approvals.record(cfg, "approved", draft, p, state.get("draft_model"),
                                 state.get("draft_fingerprint"))
                state["approved"] = True
                st.rerun()
            if st.button("Reject draft", key="reject_draft", icon=":material/close:"):
                approvals.record(cfg, "rejected", draft, p, state.get("draft_model"),
                                 state.get("draft_fingerprint"))
                state["draft"] = None
                state["approved"] = False
                st.rerun()
            st.download_button("Export to Word", data=export.draft_to_docx(draft, p),
                               file_name="board_draft.docx", key="export", icon=":material/download:",
                               disabled=not approved or stale)
