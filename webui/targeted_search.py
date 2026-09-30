"""Caption-first clip search UI, loaded only when the user opens the library."""

from datetime import datetime, time, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import streamlit as st

from webui.case_workspace import CASE_WORKSPACE_TRANSLATION_KEYS

TARGETED_SEARCH_TRANSLATION_KEYS = (
    frozenset(
        {
            "Add source collection",
            "All collections",
            "All rights statuses",
            "Approve and extract selected clip",
            "Approved playlist or channel URL",
            "Authorized transcription help",
            "Caption language filter",
            "Caption-first search help",
            "Clear selected search clips",
            "Clip end seconds",
            "Clip extraction queued",
            "Clip generation export rights required",
            "Clip selected for generation",
            "Clip start seconds",
            "Collection discovery limit",
            "Collection discovery queued",
            "Collection name",
            "Collection name required",
            "Collection queries",
            "Collection saved",
            "Collection topic",
            "Confirm selected range relevance",
            "Context reranking",
            "Disabled",
            "Discover approved source",
            "Discover metadata and captions",
            "Discover selected source collection",
            "Discovering source collection",
            "Discovery captions only help",
            "Discovery queued",
            "Download clip provenance",
            "Download extracted clip",
            "Enabled",
            "Evidence time range",
            "Extract clip for",
            "Extracted clip",
            "Extracted clips",
            "Import source captions",
            "Import timed source captions",
            "Index authorized visual evidence",
            "Index semantic evidence",
            "Indexing queued",
            "Library source",
            "Metadata evidence range help",
            "Metadata registered",
            "Metadata registration help",
            "No search jobs yet",
            "No video evidence found",
            "OCR indexing",
            "Open source",
            "Open source at evidence",
            "Permitted use",
            "Policy expires",
            "Policy expiry date",
            "Refresh source library",
            "Register metadata only",
            "Remove selected clip",
            "Retrieval scores",
            "Review source rights",
            "Reviewed by",
            "Rights evidence and reason",
            "Rights filter",
            "Rights review fields required",
            "Rights review saved",
            "Rights status",
            "Save collection",
            "Save rights review",
            "Search capabilities",
            "Search clips",
            "Search clips from source library",
            "Search clips generation help",
            "Search optional model help",
            "Search processing jobs",
            "Search query required",
            "Search result",
            "Search video evidence",
            "Select collection before discovery",
            "Selected search clips",
            "Semantic retrieval",
            "Source URL",
            "Source caption counts",
            "Source caption language",
            "Source captions indexed",
            "Source collection",
            "Source creator",
            "Source description",
            "Source library",
            "Source rights help",
            "Source title",
            "Targeted search disabled",
            "Transcribe authorized source audio",
            "Use clip as video material",
            "Validate contextual evidence",
            "Visual indexing",
            "evidence_type.metadata",
            "evidence_type.ocr",
            "evidence_type.transcript",
            "evidence_type.visual",
            "evidence_type.visual_description",
            "requested_use.analysis",
            "requested_use.archival",
            "requested_use.clip_export",
            "requested_use.generated_export",
            "requested_use.internal_review",
            "requested_use.publication",
            "rights_status.allowed_export",
            "rights_status.allowed_internal",
            "rights_status.blocked",
            "rights_status.expired",
            "rights_status.review_required",
            "rights_status.unknown",
        }
    )
    | CASE_WORKSPACE_TRANSLATION_KEYS
)


SELECTED_ARTIFACTS_KEY = "targeted_search_selected_artifact_ids"
ACTIVE_SCOPE_KEY = "targeted_search_active_scope"
SCOPED_ARTIFACTS_KEY = "targeted_search_scoped_artifact_ids"


def switch_scope(case_id=None, state=None):
    """Keep generation selections and transient evidence within one workspace."""
    state = st.session_state if state is None else state
    scope = case_id or "source-library"
    previous = state.get(ACTIVE_SCOPE_KEY, "source-library")
    if previous == scope:
        state.setdefault(ACTIVE_SCOPE_KEY, scope)
        return
    selections = dict(state.get(SCOPED_ARTIFACTS_KEY, {}))
    selections[previous] = selected_artifact_ids(state)
    state[SELECTED_ARTIFACTS_KEY] = list(selections.get(scope, []))
    state[SCOPED_ARTIFACTS_KEY] = selections
    state[ACTIVE_SCOPE_KEY] = scope
    for key in list(state):
        if key in {
            "targeted_search_results",
            "targeted_search_result_select",
            "targeted_library_source",
            "targeted_search_clip_select",
            "targeted_search_job_ids",
            "case_support_results",
        } or key.startswith("targeted_validation_"):
            del state[key]


@st.cache_resource(show_spinner=False)
def get_search_service():
    # Keep optional search dependencies out of the ordinary generation startup.
    from app.services.targeted_search.service import SearchService
    from app.services.targeted_search.worker import ensure_worker_running

    service = SearchService()
    if _setting(service, "enabled", True):
        ensure_worker_running(root_dir=service.repo.root)
    return service


def selected_artifact_ids(state=None):
    state = st.session_state if state is None else state
    values = state.get(SELECTED_ARTIFACTS_KEY, [])
    return list(
        dict.fromkeys(value for value in values if isinstance(value, str) and value)
    )


def select_artifact(artifact_id, state=None):
    state = st.session_state if state is None else state
    values = selected_artifact_ids(state)
    if artifact_id and artifact_id not in values:
        values.append(artifact_id)
    state[SELECTED_ARTIFACTS_KEY] = values


def remove_artifact(artifact_id, state=None):
    state = st.session_state if state is None else state
    state[SELECTED_ARTIFACTS_KEY] = [
        value for value in selected_artifact_ids(state) if value != artifact_id
    ]


def restore_artifact_selection(params, state=None):
    state = st.session_state if state is None else state
    switch_scope(None, state)
    state["targeted_search_case"] = None
    values = params.get("search_artifact_ids") or []
    state[SELECTED_ARTIFACTS_KEY] = list(
        dict.fromkeys(value for value in values if isinstance(value, str) and value)
    )


def timestamp_link(url, start_ms=None):
    """Return a public link, preserving source identity parameters such as v=."""
    try:
        parsed = urlsplit(str(url))
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password:
        return None
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if start_ms is not None:
        query["t"] = str(max(0, int(start_ms) // 1000))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def checked_range(start_seconds, end_seconds, duration_ms=None):
    start_ms = round(float(start_seconds) * 1000)
    end_ms = round(float(end_seconds) * 1000)
    if start_ms < 0 or end_ms <= start_ms:
        raise ValueError("Choose an end time after the start time.")
    if duration_ms and end_ms > int(duration_ms):
        raise ValueError("The selected range extends beyond the source duration.")
    return start_ms, end_ms


def _track_job(value):
    job_id = value.get("job_id") or (value.get("id") if value.get("job_type") else None)
    if job_id:
        jobs = st.session_state.setdefault("targeted_search_job_ids", [])
        if job_id not in jobs:
            jobs.append(job_id)


def _setting(service, key, default=False):
    settings = service.settings
    return (
        settings.get(key, default)
        if isinstance(settings, dict)
        else getattr(settings, key, default)
    )


def _error(exc):
    # Errors from services are sanitized; never print raw extractor metadata.
    st.error(str(exc))


def _render_capabilities(service, tr):
    with st.expander(tr("Search capabilities")):
        st.caption(tr("Caption-first search help"))
        for label, setting in (
            ("Semantic retrieval", "semantic_enabled"),
            ("Context reranking", "rerank_enabled"),
            ("Visual indexing", "visual_enabled"),
            ("OCR indexing", "ocr_enabled"),
        ):
            state = tr("Enabled") if _setting(service, setting) else tr("Disabled")
            st.write(f"{tr(label)}: {state}")
        st.caption(tr("Search optional model help"))
        capability_reader = getattr(service, "capabilities", None)
        if callable(capability_reader):
            capabilities = capability_reader()
            if capabilities.get("warnings"):
                for warning in capabilities["warnings"]:
                    st.warning(str(warning))


def _render_library(service, tr, collection_id=None, case_mode=False, on_source=None):
    st.button(tr("Refresh source library"), key="targeted_search_refresh_library")
    if not case_mode:
        collections = service.list_collections()
        labels = {None: tr("All collections")}
        labels.update({row["id"]: row["name"] for row in collections})
        collection_id = st.selectbox(
            tr("Source collection"),
            list(labels),
            format_func=labels.get,
            key="targeted_search_collection",
        )
    with st.expander(tr("Add source collection")) if not case_mode else st.container():
        if not case_mode:
            with st.form("targeted_search_add_collection"):
                name = st.text_input(tr("Collection name"))
                topic = st.text_input(tr("Collection topic"))
                queries = st.text_area(tr("Collection queries"))
                if st.form_submit_button(tr("Save collection")):
                    try:
                        if not name.strip():
                            raise ValueError(tr("Collection name required"))
                        service.add_collection(
                            name.strip(),
                            topic.strip(),
                            queries=[
                                line.strip()
                                for line in queries.splitlines()
                                if line.strip()
                            ],
                        )
                        st.success(tr("Collection saved"))
                    except Exception as exc:
                        _error(exc)

    with st.expander(tr("Discover approved source")):
        st.caption(tr("Discovery captions only help"))
        with st.form("targeted_search_discover"):
            source_url = st.text_input(tr("Source URL"), key="targeted_discover_url")
            if st.form_submit_button(tr("Discover metadata and captions")):
                try:
                    source = service.discover(
                        source_url.strip(), collection_id=collection_id
                    )
                    if on_source:
                        on_source(source["id"])
                    _track_job(source)
                    st.success(tr("Discovery queued"))
                except Exception as exc:
                    _error(exc)
        with st.form("targeted_search_expand_collection"):
            collection_url = st.text_input(tr("Approved playlist or channel URL"))
            source_limit = st.number_input(
                tr("Collection discovery limit"), min_value=1, max_value=500, value=20
            )
            if st.form_submit_button(tr("Discover selected source collection")):
                try:
                    if not collection_id:
                        raise ValueError(tr("Select collection before discovery"))
                    with st.spinner(tr("Discovering source collection")):
                        result = service.expand_collection(
                            collection_url.strip(),
                            collection_id,
                            limit=int(source_limit),
                        )
                    for source in result.get("sources", []):
                        _track_job(source)
                        if on_source:
                            on_source(source["id"])
                    st.success(
                        tr("Collection discovery queued").format(
                            count=result.get("count", 0)
                        )
                    )
                except Exception as exc:
                    _error(exc)
        with st.form("targeted_search_register_metadata"):
            st.caption(tr("Metadata registration help"))
            url = st.text_input(tr("Source URL"), key="targeted_metadata_url")
            title = st.text_input(tr("Source title"))
            creator = st.text_input(tr("Source creator"))
            description = st.text_area(tr("Source description"))
            if st.form_submit_button(tr("Register metadata only")):
                try:
                    source = service.register_metadata(
                        url.strip(),
                        title.strip(),
                        description.strip(),
                        creator_name=creator.strip(),
                        collection_id=collection_id,
                    )
                    if on_source:
                        on_source(source["id"])
                    st.success(tr("Metadata registered"))
                except Exception as exc:
                    _error(exc)

    sources = [
        row
        for row in service.list_sources(collection_id=collection_id)
        if (row.get("metadata") or {}).get("asset_kind", "video") == "video"
    ]
    with st.expander(tr("Source library").format(count=len(sources))):
        if sources:
            by_id = {row["id"]: row for row in sources}
            selected_source = st.selectbox(
                tr("Library source"),
                list(by_id),
                key="targeted_library_source",
                format_func=lambda value: source_label(by_id[value]),
            )
            row = by_id[selected_source]
            policy = row.get("policy") or {}
            st.write(row.get("title") or row.get("canonical_url") or row["id"])
            st.caption(
                f"{row.get('state', '')} · {policy.get('rights_status', 'unknown')}"
            )
            link = timestamp_link(row.get("canonical_url", ""))
            if link:
                st.link_button(tr("Open source"), link, key=f"source_link_{row['id']}")
            st.caption(
                tr("Source caption counts").format(
                    captions=row.get("caption_count", 0),
                    chunks=row.get("chunk_count", 0),
                )
            )
            captions_file = st.file_uploader(
                tr("Import timed source captions"),
                type=["vtt", "srt", "json"],
                key=f"targeted_captions_{row['id']}",
            )
            caption_language = st.text_input(
                tr("Source caption language"),
                value="en",
                key=f"targeted_caption_language_{row['id']}",
            )
            if st.button(
                tr("Import source captions"),
                disabled=captions_file is None,
                key=f"targeted_import_captions_{row['id']}",
            ):
                try:
                    caption_format = (
                        Path(captions_file.name).suffix.removeprefix(".").lower()
                    )
                    service.import_captions(
                        row["id"],
                        captions_file.getvalue().decode("utf-8-sig"),
                        language=caption_language.strip() or "en",
                        kind="manual",
                        format=caption_format,
                    )
                    st.success(tr("Source captions indexed"))
                except Exception as exc:
                    _error(exc)
            if not row.get("caption_count"):
                st.caption(tr("Authorized transcription help"))
                if st.button(
                    tr("Transcribe authorized source audio"),
                    key=f"targeted_transcribe_{row['id']}",
                ):
                    try:
                        _track_job(service.enqueue_transcription(row["id"]))
                        st.success(tr("Indexing queued"))
                    except Exception as exc:
                        _error(exc)
            if _setting(service, "semantic_enabled") and st.button(
                tr("Index semantic evidence"),
                key=f"source_embeddings_{row['id']}",
            ):
                try:
                    _track_job(service.enqueue_embeddings(row["id"]))
                    st.success(tr("Indexing queued"))
                except Exception as exc:
                    _error(exc)
            if (
                _setting(service, "visual_enabled") or _setting(service, "ocr_enabled")
            ) and st.button(
                tr("Index authorized visual evidence"),
                key=f"source_visuals_{row['id']}",
            ):
                try:
                    _track_job(service.enqueue_visual_index(row["id"]))
                    st.success(tr("Indexing queued"))
                except Exception as exc:
                    _error(exc)
    return collection_id


def _render_policy(service, source, tr):
    policy = source.get("policy") or {}
    rights_values = [
        "unknown",
        "allowed_internal",
        "allowed_export",
        "review_required",
        "blocked",
        "expired",
    ]
    with st.expander(tr("Review source rights")):
        st.caption(tr("Source rights help"))
        with st.form(f"targeted_policy_{source['id']}"):
            status = policy.get("rights_status", "unknown")
            rights_labels = {
                value: tr(f"rights_status.{value}") for value in rights_values
            }
            rights_status = st.selectbox(
                tr("Rights status"),
                rights_values,
                index=rights_values.index(status) if status in rights_values else 0,
                format_func=rights_labels.get,
            )
            use_options = [
                "internal_review",
                "analysis",
                "generated_export",
                "publication",
                "clip_export",
                "archival",
            ]
            configured_uses = [
                value.strip()
                for value in (policy.get("permitted_use") or "internal_review").split(
                    ","
                )
            ]
            use_labels = {value: tr(f"requested_use.{value}") for value in use_options}
            permitted_uses = st.multiselect(
                tr("Permitted use"),
                use_options,
                default=[value for value in configured_uses if value in use_options],
                format_func=use_labels.get,
            )
            reason = st.text_area(
                tr("Rights evidence and reason"),
                value=policy.get("reason") or policy.get("policy_reason") or "",
            )
            reviewed_by = st.text_input(
                tr("Reviewed by"), value=policy.get("reviewed_by") or "local-user"
            )
            has_expiry = st.checkbox(
                tr("Policy expires"), value=bool(policy.get("expires_at"))
            )
            previous_expiry = (
                datetime.fromisoformat(
                    policy["expires_at"].replace("Z", "+00:00")
                ).date()
                if policy.get("expires_at")
                else datetime.now(timezone.utc).date()
            )
            expires = st.date_input(
                tr("Policy expiry date"), value=previous_expiry, disabled=not has_expiry
            )
            if st.form_submit_button(tr("Save rights review")):
                try:
                    if (
                        not reason.strip()
                        or not reviewed_by.strip()
                        or not permitted_uses
                    ):
                        raise ValueError(tr("Rights review fields required"))
                    expiry = (
                        datetime.combine(expires, time.min, timezone.utc).isoformat()
                        if has_expiry
                        else None
                    )
                    service.set_policy(
                        source["id"],
                        rights_status,
                        ",".join(permitted_uses),
                        reason.strip(),
                        reviewed_by.strip(),
                        expires_at=expiry,
                    )
                    st.success(tr("Rights review saved"))
                except Exception as exc:
                    _error(exc)


def _render_candidate(service, result, tr):
    candidate_id = result.get("candidate_id") or result["id"]
    source = service.get_source(result["source_id"])
    st.write(result.get("title") or source.get("title") or source["id"])
    evidence_type = result.get("evidence_type", "metadata")
    st.caption(tr(f"evidence_type.{evidence_type}"))
    st.text(result.get("evidence") or "")
    if evidence_type == "metadata":
        st.info(tr("Metadata evidence range help"))
    elif result.get("start_ms") is not None:
        st.caption(
            tr("Evidence time range").format(
                start=result["start_ms"] / 1000,
                end=result["end_ms"] / 1000,
            )
        )
    link = timestamp_link(
        result.get("canonical_url") or source.get("canonical_url"),
        result.get("start_ms"),
    )
    if link:
        st.link_button(tr("Open source at evidence"), link)
    with st.expander(tr("Retrieval scores")):
        st.json(result.get("scores") or {})
    _render_policy(service, source, tr)

    duration_ms = source.get("duration_ms")
    start = max(0.0, (result.get("start_ms") or 0) / 1000)
    end = (result.get("end_ms") or min(duration_ms or 30000, 30000)) / 1000
    first, second = st.columns(2)
    start_seconds = first.number_input(
        tr("Clip start seconds"),
        min_value=0.0,
        value=float(start),
        step=0.1,
        key=f"targeted_start_{candidate_id}",
    )
    end_seconds = second.number_input(
        tr("Clip end seconds"),
        min_value=0.0,
        value=float(max(end, start + 0.1)),
        step=0.1,
        key=f"targeted_end_{candidate_id}",
    )
    if st.button(
        tr("Validate contextual evidence"), key=f"targeted_validate_{candidate_id}"
    ):
        try:
            validation = service.validate(candidate_id)
            st.session_state[f"targeted_validation_{candidate_id}"] = validation
        except Exception as exc:
            _error(exc)
    validation = st.session_state.get(f"targeted_validation_{candidate_id}")
    if validation:
        st.write(validation.get("reason") or validation.get("decision") or validation)
    use_options = ["internal_review", "generated_export", "analysis"]
    use_labels = {value: tr(f"requested_use.{value}") for value in use_options}
    requested_use = st.selectbox(
        tr("Extract clip for"),
        use_options,
        format_func=use_labels.get,
        key=f"targeted_use_{candidate_id}",
    )
    relevant = st.checkbox(
        tr("Confirm selected range relevance"), key=f"targeted_relevance_{candidate_id}"
    )
    reviewer = st.text_input(
        tr("Reviewed by"), value="local-user", key=f"targeted_reviewer_{candidate_id}"
    )
    if st.button(
        tr("Approve and extract selected clip"),
        disabled=not relevant,
        key=f"targeted_extract_{candidate_id}",
    ):
        try:
            if not reviewer.strip():
                raise ValueError(tr("Rights review fields required"))
            start_ms, end_ms = checked_range(start_seconds, end_seconds, duration_ms)
            service.approve_download(
                candidate_id,
                requested_use=requested_use,
                reviewed_by=reviewer.strip(),
                start_ms=start_ms,
                end_ms=end_ms,
            )
            job = service.enqueue_clip(
                candidate_id,
                start_ms=start_ms,
                end_ms=end_ms,
                requested_use=requested_use,
            )
            _track_job(job)
            st.success(tr("Clip extraction queued"))
        except Exception as exc:
            _error(exc)


def _render_query(service, collection_id, tr, on_result=None, source_ids=None):
    with st.form("targeted_search_query"):
        query = st.text_input(
            tr("Search video evidence"), placeholder=tr("Footage search placeholder")
        )
        with st.expander(tr("Search filters")):
            language = st.text_input(tr("Caption language filter"), placeholder="en")
            rights_options = [
                None,
                "allowed_export",
                "allowed_internal",
                "review_required",
                "unknown",
            ]
            rights_labels = {
                value: tr("All rights statuses")
                if value is None
                else tr(f"rights_status.{value}")
                for value in rights_options
            }
            rights = st.selectbox(
                tr("Rights filter"),
                rights_options,
                format_func=rights_labels.get,
            )
        if st.form_submit_button(tr("Search clips"), type="primary"):
            try:
                if not query.strip():
                    raise ValueError(tr("Search query required"))
                filters = {
                    "collection_id": collection_id,
                    "language": language.strip() or None,
                    "rights_status": rights,
                }
                if source_ids is not None:
                    filters["source_ids"] = list(source_ids)
                response = service.search(
                    query.strip(),
                    filters={
                        key: value
                        for key, value in filters.items()
                        if value is not None
                    },
                )
                st.session_state["targeted_search_results"] = response.get(
                    "results", []
                )
            except Exception as exc:
                _error(exc)
    results = st.session_state.get("targeted_search_results")
    if results is None:
        st.caption(tr("Footage search starting help"))
        return
    if not results:
        st.info(tr("No video evidence found"))
        return
    by_id = {row.get("candidate_id") or row["id"]: row for row in results}
    result_labels = {key: candidate_label(row, tr) for key, row in by_id.items()}
    selected = st.selectbox(
        tr("Search result"),
        list(by_id),
        format_func=result_labels.get,
        key="targeted_search_result_select",
    )
    _render_candidate(service, by_id[selected], tr)
    if on_result:
        on_result(by_id[selected])


def source_label(source):
    title = source.get("title") or source.get("source_id") or source.get("id", "")
    creator = source.get("creator_name", "")
    parsed = urlsplit(source.get("canonical_url", ""))
    catalog = (
        parsed.path.rstrip("/").split("/")[-1]
        if parsed.hostname == "tegna.kurator.com"
        else ""
    )
    return " · ".join(str(value) for value in (title, creator, catalog) if value)


def candidate_label(result, tr):
    label = source_label(result)
    if result.get("start_ms") is not None:
        label += f" · {result['start_ms'] / 1000:.3f}–{result['end_ms'] / 1000:.3f}s"
    return (
        label + " · " + tr("evidence_type." + result.get("evidence_type", "metadata"))
    )


def attach_selected_clip(service, artifact_id, state=None):
    from app.services.targeted_search.attachments import attach_clip

    # The server verifies current policy, source identity, and artifact hashes.
    reference = attach_clip(
        artifact_id, requested_use="generated_export", root_dir=service.repo.root
    )
    select_artifact(reference.get("artifact_id") or artifact_id, state)
    return reference


def _artifact_path(service, artifact_id, requested_use="internal_review"):
    from app.services.targeted_search.attachments import artifact_content

    return Path(
        artifact_content(
            artifact_id, requested_use=requested_use, root_dir=service.repo.root
        )
    )


def _render_clip(service, artifact, tr):
    artifact_id = artifact["id"]
    source = service.get_source(artifact["source_id"])
    metadata = artifact.get("metadata") or {}
    with st.expander(source.get("title") or artifact_id):
        try:
            preview = metadata.get("proxy_artifact_id") or artifact_id
            permitted_use = metadata.get("requested_use") or "internal_review"
            st.video(str(_artifact_path(service, preview, permitted_use)))
            clip = _artifact_path(service, artifact_id, permitted_use)
            st.download_button(
                tr("Download extracted clip"),
                clip.read_bytes(),
                file_name=clip.name,
                mime="video/mp4",
                key=f"targeted_download_{artifact_id}",
            )
            manifest_id = metadata.get("manifest_artifact_id")
            if manifest_id:
                manifest = _artifact_path(service, manifest_id, permitted_use)
                st.download_button(
                    tr("Download clip provenance"),
                    manifest.read_bytes(),
                    file_name=manifest.name,
                    mime="application/json",
                    key=f"targeted_manifest_{artifact_id}",
                )
        except Exception as exc:
            _error(exc)
        policy = source.get("policy") or {}
        allowed = policy.get("rights_status") == "allowed_export"
        if artifact_id in selected_artifact_ids():
            st.success(tr("Clip selected for generation"))
            if st.button(
                tr("Remove selected clip"), key=f"targeted_remove_{artifact_id}"
            ):
                remove_artifact(artifact_id)
                st.rerun(scope="fragment")
        elif st.button(
            tr("Use clip as video material"),
            disabled=not allowed,
            key=f"targeted_attach_{artifact_id}",
        ):
            try:
                attach_selected_clip(service, artifact_id)
                st.success(tr("Clip selected for generation"))
            except Exception as exc:
                _error(exc)
        if not allowed:
            st.caption(tr("Clip generation export rights required"))


@st.fragment(run_every="2s")
def _render_jobs_and_clips(service, tr, source_ids=None, collection_id=None):
    jobs = service.list_jobs()
    if source_ids is not None:
        scope = set(source_ids)
        jobs = [
            row
            for row in jobs
            if (row.get("payload") or {}).get("source_id") in scope
            or (
                collection_id is not None
                and (row.get("payload") or {}).get("collection_id") == collection_id
            )
        ]
    active_jobs = [
        row
        for row in jobs
        if row.get("status") in {"queued", "retry", "running", "failed", "blocked"}
    ]
    with st.expander(tr("Search processing jobs"), expanded=bool(active_jobs)):
        for job in jobs[:12]:
            status = job.get("status", "unknown")
            st.caption(f"{job.get('job_type', '')} · {status}")
            if status in {"failed", "blocked"} and job.get("last_error"):
                st.error(job["last_error"])
        if not jobs:
            st.caption(tr("No search jobs yet"))
    artifacts = [row for row in service.list_artifacts() if row.get("kind") == "clip"]
    if source_ids is not None:
        artifacts = [
            row for row in artifacts if row.get("source_id") in set(source_ids)
        ]
    if artifacts:
        st.write(tr("Extracted clips"))
        by_id = {row["id"]: row for row in artifacts}
        selected = st.selectbox(
            tr("Extracted clip"),
            list(by_id),
            key="targeted_search_clip_select",
            format_func=lambda value: (
                f"{by_id[value]['source_id']} · {(by_id[value].get('start_ms') or 0) / 1000:g}s–{(by_id[value].get('end_ms') or 0) / 1000:g}s"
            ),
        )
        _render_clip(service, by_id[selected], tr)


def render_search_button(tr, params):
    """Open footage on the full-page desk and retain refs for video generation."""
    st.session_state.setdefault(SELECTED_ARTIFACTS_KEY, [])
    if st.button(tr("Search clips from source library"), key="targeted_search_open"):
        st.session_state["application_pending_workspace"] = "documentary"
        st.session_state["case_workspace_pending_view"] = "Footage Search"
        st.rerun()
    refs = selected_artifact_ids()
    params.search_artifact_ids = refs
    if refs:
        st.caption(tr("Selected search clips").format(count=len(refs)))
        if st.button(tr("Clear selected search clips"), key="targeted_search_clear"):
            st.session_state[SELECTED_ARTIFACTS_KEY] = []
            params.search_artifact_ids = []
