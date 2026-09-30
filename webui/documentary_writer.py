"""Evidence-based documentary writing controls for one case workspace."""

from __future__ import annotations

from copy import deepcopy
import re

import streamlit as st

from app.models.documentary import (
    DOCUMENTARY_DEFAULT_MINUTES,
    DOCUMENTARY_MAX_MINUTES,
    DOCUMENTARY_MIN_MINUTES,
    DOCUMENTARY_PLANNING_WORDS_PER_MINUTE,
    normalize_documentary_minutes,
)
from webui.case_design import (
    friendly_status,
    render_empty_state,
    render_section_header,
    render_steps,
    request_view,
)


def get_writer(workspace):
    from app.services.targeted_search.documentary import DocumentaryWriter

    return DocumentaryWriter(workspace)


def _key(case_id, name):
    return f"case_{case_id}_documentary_{name}"


def _document_key(case_id, document, name):
    return _key(case_id, f"{document['id']}_{document['revision']}_{name}")


def _citation_label(citation):
    from webui.case_workspace import locator_label

    return " · ".join(
        str(value)
        for value in (
            citation.get("filename") or citation.get("asset_id"),
            locator_label(citation.get("locator")),
        )
        if value
    )


def _render_citation(citation, tr):
    st.caption(_citation_label(citation))
    if citation.get("quote"):
        st.text(citation["quote"])
    elif citation.get("text"):
        st.text(citation["text"])
    else:
        st.caption(tr("Documentary source has no indexed text"))


def _supported(document):
    factual = document.get("factual_review") or {}
    passages = factual.get("passages") or []
    return bool(passages) and all(
        passage.get("status") == "supported" for passage in passages
    )


def _narration_stats(document):
    from app.services.targeted_search.documentary import clean_narration_text

    spoken = "\n".join(
        clean_narration_text(passage.get("text", ""))
        for chapter in (document.get("draft") or {}).get("chapters", [])
        for scene in chapter.get("scenes", [])
        for passage in scene.get("passages", [])
    )
    words = len(re.findall(r"\b\w+(?:['’\-]\w+)*\b", spoken))
    language = str((document.get("options") or {}).get("language") or "").strip()
    english = bool(
        re.fullmatch(r"english|en(?:[-_][a-z]{2,8})*", language, flags=re.IGNORECASE)
    )
    return words, words / DOCUMENTARY_PLANNING_WORDS_PER_MINUTE if english else None


def _render_narration_stats(document, tr):
    words, minutes = _narration_stats(document)
    count, estimate = st.columns(2)
    count.metric(tr("Documentary spoken word count"), f"{words:,}")
    estimate.metric(
        tr("Documentary estimated narration minutes"),
        f"{minutes:.1f}" if minutes is not None else tr("Documentary estimate unavailable"),
    )
    band = {
        "min_minutes": DOCUMENTARY_MIN_MINUTES,
        "max_minutes": DOCUMENTARY_MAX_MINUTES,
        "words_per_minute": DOCUMENTARY_PLANNING_WORDS_PER_MINUTE,
    }
    st.caption(
        tr(
            "Documentary narration estimate help"
            if minutes is not None
            else "Documentary narration estimate unavailable help"
        ).format(**band)
    )
    if minutes is not None and not (
        DOCUMENTARY_MIN_MINUTES <= minutes <= DOCUMENTARY_MAX_MINUTES
    ):
        st.warning(
            tr("Documentary narration estimate outside band").format(
                minutes=minutes, **band
            )
        )


def _workflow_index(document):
    if not document.get("draft"):
        return 1
    if document.get("human_review") and not document["human_review"].get("approved"):
        return 1
    if not document.get("factual_review") or not _supported(document):
        return 2
    if not (document.get("human_review") or {}).get("approved"):
        return 3
    return 4


def _view_key(case_id, document):
    return _document_key(case_id, document, "view")


def _open_view(case_id, document, view):
    st.session_state[_view_key(case_id, document)] = view


def _open_evidence_review(case_id):
    request_view(case_id, "Timeline / Claims")


def _save_human_review(writer, case_id, document, approved, tr):
    try:
        reviewed = writer.review(
            case_id,
            document["id"],
            reviewed_by=st.session_state[
                _document_key(case_id, document, "reviewer")
            ].strip(),
            notes=st.session_state[_document_key(case_id, document, "review_notes")],
            approved=approved,
            expected_revision=document["revision"],
        )
        _open_view(
            case_id,
            reviewed,
            "Documentary export view" if approved else "Documentary edit view",
        )
        st.session_state[_key(case_id, "flash")] = (
            "success", tr("Documentary review saved")
        )
    except Exception as exc:
        st.session_state[_key(case_id, "flash")] = ("error", str(exc))


def _render_next_action(writer, workspace, case_id, document, tr):
    if not document.get("draft"):
        st.info(tr("Documentary next draft help"))
        if st.button(
            tr("Write cited documentary draft"),
            key=_document_key(case_id, document, "write_draft"),
            type="primary",
        ):
            try:
                _queue(writer, workspace, case_id, _stage_options(document, "draft"), tr)
            except Exception as exc:
                st.error(str(exc))
    elif not document.get("factual_review"):
        st.info(tr("Documentary next fact check help"))
        if st.button(
            tr("Run documentary factual review"),
            key=_document_key(case_id, document, "factual_review"),
            type="primary",
        ):
            try:
                _queue(
                    writer,
                    workspace,
                    case_id,
                    _stage_options(document, "factual_review"),
                    tr,
                )
            except Exception as exc:
                st.error(str(exc))
    elif (
        document.get("human_review") and not document["human_review"].get("approved")
    ) or not _supported(document):
        st.warning(
            tr("Documentary revision requested help")
            if document.get("human_review")
            and not document["human_review"].get("approved")
            else tr("Documentary fact check blockers")
        )
        st.button(
            tr("Open documentary revision"),
            key=_document_key(case_id, document, "open_revision"),
            type="primary",
            on_click=_open_view,
            args=(case_id, document, "Documentary edit view"),
        )
    elif not (document.get("human_review") or {}).get("approved"):
        st.info(tr("Documentary next approval help"))
        st.button(
            tr("Review documentary for approval"),
            key=_document_key(case_id, document, "open_review"),
            type="primary",
            on_click=_open_view,
            args=(case_id, document, "Documentary review view"),
        )
    else:
        st.success(tr("Documentary next export help"))
        st.button(
            tr("Open documentary exports"),
            key=_document_key(case_id, document, "open_exports"),
            type="primary",
            on_click=_open_view,
            args=(case_id, document, "Documentary export view"),
        )


def _queue(writer, workspace, case_id, options, tr):
    from app.services.targeted_search.worker import ensure_worker_running

    job = writer.enqueue(case_id, options)
    ensure_worker_running(root_dir=workspace.repo.root)
    st.session_state[_key(case_id, "last_job")] = job["id"]
    st.success(tr("Documentary generation queued"))


def _claim_eligibility_notes(claim):
    """Screen claim metadata; build_packet still verifies each retained source."""
    notes = []
    if not str(claim.get("reviewed_by", "")).strip():
        notes.append("Documentary claim reviewer required")
    if claim.get("assertion_class") not in {
        "allegation",
        "testimony",
        "police_report",
        "court_finding",
        "news_report",
        "recording_observation",
    }:
        notes.append("Documentary claim classification required")
    if not any(
        citation.get("relation") == "supports"
        for citation in claim.get("citations", [])
    ):
        notes.append("Documentary claim supporting evidence required")
    if claim.get("has_stale_citations"):
        notes.append("Documentary claim current citations required")
    return notes


def _render_new_document(writer, workspace, case, tr):
    case_id = case["id"]
    saved_options = st.session_state.get(_key(case_id, "saved_options"), {})
    reviewed = [
        row for row in workspace.list_claims(case_id) if row.get("status") == "reviewed"
    ]
    claims = {
        row["id"]: row
        for row in reviewed
        if not _claim_eligibility_notes(row)
    }
    ineligible = [row for row in reviewed if row["id"] not in claims]
    st.caption(tr("Documentary evidence provider help"))
    selected = st.multiselect(
        tr("Documentary reviewed claims"),
        list(claims),
        default=[
            value
            for value in saved_options.get("claim_ids", list(claims))
            if value in claims
        ],
        format_func=lambda value: claims[value]["text"],
        key=_key(case_id, "claim_ids"),
    )
    if not claims:
        st.info(tr("Documentary reviewed evidence required"))
    if ineligible:
        st.warning(tr("Documentary reviewed claims need evidence"))
        with st.expander(tr("Documentary claims to fix")):
            for row in ineligible:
                st.write(row["text"])
                for note in _claim_eligibility_notes(row):
                    st.caption(tr(note))
    if not claims or ineligible:
        st.button(
            tr("Open documentary evidence review"),
            key=_key(case_id, "open_evidence_review"),
            on_click=_open_evidence_review,
            args=(case_id,),
        )
    with st.expander(tr("Documentary evidence packet preview")):
        if st.button(
            tr("Preview documentary evidence"),
            disabled=not selected,
            key=_key(case_id, "preview_packet"),
        ):
            st.session_state[_key(case_id, "packet")] = {
                "claim_ids": list(selected),
            }
        preview = st.session_state.get(_key(case_id, "packet"), {})
        if preview.get("claim_ids") == selected:
            # Resolve again so a changed source or revoked permission cannot leave
            # a previously cached packet visible after the user's next action.
            try:
                packet = writer.build_packet(case_id, claim_ids=selected)
                for claim in packet.get("claims", []):
                    st.write(claim["text"])
                    st.caption(
                        f"{claim.get('assertion_class', '')} · {claim.get('reviewed_by', '')}"
                    )
                for citation in packet.get("citations", []):
                    _render_citation(citation, tr)
                if packet.get("gaps"):
                    st.write(tr("Documentary evidence gaps"))
                    st.write(packet["gaps"])
            except Exception as exc:
                st.session_state.pop(_key(case_id, "packet"), None)
                st.error(str(exc))
    with st.form(_key(case_id, "new_document")):
        title = st.text_input(
            tr("Documentary title"),
            value=saved_options.get("title", case["name"]),
            key=_key(case_id, "title"),
        )
        minutes_key = _key(case_id, "minutes")
        if minutes_key in st.session_state:
            previous_minutes = st.session_state[minutes_key]
            normalized_minutes = normalize_documentary_minutes(previous_minutes)
            if previous_minutes != normalized_minutes:
                st.session_state[minutes_key] = normalized_minutes
        minutes = st.number_input(
            tr("Documentary target minutes"),
            min_value=float(DOCUMENTARY_MIN_MINUTES),
            max_value=float(DOCUMENTARY_MAX_MINUTES),
            value=normalize_documentary_minutes(
                saved_options.get("target_minutes", DOCUMENTARY_DEFAULT_MINUTES)
            ),
            step=1.0,
            format="%g",
            key=minutes_key,
        )
        st.caption(tr("Documentary duration help"))
        with st.expander(tr("Documentary writing preferences")):
            language = st.text_input(
                tr("Documentary narration language"),
                value=saved_options.get("language", "English"),
                key=_key(case_id, "language"),
            )
            instructions = st.text_area(
                tr("Documentary writing instructions"),
                value=saved_options.get("instructions", ""),
                key=_key(case_id, "instructions"),
            )
        if st.form_submit_button(
            tr("Generate documentary outline"), disabled=not selected, type="primary"
        ):
            try:
                _queue(
                    writer,
                    workspace,
                    case_id,
                    {
                        "title": title.strip(),
                        "target_minutes": minutes,
                        "language": language.strip(),
                        "claim_ids": selected,
                        "instructions": instructions,
                        "stage": "outline",
                    },
                    tr,
                )
                st.session_state[_key(case_id, "creating")] = False
            except Exception as exc:
                st.error(str(exc))
        st.session_state[_key(case_id, "saved_options")] = {
            "title": title,
            "target_minutes": minutes,
            "language": language,
            "claim_ids": selected,
            "instructions": instructions,
        }


def _render_scenes(document, tr):
    from app.services.targeted_search.documentary import clean_narration_text

    citations = {
        row["id"]: row for row in (document.get("packet") or {}).get("citations", [])
    }
    draft = document.get("draft") or document.get("outline") or {}
    for chapter in draft.get("chapters", []):
        st.subheader(chapter.get("title") or chapter["chapter_id"])
        for scene in chapter.get("scenes", []):
            with st.expander(scene.get("title") or scene["scene_id"], expanded=True):
                if scene.get("purpose"):
                    st.write(scene["purpose"])
                for passage in scene.get("passages", []):
                    st.write(clean_narration_text(passage["text"]))
                    st.caption(
                        tr("Documentary passage citations")
                        + ": "
                        + "; ".join(
                            _citation_label(citations[value])
                            for value in passage.get("citation_ids", [])
                            if value in citations
                        )
                    )
                    for quote in passage.get("quotes", []):
                        st.write(tr("Documentary original source quote"))
                        st.text(quote["text"])
                        if quote["citation_id"] in citations:
                            st.caption(_citation_label(citations[quote["citation_id"]]))
                if scene.get("footage_queries"):
                    st.write(tr("Documentary footage requirements"))
                    for query in scene["footage_queries"]:
                        st.text(query)
                if scene.get("evidence_gaps"):
                    st.write(tr("Documentary evidence gaps"))
                    st.write(scene["evidence_gaps"])
    with st.expander(tr("Documentary retained source citations")):
        for citation in citations.values():
            _render_citation(citation, tr)
    with st.expander(tr("Documentary source details")):
        st.json(list(citations.values()))


def _render_editor(writer, case_id, document, tr):
    from app.services.targeted_search.documentary import clean_narration_text

    draft = document.get("draft")
    if not draft:
        return
    choices = {
        (chapter_index, scene_index): scene
        for chapter_index, chapter in enumerate(draft.get("chapters", []))
        for scene_index, scene in enumerate(chapter.get("scenes", []))
    }
    if not choices:
        return
    with st.container():
        st.subheader(tr("Edit documentary draft"))
        review = document.get("human_review") or {}
        if not review.get("approved") and review.get("notes"):
            st.caption(tr("Documentary review notes"))
            st.text(review["notes"])
        if document.get("factual_review") and not _supported(document):
            with st.expander(tr("Documentary factual review"), expanded=True):
                for index, passage in enumerate(
                    document["factual_review"].get("passages", []), start=1
                ):
                    if passage.get("status") != "supported":
                        st.caption(
                            f"{tr('Documentary review passage')} {index}: "
                            + passage.get("reason", "")
                        )
        selection = st.selectbox(
            tr("Documentary scene to edit"),
            list(choices),
            format_func=lambda value: (
                choices[value].get("title") or choices[value]["scene_id"]
            ),
            key=_document_key(case_id, document, "edit_scene"),
        )
        scene = choices[selection]
        packet = document.get("packet") or {}
        claims = {row["id"]: row for row in packet.get("claims", [])}
        citations = {row["id"]: row for row in packet.get("citations", [])}
        with st.form(_document_key(case_id, document, "edit_" + scene["scene_id"])):
            passages = []
            for passage in scene.get("passages", []):
                prefix = "edit_" + scene["scene_id"] + "_" + passage["passage_id"]
                text = st.text_area(
                    tr("Documentary narration passage"),
                    value=clean_narration_text(passage["text"]),
                    key=_document_key(case_id, document, prefix + "_text"),
                )
                with st.expander(tr("Documentary evidence links")):
                    claim_ids = st.multiselect(
                        tr("Documentary passage claims"),
                        list(claims),
                        default=passage.get("claim_ids", []),
                        format_func=lambda value: claims[value]["text"],
                        key=_document_key(case_id, document, prefix + "_claims"),
                    )
                    citation_ids = st.multiselect(
                        tr("Documentary passage citations"),
                        list(citations),
                        default=passage.get("citation_ids", []),
                        format_func=lambda value: (
                            _citation_label(citations[value])
                            + (
                                " · "
                                + " ".join(citations[value]["quote"].split())[:90]
                                if citations[value].get("quote")
                                else ""
                            )
                        ),
                        key=_document_key(case_id, document, prefix + "_citations"),
                    )
                    quotes = passage.get("quotes", [])
                    quote_ids = st.multiselect(
                        tr("Documentary quoted source excerpts"),
                        list(range(len(quotes))),
                        default=list(range(len(quotes))),
                        format_func=lambda value, quotes=quotes: quotes[value]["text"],
                        key=_document_key(case_id, document, prefix + "_quotes"),
                    )
                passages.append(
                    {
                        **passage,
                        "text": text,
                        "claim_ids": claim_ids,
                        "citation_ids": citation_ids,
                        "quotes": [quotes[value] for value in quote_ids],
                    }
                )
            queries = st.text_area(
                tr("Documentary footage search queries"),
                value="\n".join(scene.get("footage_queries", [])),
                key=_document_key(case_id, document, scene["scene_id"] + "_queries"),
            )
            gaps = st.text_area(
                tr("Documentary unresolved evidence gaps"),
                value="\n".join(scene.get("evidence_gaps", [])),
                key=_document_key(case_id, document, scene["scene_id"] + "_gaps"),
            )
            if st.form_submit_button(tr("Save documentary revision")):
                try:
                    edited = deepcopy(draft)
                    chapter_index, scene_index = selection
                    target = edited["chapters"][chapter_index]["scenes"][scene_index]
                    target.update(
                        passages=passages,
                        footage_queries=[
                            line.strip()
                            for line in queries.splitlines()
                            if line.strip()
                        ],
                        evidence_gaps=[
                            line.strip() for line in gaps.splitlines() if line.strip()
                        ],
                    )
                    writer.save_revision(
                        case_id,
                        document["id"],
                        edited,
                        expected_revision=document["revision"],
                    )
                    st.success(tr("Documentary revision saved"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))


def _stage_options(document, stage):
    options = dict(document.get("options") or {})
    options["target_minutes"] = normalize_documentary_minutes(options.get("target_minutes"))
    options.update(stage=stage, document_id=document["id"])
    return options


def _render_review(writer, workspace, case_id, document, tr):
    if not document.get("draft"):
        st.info(tr("Documentary narration requires draft"))
        return
    if st.button(
        tr("Run documentary factual review"),
        key=_document_key(case_id, document, "factual_review"),
    ):
        try:
            _queue(
                writer,
                workspace,
                case_id,
                _stage_options(document, "factual_review"),
                tr,
            )
        except Exception as exc:
            st.error(str(exc))
    factual = document.get("factual_review")
    if factual:
        st.write(tr("Documentary factual review"))
        from app.services.targeted_search.documentary import clean_narration_text

        passages = {
            passage["passage_id"]: passage
            for chapter in document["draft"].get("chapters", [])
            for scene in chapter.get("scenes", [])
            for passage in scene.get("passages", [])
        }
        statuses = {
            "supported": tr("Documentary fact supported"),
            "contradicted": tr("Documentary fact contradicted"),
            "insufficient": tr("Documentary fact insufficient"),
        }
        st.dataframe(
            [
                {
                    tr("Documentary review passage"): (
                        f"{index}. "
                        + clean_narration_text(
                            passages.get(passage.get("passage_id"), {}).get("text", "")
                        )[:180]
                    ),
                    tr("Documentary review assessment"): statuses.get(
                        passage.get("status"), passage.get("status", "")
                    ),
                    tr("Documentary review reason"): passage.get("reason", ""),
                }
                for index, passage in enumerate(factual.get("passages", []), start=1)
            ],
            hide_index=True,
        )
        if factual.get("notes"):
            st.write(factual["notes"])
    if factual and not _supported(document):
        st.warning(tr("Documentary fact check blockers"))
    st.caption(tr("Documentary human review help"))
    if document.get("human_review"):
        review = document["human_review"]
        st.caption(
            str(review.get("reviewed_by", ""))
            + " · "
            + tr(
                "Documentary human approved"
                if review.get("approved")
                else "Documentary human revision requested"
            )
        )
        if review.get("notes"):
            st.text(review["notes"])
    supported = _supported(document)
    with st.form(_document_key(case_id, document, "human_review")):
        st.text_input(
            tr("Documentary reviewer"),
            key=_document_key(case_id, document, "reviewer"),
        )
        st.text_area(
            tr("Documentary review notes"),
            key=_document_key(case_id, document, "review_notes"),
        )
        st.form_submit_button(
            tr("Approve documentary final script"),
            disabled=not supported,
            type="primary",
            key=_document_key(case_id, document, "approve"),
            on_click=_save_human_review,
            args=(writer, case_id, document, True, tr),
        )
        st.form_submit_button(
            tr("Request documentary revision"),
            key=_document_key(case_id, document, "request_revision"),
            on_click=_save_human_review,
            args=(writer, case_id, document, False, tr),
        )


def _render_exports(writer, case_id, document, tr):
    if not document.get("draft"):
        st.info(tr("Documentary narration requires draft"))
        return
    export_key = _key(case_id, document["id"] + "_export")
    approved = bool((document.get("human_review") or {}).get("approved"))
    if not approved:
        st.info(tr("Documentary review draft export help"))
    for final, label in (
        (True, "Export documentary final script"),
        (False, "Export documentary draft"),
    ):
        if st.button(
            tr(label),
            disabled=final and not approved,
            key=_document_key(case_id, document, "export_" + str(final)),
            type="primary" if final and approved else "secondary",
        ):
            try:
                result = writer.export(
                    case_id,
                    document["id"],
                    final=final,
                    expected_revision=document["revision"],
                )
                st.session_state[export_key] = result
                st.success(tr("Documentary exported to Production folder"))
            except Exception as exc:
                st.error(str(exc))
    export = st.session_state.get(export_key) or {}
    if export.get("revision") != document["revision"]:
        return
    for file in export.get("files", []):
        try:
            path = writer.export_content(
                case_id,
                document["id"],
                file["filename"],
                final=export.get("final", False),
                expected_revision=document["revision"],
            )
            st.download_button(
                tr("Download documentary file") + ": " + file["filename"],
                path.read_bytes(),
                file_name=file["filename"],
                key=_document_key(case_id, document, "download_" + file["filename"]),
            )
        except Exception as exc:
            st.error(str(exc))
    if export.get("final") and export.get("script_asset_id"):
        st.caption(tr("Documentary production handoff help"))
        if st.button(
            tr("Send documentary script to Production"),
            key=_document_key(case_id, document, "handoff"),
        ):
            try:
                path = writer.export_content(
                    case_id,
                    document["id"],
                    "Final_Script.md",
                    final=True,
                    expected_revision=document["revision"],
                )
                st.session_state[f"case_{case_id}_script_asset"] = export[
                    "script_asset_id"
                ]
                st.session_state[f"case_{case_id}_storyboard_script"] = path.read_text(
                    encoding="utf-8"
                )
                st.session_state[f"case_{case_id}_storyboard_title"] = document["title"]
                request_view(case_id, "Production")
            except Exception as exc:
                st.error(str(exc))


def _job_matches_document(job, document):
    payload = job.get("payload") or {}
    result = job.get("result") or {}
    options = payload.get("options") or {}
    document_id = options.get("document_id") or result.get("document_id") or result.get("id")
    if document_id != document["id"]:
        return False
    revisions = [payload.get("document_revision")]
    if job.get("status") == "complete":
        revisions.append(result.get("revision"))
    known_revisions = {value for value in revisions if value is not None}
    return not known_revisions or document.get("revision") in known_revisions


def _render_job_details(jobs, tr):
    for job in jobs:
        st.caption(f"{job['id']} · {tr(friendly_status(job.get('status')))}")
        payload = job.get("payload") or {}
        result = job.get("result") or {}
        document_id = (
            (payload.get("options") or {}).get("document_id")
            or result.get("document_id")
            or result.get("id")
        )
        if document_id:
            st.caption(document_id)
        if job.get("last_error"):
            st.text(job["last_error"])


@st.fragment(run_every="2s")
def _render_jobs(workspace, case_id, tr, document=None):
    jobs = [
        job
        for job in workspace.search_service.list_jobs()
        if (job.get("payload") or {}).get("case_id") == case_id
        and "documentary" in job.get("job_type", "")
    ]
    current = jobs if document is None else [
        job for job in jobs if _job_matches_document(job, document)
    ]
    current_ids = {job["id"] for job in current}
    other = [job for job in jobs if job["id"] not in current_ids]
    active = [job for job in current if job.get("status") in {"queued", "running", "retry"}]
    for job in active or current[:1]:
        st.caption(
            tr("Documentary generation job")
            + ": "
            + tr(friendly_status(job.get("status")))
        )
        if job.get("last_error") and job.get("status") != "complete":
            st.error(job["last_error"])
    if current:
        with st.expander(tr("Documentary job details")):
            _render_job_details(current, tr)
    if other:
        with st.expander(tr("Documentary other case jobs")):
            st.caption(tr("Documentary other case jobs help"))
            _render_job_details(other, tr)


def render_documentary_writer(workspace, case, tr):
    writer = get_writer(workspace)
    case_id = case["id"]
    render_section_header(
        tr("Documentary Writer"), tr("Documentary writing desk help")
    )
    flash = st.session_state.pop(_key(case_id, "flash"), None)
    if flash:
        (st.error if flash[0] == "error" else st.success)(flash[1])
    rows = writer.list_documents(case_id)
    by_id = {row["id"]: row for row in rows}
    st.button(tr("Refresh documentary drafts"), key=_key(case_id, "refresh_documents"))
    creating = st.session_state.get(_key(case_id, "creating"), not rows)
    if creating or not rows:
        if rows and st.button(
            tr("Back to documentary drafts"), key=_key(case_id, "back_to_drafts")
        ):
            st.session_state[_key(case_id, "creating")] = False
            st.rerun()
        render_steps(
            [
                tr("Documentary workflow outline"),
                tr("Documentary workflow draft"),
                tr("Documentary workflow fact check"),
                tr("Documentary workflow approval"),
                tr("Documentary workflow export"),
            ],
            0,
        )
        if not rows:
            render_empty_state(
                tr("Documentary first draft"), tr("Documentary drafts will appear here")
            )
        _render_new_document(writer, workspace, case, tr)
        _render_jobs(workspace, case_id, tr)
        return
    selector, new_document = st.columns([4, 1], vertical_alignment="bottom")
    document_labels = {
        value: f"{row['title']} · {tr(friendly_status(row.get('status')))}"
        for value, row in by_id.items()
    }
    with selector:
        selected = st.selectbox(
            tr("Saved documentary"),
            list(by_id),
            format_func=document_labels.get,
            key=_key(case_id, "selected_document"),
        )
    with new_document:
        if st.button(tr("New documentary"), key=_key(case_id, "new_document_button")):
            st.session_state[_key(case_id, "creating")] = True
            st.rerun()
    _render_jobs(workspace, case_id, tr, document=by_id[selected])
    try:
        document = writer.get_document(case_id, selected)
    except Exception as exc:
        st.error(str(exc))
        return
    st.caption(
        tr("Documentary revision")
        + f": {document['revision']} · {tr(friendly_status(document.get('status')))}"
    )
    if document.get("stale") or document.get("content_withheld"):
        st.error(document.get("guard_error") or tr("Documentary evidence changed"))
        return
    if document.get("draft"):
        _render_narration_stats(document, tr)
    render_steps(
        [
            tr("Documentary workflow outline"),
            tr("Documentary workflow draft"),
            tr("Documentary workflow fact check"),
            tr("Documentary workflow approval"),
            tr("Documentary workflow export"),
        ],
        _workflow_index(document),
    )
    views = [
        "Documentary read view",
        "Documentary edit view",
        "Documentary review view",
        "Documentary export view",
    ]
    view_labels = {value: tr(value) for value in views}
    view = st.radio(
        tr("Documentary desk view"),
        views,
        format_func=view_labels.get,
        horizontal=True,
        key=_view_key(case_id, document),
    )
    if view == "Documentary edit view":
        if not document.get("draft"):
            st.info(tr("Documentary narration requires draft"))
        _render_editor(writer, case_id, document, tr)
    elif view == "Documentary review view":
        _render_review(writer, workspace, case_id, document, tr)
    elif view == "Documentary export view":
        _render_exports(writer, case_id, document, tr)
    else:
        _render_next_action(writer, workspace, case_id, document, tr)
        spoken_tab, cited_tab = st.tabs(
            [tr("Documentary spoken narration"), tr("Documentary cited narration")]
        )
        with spoken_tab:
            if document.get("draft"):
                from app.services.targeted_search.documentary import clean_narration_text

                with st.container(key=_document_key(case_id, document, "narration")):
                    for chapter in document["draft"].get("chapters", []):
                        st.subheader(chapter.get("title") or chapter["chapter_id"])
                        for scene in chapter.get("scenes", []):
                            for passage in scene.get("passages", []):
                                st.write(clean_narration_text(passage["text"]))
                st.caption(tr("Documentary original sound separate help"))
            else:
                st.info(tr("Documentary narration requires draft"))
        with cited_tab:
            _render_scenes(document, tr)


DOCUMENTARY_TRANSLATION_KEYS = frozenset(
    {
        "Documentary Writer",
        "Documentary source has no indexed text",
        "Documentary generation queued",
        "Documentary evidence provider help",
        "Documentary reviewed claims",
        "Documentary reviewed evidence required",
        "Documentary evidence packet preview",
        "Preview documentary evidence",
        "Documentary evidence gaps",
        "Documentary title",
        "Documentary target minutes",
        "Documentary duration help",
        "Documentary spoken word count",
        "Documentary estimated narration minutes",
        "Documentary estimate unavailable",
        "Documentary narration estimate help",
        "Documentary narration estimate unavailable help",
        "Documentary narration estimate outside band",
        "Documentary narration language",
        "Documentary writing instructions",
        "Generate documentary outline",
        "Documentary passage citations",
        "Documentary original source quote",
        "Documentary footage requirements",
        "Documentary retained source citations",
        "Edit documentary draft",
        "Documentary scene to edit",
        "Documentary narration passage",
        "Documentary passage claims",
        "Documentary quoted source excerpts",
        "Documentary footage search queries",
        "Documentary unresolved evidence gaps",
        "Save documentary revision",
        "Documentary revision saved",
        "Write cited documentary draft",
        "Run documentary factual review",
        "Documentary factual review",
        "Documentary human review help",
        "Documentary reviewer",
        "Documentary review notes",
        "Approve documentary final script",
        "Request documentary revision",
        "Documentary review saved",
        "Export documentary draft",
        "Export documentary final script",
        "Documentary exported to Production folder",
        "Download documentary file",
        "Documentary production handoff help",
        "Send documentary script to Production",
        "Documentary generation job",
        "Documentary drafts will appear here",
        "Saved documentary",
        "Documentary revision",
        "Documentary evidence changed",
        "Documentary cited narration",
        "Documentary spoken narration",
        "Documentary original sound separate help",
        "Documentary narration requires draft",
        "Documentary human approved",
        "Documentary human revision requested",
        "Documentary writing desk help",
        "New documentary",
        "Back to documentary drafts",
        "Documentary first draft",
        "Documentary workflow outline",
        "Documentary workflow draft",
        "Documentary workflow fact check",
        "Documentary workflow approval",
        "Documentary workflow export",
        "Documentary desk view",
        "Documentary read view",
        "Documentary edit view",
        "Documentary review view",
        "Documentary export view",
        "Documentary next draft help",
        "Documentary next fact check help",
        "Documentary next approval help",
        "Documentary next export help",
        "Review documentary for approval",
        "Open documentary revision",
        "Open documentary exports",
        "Documentary evidence links",
        "Documentary fact check blockers",
        "Documentary source details",
        "Open documentary evidence review",
        "Documentary review draft export help",
        "Documentary writing preferences",
        "Documentary fact supported",
        "Documentary fact contradicted",
        "Documentary fact insufficient",
        "Documentary review passage",
        "Documentary review assessment",
        "Documentary review reason",
        "Documentary job details",
        "Documentary other case jobs",
        "Documentary other case jobs help",
        "Refresh documentary drafts",
        "Documentary revision requested help",
        "Documentary reviewed claims need evidence",
        "Documentary claims to fix",
        "Documentary claim classification required",
        "Documentary claim supporting evidence required",
        "Documentary claim current citations required",
        "Documentary claim reviewer required",
    }
)
