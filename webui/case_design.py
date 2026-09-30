"""Shared presentation for the documentary workspace; no source authorization."""

from __future__ import annotations

from html import escape
from pathlib import Path

import streamlit as st


def inject_case_styles():
    stylesheet = Path(__file__).with_name("case_workspace.css")
    st.markdown(
        f"<style>{stylesheet.read_text(encoding='utf-8')}</style>",
        unsafe_allow_html=True,
    )


def render_section_header(title, description="", eyebrow=""):
    parts = ['<header class="cw-section">']
    if eyebrow:
        parts.append(f'<p class="cw-eyebrow">{escape(str(eyebrow))}</p>')
    parts.append(f"<h2>{escape(str(title))}</h2>")
    if description:
        parts.append(f'<p class="cw-description">{escape(str(description))}</p>')
    parts.append("</header>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def render_steps(labels, active_index):
    labels = list(labels)
    if not labels:
        return
    active = max(0, min(int(active_index), len(labels) - 1))
    steps = []
    for index, label in enumerate(labels):
        state = (
            "current" if index == active else "done" if index < active else "upcoming"
        )
        current = ' aria-current="step"' if index == active else ""
        steps.append(
            f'<li class="cw-step cw-step--{state}"{current}>'
            f'<span class="cw-step-number">{index + 1}</span>'
            f"<span>{escape(str(label))}</span></li>"
        )
    st.markdown(
        '<ol class="cw-steps">' + "".join(steps) + "</ol>", unsafe_allow_html=True
    )


def render_empty_state(title, description):
    st.markdown(
        f'<section class="cw-empty"><h3>{escape(str(title))}</h3>'
        f"<p>{escape(str(description))}</p></section>",
        unsafe_allow_html=True,
    )


def friendly_status(value):
    return {
        "outline_ready": "Outline ready",
        "draft_ready": "Draft ready",
        "review_ready": "Ready for editorial review",
        "approved": "Approved",
        "needs_revision": "Needs revision",
        "changes_requested": "Revision requested",
        "ready": "Cues ready",
        "needs_assets": "Missing sound effects",
        "stale": "Inputs changed",
        "complete": "Complete",
        "completed": "Complete",
        "queued": "Waiting to start",
        "running": "In progress",
        "retry": "Retrying",
        "failed": "Needs attention",
    }.get(value, str(value or "").replace("_", " ").capitalize())


def request_view(case_id, view):
    st.session_state["case_workspace_pending_case"] = case_id
    st.session_state["case_workspace_pending_view"] = view
    st.rerun()


def render_application_navigation(tr):
    """Expose case work independently of the chosen video material provider."""
    pending = st.session_state.pop("application_pending_workspace", None)
    if pending in {"video", "documentary"}:
        st.session_state["application_workspace"] = pending
    st.session_state.setdefault(
        "application_workspace",
        "documentary" if st.query_params.get("workspace") == "documentary" else "video",
    )
    labels = {"video": tr("Create a video"), "documentary": tr("Documentary workspace")}
    selected = st.radio(
        tr("Workspace"),
        ("video", "documentary"),
        format_func=labels.get,
        horizontal=True,
        label_visibility="collapsed",
        key="application_workspace",
    )
    previous = st.session_state.get("application_active_workspace", "video")
    draft_keys = ("video_subject", "video_script", "video_terms")
    if previous == "video" and selected == "documentary":
        st.session_state["application_video_draft"] = {
            key: st.session_state[key] for key in draft_keys if key in st.session_state
        }
    elif previous == "documentary" and selected == "video":
        for key, value in st.session_state.get("application_video_draft", {}).items():
            st.session_state[key] = value
    st.session_state["application_active_workspace"] = selected
    return selected
