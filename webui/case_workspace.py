"""Full-page case workspace for footage, evidence, writing, and production."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import streamlit as st

from webui.documentary_writer import DOCUMENTARY_TRANSLATION_KEYS
from webui.acoustic_pipeline import ACOUSTIC_TRANSLATION_KEYS

VIEWS = (
    "Footage Search",
    "Library",
    "Search Everything",
    "Timeline / Claims",
    "Documentary Writer",
    "Cinematic Sound",
    "Production",
)
KINDS = (
    "document",
    "audio",
    "video",
    "image",
    "map",
    "script",
    "transcript",
    "other",
    "reference",
)
ROLES = ("broll", "original_sound", "still", "document", "map")
ASSET_ROLES = {
    "audio": ("original_sound",),
    "video": ("broll", "original_sound"),
    "document": ("document", "still", "map"),
    "image": ("still", "map"),
    "map": ("map", "still"),
}
CLAIM_STATUSES = ("proposed", "reviewed", "disputed", "insufficient_support")


def get_workspace(service):
    from app.services.targeted_search.case_workspace import CaseWorkspace

    return CaseWorkspace(service)


def _key(case_id, name):
    return f"case_{case_id}_{name}"


def _error(exc):
    st.error(str(exc))


def asset_label(asset):
    if (asset.get("metadata") or {}).get("linked"):
        return f"{asset.get('filename') or asset['id']} · {asset.get('source_id') or asset['id']}"
    return str(asset.get("relative_path") or asset.get("filename") or asset["id"])


def locator_label(locator):
    locator = locator or {}
    if locator.get("kind") == "page":
        return (
            f"p. {locator.get('page_label') or int(locator.get('page_index', 0)) + 1}"
        )
    if locator.get("kind") == "time" and locator.get("start_ms") is not None:
        return f"{locator['start_ms'] / 1000:.3f}–{locator['end_ms'] / 1000:.3f}s"
    if locator.get("kind") == "script":
        return f"characters {locator.get('char_start', 0)}–{locator.get('char_end', 0)}"
    if locator.get("kind") == "image":
        return "image region" if locator.get("bbox") else "image"
    return ""


def citation_from_result(result):
    citation = {"relation": "supports"}
    if result.get("unit_id"):
        citation["unit_id"] = result["unit_id"]
    elif result.get("asset_id") and result.get("locator"):
        citation.update(asset_id=result["asset_id"], locator=result["locator"])
    else:
        raise ValueError("This result does not have a citable evidence location.")
    if result.get("asset_version_id"):
        citation["asset_version_id"] = result["asset_version_id"]
    if citation.get("unit_id") and (result.get("evidence") or result.get("text")):
        citation["quote"] = result.get("evidence") or result["text"]
    return citation


def save_evidence(case_id, result, state=None):
    state = st.session_state if state is None else state
    basket = dict(state.get(_key(case_id, "evidence"), {}))
    citation = citation_from_result(result)
    identifier = hashlib.sha256(
        json.dumps(citation, sort_keys=True).encode()
    ).hexdigest()[:20]
    basket[identifier] = {
        "citation": citation,
        "label": f"{result.get('filename') or result.get('asset_id', '')} · {locator_label(result.get('locator'))}",
    }
    state[_key(case_id, "evidence")] = basket
    return identifier


def _citations_input(
    case_id, tr, name, existing=None, label="Saved evidence citations"
):
    basket = st.session_state.get(_key(case_id, "evidence"), {})
    selected = st.multiselect(
        tr(label),
        list(basket),
        format_func=lambda value: basket[value]["label"],
        key=_key(case_id, name),
    )
    values = [dict(basket[value]["citation"]) for value in selected] or list(
        existing or []
    )
    return [
        {"unit_id": value, "relation": "supports"}
        if isinstance(value, str)
        else {
            key: entry
            for key, entry in value.items()
            if key
            in {
                "unit_id",
                "asset_id",
                "asset_version_id",
                "locator",
                "quote",
                "relation",
            }
            and entry is not None
        }
        for value in values
    ]


def render_asset_preview(workspace, asset, tr, locator=None, quote=""):
    from app.services.targeted_search.case_media import asset_content, preview_asset

    try:
        path = Path(
            asset_content(workspace, asset["id"], requested_use="internal_review")
        )
        kind, location = asset.get("asset_kind"), locator or {}
        st.caption(locator_label(location))
        if kind in {"document", "image", "map"} or (
            kind == "audio" and location.get("start_ms") is not None
        ):
            preview = preview_asset(
                workspace,
                asset["id"],
                locator=location or None,
                requested_use="internal_review",
            )
            # Only the policy-aware preview helper supplies derivative paths.
            from app.services.targeted_search.case_media import preview_content

            preview_path = Path(
                preview_content(
                    workspace, preview["id"], requested_use="internal_review"
                )
            )
            if kind == "audio":
                st.audio(str(preview_path))
            else:
                st.image(str(preview_path))
                if quote:
                    st.caption(tr("Highlighted evidence excerpt") + ": " + quote)
        elif kind == "audio":
            st.audio(str(path))
        elif kind == "video":
            st.video(
                str(path), start_time=max(0, int(location.get("start_ms", 0)) // 1000)
            )
        elif kind == "script" or path.suffix.lower() in {".md", ".txt", ".json"}:
            text = path.read_text(encoding="utf-8-sig")
            start = max(0, int(location.get("char_start", 0)))
            end = int(location.get("char_end", min(len(text), start + 20000)))
            st.code(
                text[max(0, start - 300) : min(len(text), end + 300)],
                language=None,
                wrap_lines=True,
            )
        else:
            st.info(tr("Preview unavailable for this asset"))
        if path.stat().st_size <= 50 * 1024 * 1024:
            st.download_button(
                tr("Download original asset"),
                path.read_bytes(),
                file_name=asset.get("filename") or path.name,
                key=_key(asset["case_id"], "download_" + asset["id"]),
            )
    except Exception as exc:
        _error(exc)


def _render_requests(workspace, case_id, tr):
    with st.expander(tr("Missing source requests")):
        rows = workspace.list_requests(case_id)
        if rows:
            st.dataframe(rows, hide_index=True)
        by_id = {row["id"]: row for row in rows}
        selected = st.selectbox(
            tr("Source request"),
            [None] + list(by_id),
            format_func=lambda value: (
                tr("New source request")
                if value is None
                else by_id[value].get("title", value)
            ),
            key=_key(case_id, "request_id"),
        )
        row = by_id.get(selected, {})
        with st.form(_key(case_id, "request_form_" + str(selected))):
            title = st.text_input(tr("Requested source"), value=row.get("title", ""))
            kind = st.selectbox(
                tr("Asset kind"),
                KINDS,
                index=KINDS.index(row.get("asset_kind", "video")),
            )
            statuses = ("missing", "requested", "received", "unavailable", "cancelled")
            status = st.selectbox(
                tr("Request status"),
                statuses,
                index=statuses.index(row.get("status", "missing")),
            )
            notes = st.text_area(tr("Request notes"), value=row.get("notes", ""))
            if st.form_submit_button(tr("Save source request")):
                try:
                    record = {
                        "title": title.strip(),
                        "asset_kind": kind,
                        "status": status,
                        "notes": notes,
                    }
                    if selected:
                        record["id"] = selected
                    workspace.save_request(case_id, record)
                    st.success(tr("Source request saved"))
                except Exception as exc:
                    _error(exc)


def _render_library(workspace, case, service, tr):
    from urllib.parse import urlsplit

    case_id = case["id"]
    st.caption(tr("Case originals help"))
    imported = st.session_state.pop(_key(case_id, "import_summary"), None)
    if imported:
        st.success(imported)
    assets = workspace.list_assets(case_id)
    by_id = {row["id"]: row for row in assets}
    sources = {row["source_id"]: service.get_source(row["source_id"]) for row in assets}
    display_labels = {}
    repeated_labels = {}
    for row in assets:
        if (row.get("metadata") or {}).get("linked"):
            source = sources[row["source_id"]]
            origin = (
                source.get("creator_name")
                or urlsplit(source.get("canonical_url", "")).hostname
            )
            label = " · ".join(
                str(value)
                for value in (row.get("filename") or source.get("title"), origin)
                if value
            )
            repeated_labels[label] = repeated_labels.get(label, 0) + 1
            display_labels[row["id"]] = (
                label
                if repeated_labels[label] == 1
                else f"{label} ({repeated_labels[label]})"
            )
        else:
            display_labels[row["id"]] = asset_label(row)
    states = {
        "imported": tr("Case source added"),
        "linked": tr("Case source linked"),
        "acquired": tr("Case source acquired"),
        "indexed": tr("Case source searchable"),
        "partial": tr("Case source partial"),
        "no_speech": tr("Case source no speech"),
    }
    st.dataframe(
        [
            {
                tr("Original filename"): display_labels[row["id"]],
                tr("Asset kind"): tr("case_kind." + row.get("asset_kind", "other")),
                tr("Rights status"): tr(
                    "rights_status."
                    + (sources[row["source_id"]].get("policy") or {}).get(
                        "rights_status", "unknown"
                    )
                ),
                tr("Index status"): states.get(
                    row.get("state"), str(row.get("state", "")).replace("_", " ")
                ),
                tr("Provenance review"): tr(
                    "Case source reviewed"
                    if (row.get("metadata") or {}).get("provenance_status")
                    == "reviewed"
                    else "Case source unreviewed"
                )
                if (row.get("metadata") or {}).get("provenance_status", "unreviewed")
                in {"reviewed", "unreviewed"}
                else (row.get("metadata") or {}).get("provenance_status"),
                tr("Category"): row.get("category", ""),
            }
            for row in assets
        ],
        hide_index=True,
    )
    if assets:
        retained_default = next(
            (index for index, row in enumerate(assets) if row.get("artifact_id")), 0
        )
        asset_id = st.selectbox(
            tr("Review case asset"),
            list(by_id),
            index=retained_default,
            format_func=display_labels.get,
            key=_key(case_id, "review_asset"),
        )
        asset = by_id[asset_id]
        with st.expander(tr("Preview original asset"), expanded=True):
            if (asset.get("metadata") or {}).get("linked") and not asset.get(
                "artifact_id"
            ):
                st.info(tr("Linked source original not acquired"))
            else:
                render_asset_preview(workspace, asset, tr)
        from webui import targeted_search as footage

        footage._render_policy(service, service.get_source(asset["source_id"]), tr)
        with st.expander(tr("Source file details")):
            st.caption(asset["source_id"])
            st.caption(
                f"SHA-256: {asset.get('sha256', '')} · v{asset.get('version', 1)}"
            )
        if (asset.get("metadata") or {}).get("linked") and st.button(
            tr("Refresh retained source original"),
            key=_key(case_id, "refresh_" + asset_id),
        ):
            try:
                updated = workspace.refresh_linked_asset(asset_id)
                st.success(
                    tr("Retained source refreshed")
                    if updated.get("artifact_id")
                    else tr("Linked source original not acquired")
                )
            except Exception as exc:
                _error(exc)
    else:
        st.info(tr("Case library empty help"))
    with st.expander(tr("Import case folder")):
        st.caption(tr("Case folder setup help"))
        relative = (case.get("metadata") or {}).get("workspace_relative_path")
        if relative:
            st.session_state.setdefault(
                _key(case_id, "folder"), str(service.repo.root / relative)
            )
        if st.button(tr("Prepare case folders"), key=_key(case_id, "prepare_folders")):
            try:
                from app.services.targeted_search.case_workspace_ops import (
                    prepare_case_folder,
                )

                prepared = prepare_case_folder(workspace, case_id)
                st.session_state[_key(case_id, "folder")] = str(
                    service.repo.root / prepared["workspace_relative_path"]
                )
                st.session_state[_key(case_id, "prepared_folder")] = st.session_state[
                    _key(case_id, "folder")
                ]
            except Exception as exc:
                _error(exc)
        prepared_path = st.session_state.get(_key(case_id, "prepared_folder"))
        if prepared_path:
            st.caption(tr("Prepared case folder help"))
            st.code(prepared_path, language=None)
        with st.form(_key(case_id, "import")):
            folder = st.text_input(tr("Local case folder"), key=_key(case_id, "folder"))
            if st.form_submit_button(tr("Import originals")):
                try:
                    result = workspace.import_folder(
                        case_id, folder.strip(), index=False
                    )
                    st.session_state[_key(case_id, "import_summary")] = tr(
                        "Case import summary"
                    ).format(
                        **{
                            name: result.get(name, 0)
                            for name in ("imported", "unchanged", "updated", "skipped")
                        }
                    )
                    st.rerun()
                except Exception as exc:
                    _error(exc)
    with st.expander(tr("Make case files searchable")):
        st.caption(tr("Case indexing help"))
        selected = st.multiselect(
            tr("Assets to index"),
            list(by_id),
            format_func=lambda value: asset_label(by_id[value]),
            key=_key(case_id, "index_assets"),
        )
        if st.button(
            tr("Index selected assets"),
            disabled=not selected,
            key=_key(case_id, "index_selected"),
        ):
            for asset_id in selected:
                try:
                    workspace.enqueue_index(asset_id)
                    st.success(
                        tr("Indexing queued") + " · " + asset_label(by_id[asset_id])
                    )
                except Exception as exc:
                    _error(exc)
    with st.expander(tr("Link existing footage source")):
        sources = service.list_sources()
        by_source = {
            row["id"]: row
            for row in sources
            if (row.get("metadata") or {}).get("asset_kind", "video") == "video"
        }
        if by_source:
            source_id = st.selectbox(
                tr("Library source"),
                list(by_source),
                format_func=lambda value: by_source[value].get("title") or value,
                key=_key(case_id, "link_source"),
            )
            if st.button(tr("Link source to case"), key=_key(case_id, "link")):
                try:
                    workspace.link_source(case_id, source_id)
                    st.success(tr("Source linked to case"))
                except Exception as exc:
                    _error(exc)
    _render_requests(workspace, case_id, tr)
    st.download_button(
        tr("Export case workspace"),
        json.dumps(workspace.export_case(case_id), ensure_ascii=False, indent=2),
        file_name="case-workspace.json",
        mime="application/json",
        key=_key(case_id, "export"),
    )


def _render_search_all(workspace, case, service, tr):
    case_id = case["id"]
    st.caption(tr("Independent modality ranking help"))
    with st.form(_key(case_id, "search_all")):
        query = st.text_input(tr("Search all case evidence"))
        kinds = st.multiselect(
            tr("Evidence asset kinds"),
            KINDS,
            default=[
                kind for kind in KINDS if kind not in {"script", "other", "reference"}
            ],
        )
        production = st.checkbox(tr("Include production drafts"), value=False)
        assets = workspace.list_assets(case_id)
        categories = sorted(
            {row.get("category", "") for row in assets if row.get("category")}
        )
        category_labels = {
            None: tr("All categories"),
            **{value: value for value in categories},
        }
        category = st.selectbox(
            tr("Evidence category filter"),
            [None] + categories,
            format_func=category_labels.get,
        )
        by_asset = {row["id"]: row for row in assets}
        asset_labels = {
            None: tr("All case assets"),
            **{key: asset_label(row) for key, row in by_asset.items()},
        }
        asset_id = st.selectbox(
            tr("Evidence source filter"),
            [None] + list(by_asset),
            format_func=asset_labels.get,
        )
        if st.form_submit_button(tr("Search case workspace")):
            try:
                if not query.strip():
                    raise ValueError(tr("Search query required"))
                filters = {
                    "include_production": production,
                    "asset_kinds": kinds,
                }
                if category:
                    filters["category"] = category
                if asset_id:
                    filters["asset_id"] = asset_id
                support = workspace.search_supporting(
                    case_id,
                    query.strip(),
                    filters=filters,
                )
                video_filters = {
                    "collection_id": case["collection_id"],
                    "include_production": production,
                }
                video_filters["source_ids"] = [
                    row["source_id"]
                    for row in assets
                    if row["asset_kind"] == "video"
                    and (not category or row.get("category") == category)
                    and (not asset_id or row["id"] == asset_id)
                ]
                videos = (
                    service.search(query.strip(), filters=video_filters)
                    if "video" in kinds
                    else {"results": []}
                )
                st.session_state[_key(case_id, "search_results")] = {
                    "support": support,
                    "videos": videos.get("results", []),
                }
            except Exception as exc:
                _error(exc)
    results = st.session_state.get(_key(case_id, "search_results"))
    if results is None:
        return
    from webui import targeted_search as footage

    if results["videos"]:
        st.write(tr("Footage matches"))
        by_id = {row.get("candidate_id") or row["id"]: row for row in results["videos"]}
        result_labels = {
            key: footage.candidate_label(row, tr) for key, row in by_id.items()
        }
        selected = st.selectbox(
            tr("Search result"),
            list(by_id),
            format_func=result_labels.get,
            key=_key(case_id, "video_result"),
        )
        footage._render_candidate(service, by_id[selected], tr)
        _render_footage_citation(workspace, case, by_id[selected], tr)
    groups = results["support"].get("groups") or {}
    if not groups:
        for row in results["support"].get("results", []):
            groups.setdefault(row.get("asset_kind", "document"), []).append(row)
    for kind in KINDS:
        rows = groups.get(kind, [])
        if not rows:
            continue
        st.write(tr("case_kind." + kind))
        by_id = {row.get("unit_id") or row["id"]: row for row in rows}
        selected = st.selectbox(
            tr("Supporting evidence result"),
            list(by_id),
            format_func=lambda value: (
                f"{by_id[value].get('filename', '')} · {locator_label(by_id[value].get('locator'))}"
            ),
            key=_key(case_id, "result_" + kind),
        )
        result = by_id[selected]
        st.caption(locator_label(result.get("locator")))
        st.text(result.get("evidence") or result.get("text") or "")
        if st.button(
            tr("Save evidence citation"), key=_key(case_id, "cite_" + selected)
        ):
            try:
                save_evidence(case_id, result)
                st.success(tr("Evidence citation saved"))
            except Exception as exc:
                _error(exc)
        with st.expander(tr("Preview evidence in context")):
            render_asset_preview(
                workspace,
                workspace.get_asset(result["asset_id"]),
                tr,
                result.get("locator"),
                result.get("evidence") or result.get("text", ""),
            )
    if not results["videos"] and not any(groups.values()):
        st.info(tr("No case evidence found"))


def _render_footage_citation(workspace, case, result, tr):
    assets = workspace.list_assets(case["id"])
    asset = next(
        (
            row
            for row in assets
            if row["source_id"] == result["source_id"] and row["asset_kind"] == "video"
        ),
        None,
    )
    if not asset:
        return
    if st.button(
        tr("Save footage range citation"),
        disabled=result.get("start_ms") is None,
        key=_key(
            case["id"],
            "footage_cite_" + str(result.get("candidate_id") or result["id"]),
        ),
    ):
        try:
            save_evidence(
                case["id"],
                {
                    "asset_id": asset["id"],
                    "asset_version_id": asset.get("asset_version_id"),
                    "filename": asset["filename"],
                    "locator": {
                        "kind": "time",
                        "start_ms": result["start_ms"],
                        "end_ms": result["end_ms"],
                    },
                },
            )
            st.success(tr("Evidence citation saved"))
        except Exception as exc:
            _error(exc)


def _fact_status_label(value, tr):
    labels = {
        "proposed": "Fact needs review",
        "reviewed": "Fact reviewed",
        "disputed": "Fact disputed",
        "insufficient_support": "Fact needs evidence",
    }
    return tr(labels[value]) if value in labels else str(value).replace("_", " ")


def _fact_class_label(value, tr):
    labels = {
        "unclassified": "Fact class unclassified",
        "allegation": "Fact class allegation",
        "testimony": "Fact class testimony",
        "police_report": "Fact class police report",
        "court_finding": "Fact class court finding",
        "news_report": "Fact class news report",
        "editorial": "Fact class editorial",
        "recording_observation": "Fact class recording observation",
    }
    return tr(labels[value]) if value in labels else str(value).replace("_", " ")


def _render_fact_citations(citations, assets, tr):
    for citation in citations:
        asset = assets.get(citation.get("asset_id"))
        source = asset_label(asset) if asset else tr("Saved source evidence")
        relation = {
            "supports": "Evidence supports",
            "contradicts": "Evidence conflicts",
            "mentions": "Evidence mentions",
        }.get(citation.get("relation", "supports"), "Evidence mentions")
        st.caption(
            " · ".join(
                value
                for value in (
                    tr(relation),
                    source,
                    locator_label(citation.get("locator")),
                )
                if value
            )
        )
        if citation.get("quote"):
            st.text(citation["quote"])
        if citation.get("is_current") is False:
            st.caption(tr("Evidence source changed"))


def _render_claims_timeline(workspace, case, tr):
    from webui.case_design import render_empty_state

    case_id = case["id"]
    section_labels = {value: tr(value) for value in ("Facts", "Timeline")}
    section = st.radio(
        tr("Facts and timeline"),
        ("Facts", "Timeline"),
        format_func=section_labels.get,
        horizontal=True,
        key=_key(case_id, "facts_section"),
    )
    assets = {row["id"]: row for row in workspace.list_assets(case_id)}
    if section == "Timeline":
        events = workspace.list_events(case_id)
        if not events:
            render_empty_state(tr("No timeline events yet"), tr("Timeline empty help"))
        for event in events:
            with st.container(border=True):
                st.caption(event.get("event_at") or tr("Event date unknown"))
                st.text(event.get("title", ""))
                if event.get("notes"):
                    st.write(event["notes"])
                if event.get("has_stale_citations"):
                    st.warning(tr("Timeline source changed help"))
                citations = event.get("citations", [])
                with st.expander(
                    tr("View source citations").format(count=len(citations))
                ):
                    _render_fact_citations(citations, assets, tr)
        with st.expander(tr("Add a timeline event")):
            with st.form(_key(case_id, "event_form")):
                title = st.text_input(tr("Event title"))
                event_at = st.text_input(
                    tr("Event date and time"), help=tr("Event date uncertainty help")
                )
                notes = st.text_area(tr("Event notes"))
                precision = st.selectbox(
                    tr("Event time precision"),
                    ("unknown", "year", "month", "day", "minute", "second", "range"),
                )
                citations = _citations_input(case_id, tr, "event_citations")
                if st.form_submit_button(tr("Save timeline event")):
                    try:
                        workspace.save_event(
                            case_id,
                            {
                                "title": title.strip(),
                                "event_at": event_at.strip() or None,
                                "time_precision": precision,
                                "notes": notes,
                                "citations": citations,
                            },
                        )
                        st.success(tr("Timeline event saved"))
                    except Exception as exc:
                        _error(exc)
        return

    st.caption(tr("Claims review help"))
    rows = workspace.list_claims(case_id)
    by_id = {row["id"]: row for row in rows}
    if not rows:
        render_empty_state(tr("No case facts yet"), tr("Case facts empty help"))
    for row in rows:
        with st.container(border=True):
            status = _fact_status_label(row.get("status", "proposed"), tr)
            assertion = _fact_class_label(
                row.get("assertion_class", "unclassified"), tr
            )
            st.caption(f"{status} · {assertion}")
            st.text(row.get("text", ""))
            if row.get("reviewed_by"):
                st.caption(tr("Fact reviewed by label") + ": " + row["reviewed_by"])
            if row.get("has_stale_citations"):
                st.warning(tr("Fact source changed help"))
            if row.get("notes"):
                st.write(row["notes"])
            citations = row.get("citations", [])
            with st.expander(tr("View source citations").format(count=len(citations))):
                _render_fact_citations(citations, assets, tr)
            if st.button(
                tr("Review this fact"), key=_key(case_id, "review_fact_" + row["id"])
            ):
                st.session_state[_key(case_id, "claim_id")] = row["id"]
                st.session_state[_key(case_id, "open_fact_review")] = True

    with st.expander(
        tr("Add or review a case fact"),
        expanded=st.session_state.get(_key(case_id, "open_fact_review"), False),
    ):
        claim_labels = {
            None: tr("New claim"),
            **{value: row.get("text", value) for value, row in by_id.items()},
        }
        selected = st.selectbox(
            tr("Claim to edit"),
            [None] + list(by_id),
            format_func=claim_labels.get,
            key=_key(case_id, "claim_id"),
        )
        row = by_id.get(selected, {})
        with st.form(_key(case_id, "claim_form_" + str(selected))):
            text = st.text_area(tr("Claim text"), value=row.get("text", ""))
            status_labels = {
                value: _fact_status_label(value, tr) for value in CLAIM_STATUSES
            }
            status = st.selectbox(
                tr("Claim status"),
                CLAIM_STATUSES,
                index=CLAIM_STATUSES.index(row.get("status", "proposed")),
                format_func=status_labels.get,
            )
            reviewer = st.text_input(
                tr("Claim reviewed by"), value=row.get("reviewed_by", "")
            )
            assertion_classes = (
                "unclassified",
                "allegation",
                "testimony",
                "police_report",
                "court_finding",
                "news_report",
                "editorial",
                "recording_observation",
            )
            assertion_labels = {
                value: _fact_class_label(value, tr) for value in assertion_classes
            }
            assertion_class = st.selectbox(
                tr("Claim assertion class"),
                assertion_classes,
                index=assertion_classes.index(
                    row.get("assertion_class", "unclassified")
                ),
                format_func=assertion_labels.get,
            )
            notes = st.text_area(tr("Claim review notes"), value=row.get("notes", ""))
            citations = _citations_input(
                case_id,
                tr,
                "claim_citations_" + str(selected),
                [
                    value
                    for value in row.get("citations", [])
                    if value.get("relation", "supports") == "supports"
                ],
                label="Supporting evidence citations",
            )
            contradictions = _citations_input(
                case_id,
                tr,
                "claim_contradictions_" + str(selected),
                [
                    value
                    for value in row.get("citations", [])
                    if value.get("relation") == "contradicts"
                ],
                label="Contradicting evidence citations",
            )
            if st.form_submit_button(tr("Save claim")):
                try:
                    record = {
                        "text": text.strip(),
                        "status": status,
                        "reviewed_by": reviewer.strip(),
                        "assertion_class": assertion_class,
                        "notes": notes,
                        "citations": [
                            {**value, "relation": "supports"} for value in citations
                        ]
                        + [
                            {**value, "relation": "contradicts"}
                            for value in contradictions
                        ]
                        + [
                            {
                                key: value
                                for key, value in citation.items()
                                if key
                                in {
                                    "unit_id",
                                    "asset_id",
                                    "asset_version_id",
                                    "locator",
                                    "quote",
                                    "relation",
                                }
                                and value is not None
                            }
                            for citation in row.get("citations", [])
                            if citation.get("relation") == "mentions"
                        ],
                    }
                    if selected:
                        record["id"] = selected
                    workspace.save_claim(case_id, record)
                    st.success(tr("Claim saved"))
                except Exception as exc:
                    _error(exc)


def validate_scene(scene, assets):
    if not str(scene.get("scene_id", "")).strip():
        raise ValueError("Each scene needs a stable scene ID.")
    if scene.get("asset_id") not in assets:
        raise ValueError("Select a case asset for this scene.")
    if scene.get("role") not in ROLES:
        raise ValueError("Select a supported scene role.")
    duration, speed = scene.get("duration_ms", 0), scene.get("speed", 1)
    if not math.isfinite(float(duration)) or not 100 <= duration <= 300000:
        raise ValueError("Scene duration must be between 0.1 and 300 seconds.")
    if not math.isfinite(float(speed)) or not 0.25 <= speed <= 4:
        raise ValueError("Playback speed must be between 0.25 and 4.")
    asset = assets[scene["asset_id"]]
    kind = asset.get("asset_kind")
    if scene["role"] not in ASSET_ROLES.get(kind, ()):
        raise ValueError("The scene role must match the selected asset type.")
    if kind in {"audio", "video"}:
        start, end = scene.get("source_start_ms"), scene.get("source_end_ms")
        if start is None or end is None or start < 0 or end <= start:
            raise ValueError("Timed assets need an end time after the start time.")
        duration_ms = (asset.get("metadata") or {}).get("duration_ms")
        if duration_ms and end > duration_ms:
            raise ValueError("The selected source range exceeds this asset's duration.")
    return scene


def _edited_locator(asset, previous, start_ms, end_ms, page_index):
    kind = asset["asset_kind"]
    old = (
        dict(previous.get("locator") or {})
        if previous.get("asset_id") == asset["id"]
        else {}
    )
    if kind == "document":
        return (
            old
            if old.get("kind") == "page" and old.get("page_index") == page_index
            else {"kind": "page", "page_index": page_index}
        )
    if kind in {"image", "map"}:
        return old if old.get("kind") == "image" else {"kind": "image"}
    if kind == "video" and old.get("kind") == "image":
        return old
    if (
        old.get("kind") == "word"
        and old.get("start_ms") == start_ms
        and old.get("end_ms") == end_ms
    ):
        return old
    extra = (
        {key: old[key] for key in ("channel", "speaker") if key in old}
        if old.get("kind") == "time"
        else {}
    )
    return {"kind": "time", "start_ms": start_ms, "end_ms": end_ms, **extra}


def _render_timing_import(workspace, case_id, assets, tr):
    with st.expander(tr("Import aligned word timestamps")):
        st.caption(tr("Word timing binding help"))
        audio = {
            row["id"]: row
            for row in assets
            if row.get("asset_kind") in {"audio", "video"}
        }
        scripts = {
            row["id"]: row for row in assets if row.get("asset_kind") == "script"
        }
        timing_assets = {
            row["id"]: row
            for row in assets
            if Path(row.get("filename", "")).suffix.lower() == ".json"
        }
        if not audio:
            st.info(tr("Import audio before word timestamps"))
            return
        with st.form(_key(case_id, "timing")):
            audio_id = st.selectbox(
                tr("Aligned audio asset"),
                list(audio),
                format_func=lambda value: asset_label(audio[value]),
            )
            scope = st.selectbox(tr("Word timing scope"), ("narration", "source"))
            script_labels = {
                None: tr("No script selected"),
                **{key: asset_label(row) for key, row in scripts.items()},
            }
            script_id = st.selectbox(
                tr("Aligned script asset"),
                [None] + list(scripts),
                format_func=script_labels.get,
            )
            payload = st.file_uploader(
                tr("WhisperX word timestamps JSON"), type=["json"]
            )
            timing_labels = {
                None: tr("Use uploaded timestamps JSON"),
                **{key: asset_label(row) for key, row in timing_assets.items()},
            }
            timing_id = st.selectbox(
                tr("Imported word timestamps asset"),
                [None] + list(timing_assets),
                format_func=timing_labels.get,
            )
            confirmed = st.checkbox(
                tr("Confirm word timing asset binding"), value=False
            )
            if st.form_submit_button(tr("Bind word timestamps")):
                try:
                    if payload is None and timing_id is None:
                        raise ValueError(tr("Word timestamps JSON required"))
                    if scope == "narration" and script_id is None:
                        raise ValueError(tr("Narration timestamps require script"))
                    if not confirmed:
                        raise ValueError(tr("Confirm word timing asset binding"))
                    from app.services.targeted_search.case_media import import_whisperx

                    if timing_id:
                        from app.services.targeted_search.case_media import (
                            asset_content,
                        )

                        timing_path = Path(
                            asset_content(
                                workspace, timing_id, requested_use="analysis"
                            )
                        )
                        data = json.loads(timing_path.read_bytes())
                    else:
                        data = json.loads(payload.getvalue())
                    if not isinstance(data, dict):
                        raise ValueError(tr("Word timestamps must be a JSON object"))
                    data.setdefault("audio_sha256", audio[audio_id]["sha256"])
                    if script_id:
                        data.setdefault("script_sha256", scripts[script_id]["sha256"])
                    result = import_whisperx(
                        workspace,
                        audio_id,
                        data,
                        scope=scope,
                        script_asset_id=script_id,
                    )
                    st.success(tr("Word timestamps bound"))
                    st.json(result)
                except Exception as exc:
                    _error(exc)


def _render_scene_editor(case_id, assets, claims, tr):
    by_id = {
        row["id"]: row
        for row in assets
        if row.get("asset_kind") in {"video", "audio", "document", "image", "map"}
    }
    if not by_id:
        st.info(tr("Import assets before storyboard"))
        return
    scenes_key = _key(case_id, "draft_scenes")
    scenes = list(st.session_state.get(scenes_key, []))
    scene_labels = {None: tr("New scene")}
    scene_labels.update(
        {
            index: tr("Production scene number").format(number=index + 1)
            + " · "
            + asset_label(by_id[scene["asset_id"]])
            if scene.get("asset_id") in by_id
            else tr("Production scene number").format(number=index + 1)
            for index, scene in enumerate(scenes)
        }
    )
    editing = st.selectbox(
        tr("Scene to edit"),
        [None] + list(range(len(scenes))),
        format_func=scene_labels.get,
        key=_key(case_id, "editing_scene"),
    )
    row = scenes[editing] if editing is not None else {}
    selected_asset = (
        row.get("asset_id") if row.get("asset_id") in by_id else next(iter(by_id))
    )
    asset_labels = {value: asset_label(asset) for value, asset in by_id.items()}
    asset_id = st.selectbox(
        tr("Scene source asset"),
        list(by_id),
        index=list(by_id).index(selected_asset),
        format_func=asset_labels.get,
    )
    roles = ASSET_ROLES[by_id[asset_id]["asset_kind"]]
    selected_role = row.get("role") if row.get("role") in roles else roles[0]
    with st.form(_key(case_id, "scene_form_" + str(editing))):
        scene_id = st.text_input(
            tr("Scene ID"), value=row.get("scene_id", f"scene_{len(scenes) + 1:03}")
        )
        role = st.selectbox(tr("Scene role"), roles, index=roles.index(selected_role))
        narration = st.text_area(tr("Scene narration"), value=row.get("narration", ""))
        start = st.number_input(
            tr("Scene source start seconds"),
            min_value=0.0,
            value=float(row.get("source_start_ms") or 0) / 1000,
            step=0.1,
        )
        end = st.number_input(
            tr("Scene source end seconds"),
            min_value=0.0,
            value=float(row.get("source_end_ms") or 5000) / 1000,
            step=0.1,
        )
        duration = st.number_input(
            tr("Scene duration seconds"),
            min_value=0.1,
            max_value=300.0,
            value=float(row.get("duration_ms") or 5000) / 1000,
            step=0.1,
        )
        speed = st.number_input(
            tr("Scene playback speed"),
            min_value=0.25,
            max_value=4.0,
            value=float(row.get("speed", 1)),
            step=0.05,
        )
        volume = st.number_input(
            tr("Scene audio volume"),
            min_value=0.0,
            max_value=4.0,
            value=float(row.get("volume", 1)),
            step=0.1,
        )
        visuals = [None] + [
            value
            for value, asset in by_id.items()
            if by_id[asset_id]["asset_kind"] == "audio"
            and asset.get("asset_kind") in {"image", "map", "document"}
        ]
        visual_labels = {None: tr("No visual overlay"), **asset_labels}
        visual_id = st.selectbox(
            tr("Visual asset for original sound"),
            visuals,
            index=visuals.index(row.get("visual_asset_id"))
            if row.get("visual_asset_id") in visuals
            else 0,
            format_func=visual_labels.get,
        )
        page = st.number_input(
            tr("Document page number"),
            min_value=1,
            value=int((row.get("locator") or {}).get("page_index", 0)) + 1,
        )
        background_page = st.number_input(
            tr("Background document page number"),
            min_value=1,
            value=int((row.get("visual_locator") or {}).get("page_index", 0)) + 1,
        )
        claim_by_id = {value["id"]: value for value in claims}
        claim_labels = {
            value: claim.get("text", value) for value, claim in claim_by_id.items()
        }
        claim_ids = st.multiselect(
            tr("Scene claim references"),
            list(claim_by_id),
            default=[
                value for value in row.get("claim_ids", []) if value in claim_by_id
            ],
            format_func=claim_labels.get,
        )
        citations = _citations_input(
            case_id, tr, "scene_citations_" + str(editing), row.get("citations")
        )
        if st.form_submit_button(tr("Save storyboard scene")):
            try:
                kind = by_id[asset_id].get("asset_kind")
                scene = {
                    "scene_id": scene_id.strip(),
                    "asset_id": asset_id,
                    "role": role,
                    "narration": narration,
                    "source_start_ms": round(start * 1000)
                    if kind in {"audio", "video"}
                    else None,
                    "source_end_ms": round(end * 1000)
                    if kind in {"audio", "video"}
                    else None,
                    "duration_ms": round(duration * 1000),
                    "speed": speed,
                    "volume": volume,
                    "claim_ids": claim_ids,
                    "citations": [
                        value["unit_id"] for value in citations if value.get("unit_id")
                    ],
                    "locator": _edited_locator(
                        by_id[asset_id],
                        row,
                        round(start * 1000),
                        round(end * 1000),
                        int(page) - 1,
                    ),
                }
                if visual_id:
                    scene["visual_asset_id"] = visual_id
                    visual_kind = by_id[visual_id]["asset_kind"]
                    previous_visual = (
                        dict(row.get("visual_locator") or {})
                        if row.get("visual_asset_id") == visual_id
                        else {}
                    )
                    if visual_kind == "document":
                        scene["visual_locator"] = (
                            previous_visual
                            if previous_visual.get("kind") == "page"
                            and previous_visual.get("page_index")
                            == int(background_page) - 1
                            else {
                                "kind": "page",
                                "page_index": int(background_page) - 1,
                            }
                        )
                    elif visual_kind in {"image", "map"}:
                        scene["visual_locator"] = (
                            previous_visual
                            if previous_visual.get("kind") == "image"
                            else {"kind": "image"}
                        )
                validate_scene(scene, by_id)
                if any(
                    other["scene_id"] == scene["scene_id"]
                    for index, other in enumerate(scenes)
                    if index != editing
                ):
                    raise ValueError(tr("Scene IDs must be unique"))
                if editing is None:
                    scenes.append(scene)
                else:
                    scenes[editing] = scene
                st.session_state[scenes_key] = scenes
                st.session_state.pop(_key(case_id, "scene_order"), None)
                st.success(tr("Storyboard scene saved"))
            except Exception as exc:
                _error(exc)
    if scenes:
        order_labels = {
            scene["scene_id"]: tr("Production scene number").format(number=index + 1)
            for index, scene in enumerate(scenes)
        }
        order = st.multiselect(
            tr("Storyboard scene order"),
            [row["scene_id"] for row in scenes],
            default=[row["scene_id"] for row in scenes],
            format_func=order_labels.get,
            key=_key(case_id, "scene_order"),
        )
        if st.button(tr("Apply scene order"), key=_key(case_id, "apply_order")):
            by_scene = {row["scene_id"]: row for row in scenes}
            st.session_state[scenes_key] = [by_scene[value] for value in order]
        st.dataframe(
            [
                {
                    tr("Production scene label"): tr("Production scene number").format(
                        number=index + 1
                    ),
                    tr("Production source label"): asset_labels.get(
                        scene.get("asset_id"), tr("Production source unavailable")
                    ),
                    tr("Production range label"): locator_label(scene.get("locator")),
                    tr("Production duration label"): round(
                        scene.get("duration_ms", 0) / 1000, 3
                    ),
                }
                for index, scene in enumerate(st.session_state[scenes_key])
            ],
            hide_index=True,
            width="stretch",
        )


def _render_production(workspace, case, tr):
    from webui.case_design import render_empty_state, render_steps, request_view

    case_id = case["id"]
    assets = workspace.list_assets(case_id)
    rows = workspace.list_storyboards(case_id)
    by_id = {row["id"]: row for row in rows}
    storyboard_labels = {
        None: tr("New storyboard"),
        **{value: row.get("title", value) for value, row in by_id.items()},
    }
    selected = st.selectbox(
        tr("Saved storyboard"),
        [None] + list(by_id),
        index=1 if by_id else 0,
        format_func=storyboard_labels.get,
        key=_key(case_id, "storyboard_id"),
    )
    render_slot = st.empty()
    saved_scenes = (by_id.get(selected) or {}).get("scenes", [])
    draft_scenes = st.session_state.get(_key(case_id, "draft_scenes"), [])
    if saved_scenes:
        st.caption(
            tr("Production edit summary").format(
                count=len(saved_scenes),
                seconds=round(
                    sum(row.get("duration_ms", 0) or 0 for row in saved_scenes) / 1000,
                    1,
                ),
            )
        )
    render_jobs = [
        job
        for job in workspace.search_service.list_jobs()
        if job.get("job_type") == "case_render"
        and (
            (job.get("payload") or {}).get("case_id") == case_id
            or (job.get("payload") or {}).get("storyboard_id") in by_id
        )
    ]
    if render_jobs:
        st.markdown("### " + tr("Production previews and render status"))
        _render_case_jobs(workspace, case, tr)
    else:
        with st.expander(tr("Case processing history")):
            _render_case_jobs(workspace, case, tr)
    render_steps(
        [
            tr("Production step scenes"),
            tr("Production step save"),
            tr("Production step render"),
        ],
        2 if saved_scenes else 1 if draft_scenes else 0,
    )
    if not saved_scenes and not draft_scenes:
        render_empty_state(tr("Production empty title"), tr("Production empty help"))
        script_col, footage_col = st.columns(2)
        with script_col:
            if st.button(
                tr("Production open script"), key=_key(case_id, "production_script")
            ):
                request_view(case_id, "Documentary Writer")
        with footage_col:
            if st.button(
                tr("Production find footage"), key=_key(case_id, "production_footage")
            ):
                request_view(case_id, "Footage Search")
    loaded = False
    if selected and st.button(
        tr("Load storyboard for editing"), key=_key(case_id, "load_storyboard")
    ):
        loaded = True
        row = by_id[selected]
        st.session_state[_key(case_id, "draft_scenes")] = row.get("scenes", [])
        st.session_state[_key(case_id, "storyboard_title")] = row.get("title", "")
        st.session_state[_key(case_id, "storyboard_script")] = row.get("script", "")
        st.session_state[_key(case_id, "narration_asset")] = row.get(
            "narration_asset_id"
        )
        st.session_state[_key(case_id, "script_asset")] = (
            row.get("metadata") or {}
        ).get("script_asset_id")
        st.session_state.pop(_key(case_id, "scene_order"), None)
    scripts = {row["id"]: row for row in assets if row.get("asset_kind") == "script"}
    with st.expander(tr("Production build scenes"), expanded=loaded):
        st.caption(tr("Explicit storyboard binding help"))
        _render_scene_editor(case_id, assets, workspace.list_claims(case_id), tr)
    audio = {row["id"]: row for row in assets if row.get("asset_kind") == "audio"}
    script_labels = {
        None: tr("No script selected"),
        **{value: asset_label(row) for value, row in scripts.items()},
    }
    audio_labels = {
        None: tr("No narration track"),
        **{value: asset_label(row) for value, row in audio.items()},
    }
    with st.expander(tr("Production narration and save"), expanded=loaded):
        st.caption(tr("Continuous narration timing help"))
        script_asset_id = st.selectbox(
            tr("Final narration script asset"),
            [None] + list(scripts),
            format_func=script_labels.get,
            key=_key(case_id, "script_asset"),
        )
        if script_asset_id and st.button(
            tr("Load narration script asset"), key=_key(case_id, "load_script")
        ):
            try:
                from app.services.targeted_search.case_media import asset_content

                script_path = Path(
                    asset_content(
                        workspace, script_asset_id, requested_use="internal_review"
                    )
                )
                st.session_state[_key(case_id, "storyboard_script")] = (
                    script_path.read_text(encoding="utf-8-sig")
                )
            except Exception as exc:
                _error(exc)
        save_form = st.form(_key(case_id, "save_storyboard"))
    with save_form:
        title = st.text_input(
            tr("Storyboard title"), key=_key(case_id, "storyboard_title")
        )
        script = st.text_area(
            tr("Final narration script"), key=_key(case_id, "storyboard_script")
        )
        narration_asset_id = st.selectbox(
            tr("Final narration audio asset"),
            [None] + list(audio),
            format_func=audio_labels.get,
            key=_key(case_id, "narration_asset"),
        )
        if st.form_submit_button(tr("Save storyboard")):
            try:
                scene_fields = {
                    "scene_id",
                    "asset_id",
                    "source_start_ms",
                    "source_end_ms",
                    "duration_ms",
                    "role",
                    "locator",
                    "citations",
                    "claim_ids",
                    "speed",
                    "volume",
                    "visual_asset_id",
                    "visual_locator",
                    "narration",
                }
                scenes = [
                    {key: value for key, value in scene.items() if key in scene_fields}
                    for scene in st.session_state.get(_key(case_id, "draft_scenes"), [])
                ]
                record = {
                    "title": title.strip(),
                    "script": script,
                    "narration_asset_id": narration_asset_id,
                    "scenes": scenes,
                    "metadata": {
                        "script_sha256": hashlib.sha256(script.encode()).hexdigest()
                    },
                }
                if script_asset_id:
                    record["metadata"]["script_asset_id"] = script_asset_id
                if selected:
                    record["id"] = selected
                result = workspace.save_storyboard(case_id, record)
                st.session_state[_key(case_id, "last_saved_storyboard")] = result["id"]
                st.success(tr("Storyboard saved"))
            except Exception as exc:
                _error(exc)
    render_id = selected or st.session_state.get(_key(case_id, "last_saved_storyboard"))
    with render_slot.container():
        if st.button(
            tr("Render saved storyboard"),
            disabled=not render_id,
            type="primary",
            key=_key(case_id, "render_storyboard"),
        ):
            try:
                job = workspace.enqueue_render(
                    render_id, requested_use="generated_export"
                )
                st.session_state[_key(case_id, "render_job")] = job["id"]
                st.success(tr("Storyboard render queued"))
            except Exception as exc:
                _error(exc)
    _render_timing_import(workspace, case_id, assets, tr)


@st.fragment(run_every="2s")
def _render_case_jobs(workspace, case, tr):
    service = workspace.search_service
    asset_ids = {row["id"] for row in workspace.list_assets(case["id"])}
    storyboard_ids = {row["id"] for row in workspace.list_storyboards(case["id"])}
    for job in service.list_jobs():
        payload = job.get("payload") or {}
        if (
            payload.get("case_id") != case["id"]
            and payload.get("asset_id") not in asset_ids
            and payload.get("storyboard_id") not in storyboard_ids
        ):
            continue
        st.caption(f"{job.get('job_type', '')} · {job.get('status', '')}")
        if job.get("last_error"):
            st.error(job["last_error"])
        result = job.get("result") or {}
        if (
            job.get("status") in {"complete", "completed"}
            and result.get("artifact_id")
            and job.get("job_type") == "case_render"
        ):
            try:
                from app.services.targeted_search.case_media import preview_content

                path = Path(
                    preview_content(
                        workspace,
                        result["artifact_id"],
                        requested_use="generated_export",
                    )
                )
                st.video(str(path))
                st.download_button(
                    tr("Download storyboard video"),
                    path.read_bytes(),
                    file_name=path.name,
                    key=_key(case["id"], "render_download_" + job["id"]),
                )
                if result.get("manifest_artifact_id"):
                    manifest = Path(
                        preview_content(
                            workspace,
                            result["manifest_artifact_id"],
                            requested_use="generated_export",
                        )
                    )
                    st.download_button(
                        tr("Download storyboard provenance"),
                        manifest.read_bytes(),
                        file_name=manifest.name,
                        key=_key(case["id"], "render_manifest_" + job["id"]),
                    )
            except Exception as exc:
                _error(exc)


WORKFLOW_VIEWS = tuple(view for view in VIEWS if view != "Search Everything")
VIEW_LABELS = {
    "Footage Search": "Workspace footage",
    "Library": "Workspace sources",
    "Timeline / Claims": "Workspace facts",
    "Documentary Writer": "Workspace script",
    "Cinematic Sound": "Workspace sound",
    "Production": "Workspace edit",
}
VIEW_DESCRIPTIONS = {
    "Footage Search": "Workspace footage help",
    "Library": "Workspace sources help",
    "Timeline / Claims": "Workspace facts help",
    "Production": "Workspace edit help",
}


def case_readiness(workspace, case_id):
    """Count retained evidence separately from leads and production assets."""
    assets = workspace.list_assets(case_id)
    production_roles = {
        "production",
        "narration",
        "script",
        "production_transcript",
        "sound_effect",
        "sfx",
    }
    evidence = [
        row
        for row in assets
        if (row.get("metadata") or {}).get("role") not in production_roles
        and row.get("asset_kind") != "script"
    ]
    return {
        "retained": sum(bool(row.get("artifact_id")) for row in evidence),
        "footage_leads": sum(
            row.get("asset_kind") == "video" and not row.get("artifact_id")
            for row in evidence
        ),
        "reviewed_claims": sum(
            row.get("status") == "reviewed"
            and bool(row.get("reviewed_by"))
            and bool(row.get("citations"))
            and not row.get("has_stale_citations")
            for row in workspace.list_claims(case_id)
        ),
    }


def _render_workspace_body(workspace, service, case, view, tr):
    from webui import targeted_search as footage
    from webui.case_design import render_empty_state, render_section_header

    case_id = case["id"] if case else None
    if view in VIEW_DESCRIPTIONS:
        render_section_header(tr(VIEW_LABELS[view]), tr(VIEW_DESCRIPTIONS[view]))
    if view == "Footage Search":
        # The query is the first working control. Adding sources is a secondary task.
        collection_id = (
            case["collection_id"]
            if case
            else st.session_state.get("targeted_search_collection")
        )
        if not case:
            collections = service.list_collections()
            labels = {
                None: tr("All collections"),
                **{row["id"]: row["name"] for row in collections},
            }
            collection_id = st.selectbox(
                tr("Source collection"),
                list(labels),
                format_func=labels.get,
                key="targeted_search_collection",
            )
        source_ids = workspace.get_case(case_id)["source_ids"] if case else None
        footage._render_query(
            service,
            collection_id,
            tr,
            source_ids=source_ids,
            on_result=(
                lambda result: _render_footage_citation(workspace, case, result, tr)
            )
            if case
            else None,
        )
        with st.expander(tr("Manage footage sources")):
            footage._render_library(
                service,
                tr,
                collection_id=collection_id,
                case_mode=True,
                on_source=(lambda source_id: workspace.link_source(case_id, source_id))
                if case
                else None,
            )
        footage._render_jobs_and_clips(
            service,
            tr,
            source_ids=source_ids,
            collection_id=collection_id if case else None,
        )
        footage._render_capabilities(service, tr)
    elif case is None:
        render_empty_state(tr("Choose a case to continue"), tr("Choose a case help"))
    elif view == "Library":
        source_views = ("Browse sources", "Find evidence")
        source_labels = {value: tr(value) for value in source_views}
        section = st.radio(
            tr("Case source view"),
            source_views,
            format_func=source_labels.get,
            horizontal=True,
            key=_key(case_id, "source_tab"),
        )
        if section == "Find evidence":
            _render_search_all(workspace, case, service, tr)
        else:
            _render_library(workspace, case, service, tr)
        with st.expander(tr("Case processing history")):
            _render_case_jobs(workspace, case, tr)
    elif view == "Timeline / Claims":
        _render_claims_timeline(workspace, case, tr)
    elif view == "Documentary Writer":
        from webui.documentary_writer import render_documentary_writer

        render_documentary_writer(workspace, case, tr)
    elif view == "Cinematic Sound":
        from webui.acoustic_pipeline import render_acoustic_pipeline

        render_acoustic_pipeline(workspace, case, tr)
    elif view == "Production":
        _render_production(workspace, case, tr)


def render_workspace(service, tr):
    from html import escape
    from webui import targeted_search as footage
    from webui.case_design import inject_case_styles, render_empty_state

    if not footage._setting(service, "enabled", True):
        st.info(tr("Targeted search disabled"))
        return
    workspace = get_workspace(service) if hasattr(service, "repo") else None
    cases = workspace.list_cases() if workspace else []
    by_id = {row["id"]: row for row in cases}
    pending_case = st.session_state.pop("case_workspace_pending_case", None)
    if pending_case in by_id:
        st.session_state["targeted_search_case"] = pending_case
    st.session_state.setdefault("targeted_search_case", next(iter(by_id), None))
    if (
        st.session_state["targeted_search_case"] not in by_id
        and st.session_state["targeted_search_case"] is not None
    ):
        st.session_state["targeted_search_case"] = next(iter(by_id), None)
    with st.container(key="case_workspace_shell"):
        inject_case_styles()
        picker, create = st.columns([4, 1], vertical_alignment="bottom")
        with picker:
            case_labels = {
                None: tr("Source library workspace"),
                **{key: row["name"] for key, row in by_id.items()},
            }
            case_id = st.selectbox(
                tr("Case workspace"),
                list(by_id) + [None],
                format_func=case_labels.get,
                key="targeted_search_case",
            )
        with create:
            with st.popover(
                tr("Create case workspace"), width="stretch", disabled=workspace is None
            ):
                with st.form("case_create"):
                    name = st.text_input(tr("Case name"))
                    topic = st.text_area(tr("Case topic"))
                    if st.form_submit_button(tr("Create case")):
                        try:
                            created = workspace.create_case(name.strip(), topic.strip())
                            st.session_state["case_workspace_pending_case"] = created[
                                "id"
                            ]
                            st.rerun()
                        except Exception as exc:
                            _error(exc)
        previous_scope = st.session_state.get("case_workspace_navigation_scope")
        if previous_scope != case_id:
            if (
                previous_scope
                and st.session_state.get("case_workspace_view") in WORKFLOW_VIEWS
            ):
                st.session_state[_key(previous_scope, "last_view")] = st.session_state[
                    "case_workspace_view"
                ]
            st.session_state["case_workspace_view"] = st.session_state.get(
                _key(case_id, "last_view"), "Footage Search"
            )
        st.session_state["case_workspace_navigation_scope"] = case_id
        footage.switch_scope(case_id)
        pending_view = st.session_state.pop("case_workspace_pending_view", None)
        if pending_view == "Search Everything":
            pending_view = "Library"
            st.session_state[_key(case_id, "source_tab")] = "Find evidence"
        if pending_view in WORKFLOW_VIEWS:
            st.session_state["case_workspace_view"] = pending_view
        if st.session_state.get("case_workspace_view") not in WORKFLOW_VIEWS:
            st.session_state["case_workspace_view"] = "Footage Search"
        case = workspace.get_case(case_id) if case_id else None
        if case:
            readiness = case_readiness(workspace, case_id)
            summary = tr("Case readiness summary").format(**readiness)
            st.markdown(
                f'<header class="cw-case-header"><h2>{escape(case["name"])}</h2><p>{escape(summary)}</p></header>',
                unsafe_allow_html=True,
            )
        elif not cases:
            render_empty_state(
                tr("Build your first documentary case"),
                tr("First documentary case help"),
            )
        navigation, body = st.columns([1, 4], gap="large")
        with navigation:
            with st.container(key="case_workspace_navigation"):
                view_labels = {
                    value: tr(VIEW_LABELS[value]) for value in WORKFLOW_VIEWS
                }
                view = st.radio(
                    tr("Workspace view"),
                    WORKFLOW_VIEWS,
                    format_func=view_labels.get,
                    key="case_workspace_view",
                    label_visibility="collapsed",
                )
                st.caption(tr("Workspace navigation help"))
        st.session_state[_key(case_id, "last_view")] = view
        with body:
            _render_workspace_body(workspace, service, case, view, tr)


CASE_WORKSPACE_TRANSLATION_KEYS = frozenset(
    [
        "Production scene number",
        "Production scene label",
        "Production source label",
        "Production source unavailable",
        "Production range label",
        "Production duration label",
        "Production edit summary",
        "Production previews and render status",
        "Production step scenes",
        "Production step save",
        "Production step render",
        "Production empty title",
        "Production empty help",
        "Production open script",
        "Production find footage",
        "Production build scenes",
        "Production narration and save",
        "Case source added",
        "Case source linked",
        "Case source acquired",
        "Case source searchable",
        "Case source partial",
        "Case source no speech",
        "Case source reviewed",
        "Case source unreviewed",
        "Case library empty help",
        "Case folder setup help",
        "Make case files searchable",
        "Case indexing help",
        "Facts and timeline",
        "Facts",
        "Timeline",
        "Fact needs review",
        "Fact reviewed",
        "Fact disputed",
        "Fact needs evidence",
        "Fact class unclassified",
        "Fact class allegation",
        "Fact class testimony",
        "Fact class police report",
        "Fact class court finding",
        "Fact class news report",
        "Fact class editorial",
        "Fact class recording observation",
        "Saved source evidence",
        "Evidence supports",
        "Evidence conflicts",
        "Evidence mentions",
        "Evidence source changed",
        "No timeline events yet",
        "Timeline empty help",
        "Event date unknown",
        "Timeline source changed help",
        "View source citations",
        "Add a timeline event",
        "No case facts yet",
        "Case facts empty help",
        "Fact reviewed by label",
        "Fact source changed help",
        "Review this fact",
        "Add or review a case fact",
        "Workspace",
        "Create a video",
        "Documentary workspace",
        "Workspace footage",
        "Workspace sources",
        "Workspace facts",
        "Workspace script",
        "Workspace sound",
        "Workspace edit",
        "Workspace footage help",
        "Workspace sources help",
        "Workspace facts help",
        "Workspace edit help",
        "Manage footage sources",
        "Case source view",
        "Browse sources",
        "Find evidence",
        "Case processing history",
        "Choose a case to continue",
        "Choose a case help",
        "Build your first documentary case",
        "First documentary case help",
        "Case readiness summary",
        "Workspace navigation help",
        "Source file details",
        "Footage search placeholder",
        "Footage search starting help",
        "Search filters",
        "Outline ready",
        "Draft ready",
        "Ready for editorial review",
        "Approved",
        "Needs revision",
        "Revision requested",
        "Cues ready",
        "Missing sound effects",
        "Inputs changed",
        "Complete",
        "Waiting to start",
        "In progress",
        "Retrying",
        "Needs attention",
        "Background document page number",
        "Continuous narration timing help",
        "Prepare case folders",
        "Prepared case folder help",
        "Imported word timestamps asset",
        "Use uploaded timestamps JSON",
        "Supporting evidence citations",
        "Contradicting evidence citations",
        "Refresh retained source original",
        "Retained source refreshed",
        "Linked source original not acquired",
        "Final narration script asset",
        "Load narration script asset",
        "Evidence category filter",
        "All categories",
        "Evidence source filter",
        "All case assets",
        "Save footage range citation",
        "Aligned audio asset",
        "Aligned script asset",
        "Apply scene order",
        "Asset kind",
        "Assets to index",
        "Bind word timestamps",
        "Case claims",
        "Case created",
        "Case import summary",
        "Case name",
        "Case originals help",
        "Case timeline",
        "Case topic",
        "Case workspace",
        "Category",
        "Citation relation",
        "Claim assertion class",
        "Claim review notes",
        "Claim reviewed by",
        "Claim saved",
        "Claim status",
        "Claim text",
        "Claim to edit",
        "Claims review help",
        "Confirm word timing asset binding",
        "Create case",
        "Create case workspace",
        "Document page number",
        "Download original asset",
        "Download storyboard provenance",
        "Download storyboard video",
        "Event date and time",
        "Event date uncertainty help",
        "Event notes",
        "Event time precision",
        "Event title",
        "Evidence asset kinds",
        "Evidence citation saved",
        "Explicit storyboard binding help",
        "Export case workspace",
        "Final narration audio asset",
        "Final narration script",
        "Footage Search",
        "Footage matches",
        "Highlighted evidence excerpt",
        "Import aligned word timestamps",
        "Import assets before storyboard",
        "Import audio before word timestamps",
        "Import case folder",
        "Import originals",
        "Include production drafts",
        "Independent modality ranking help",
        "Index selected assets",
        "Index status",
        "Indexing queued",
        "Library",
        "Library source",
        "Link existing footage source",
        "Link source to case",
        "Load storyboard for editing",
        "Local case folder",
        "Missing source requests",
        "Narration timestamps require script",
        "New claim",
        "New scene",
        "New source request",
        "New storyboard",
        "No case evidence found",
        "No narration track",
        "No script selected",
        "No visual overlay",
        "Original filename",
        "Preview evidence in context",
        "Preview original asset",
        "Preview unavailable for this asset",
        "Production",
        "Provenance review",
        "Render saved storyboard",
        "Request notes",
        "Request status",
        "Requested source",
        "Review case asset",
        "Rights status",
        "Save claim",
        "Save evidence citation",
        "Save source request",
        "Save storyboard",
        "Save storyboard scene",
        "Save timeline event",
        "Saved evidence citations",
        "Saved storyboard",
        "Scene ID",
        "Scene IDs must be unique",
        "Scene audio volume",
        "Scene claim references",
        "Scene duration seconds",
        "Scene narration",
        "Scene playback speed",
        "Scene role",
        "Scene source asset",
        "Scene source end seconds",
        "Scene source start seconds",
        "Scene to edit",
        "Search Everything",
        "Search all case evidence",
        "Search case workspace",
        "Search query required",
        "Search result",
        "Select a case workspace",
        "Source library",
        "Source linked to case",
        "Source request",
        "Source request saved",
        "Storyboard render queued",
        "Storyboard saved",
        "Storyboard scene order",
        "Storyboard scene saved",
        "Storyboard title",
        "Supporting evidence result",
        "Timeline / Claims",
        "Timeline event saved",
        "Visual asset for original sound",
        "WhisperX word timestamps JSON",
        "Word timestamps bound",
        "Word timestamps must be a JSON object",
        "Word timestamps JSON required",
        "Word timing binding help",
        "Word timing scope",
        "Workspace view",
        "Source library workspace",
        "case_kind.audio",
        "case_kind.document",
        "case_kind.image",
        "case_kind.map",
        "case_kind.other",
        "case_kind.reference",
        "case_kind.script",
        "case_kind.transcript",
        "case_kind.video",
    ]
)

CASE_WORKSPACE_TRANSLATION_KEYS |= (
    DOCUMENTARY_TRANSLATION_KEYS | ACOUSTIC_TRANSLATION_KEYS
)
