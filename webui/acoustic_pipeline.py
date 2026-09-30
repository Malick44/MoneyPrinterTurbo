"""Review word-anchored sound cues before rendering a cinematic narration mix."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import streamlit as st

from app.models.acoustic import CueEdit, SOUND_CATEGORIES
from webui.case_design import (
    friendly_status,
    render_empty_state,
    render_section_header,
    render_steps,
)


def get_pipeline(workspace):
    from app.services.targeted_search.acoustic_pipeline import AcousticPipeline

    return AcousticPipeline(workspace)


def _key(case_id, name):
    return f"case_{case_id}_acoustic_{name}"


def _plan_key(case_id, plan, name):
    return _key(case_id, f"{plan['id']}_{plan['revision']}_{name}")


def _label(asset):
    """Show the name an editor recognizes, keeping identifiers in widget values."""
    return asset.get("filename") or Path(asset.get("relative_path", "")).name


def _time_label(milliseconds):
    seconds, millis = divmod(max(0, int(milliseconds or 0)), 1000)
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes:02d}:{seconds:02d}.{millis:03d}"


def _alignment_label(artifact, number, tr):
    date = str(artifact.get("created_at") or "")[:16].replace("T", " ")
    return tr("Acoustic word timing") + (f" · {date}" if date else f" {number}")


def _start_worker(workspace):
    from app.services.targeted_search.worker import ensure_worker_running

    ensure_worker_running(root_dir=workspace.repo.root)


def _render_inputs(pipeline, workspace, case, assets, tr, show_steps=False):
    case_id = case["id"]
    narration = {
        row["id"]: row
        for row in assets
        if row.get("asset_kind") == "audio"
        and Path(row.get("filename", "")).suffix.lower() == ".wav"
        and (row.get("metadata") or {}).get("role") not in {"sound_effect", "sfx"}
        and row.get("artifact_id")
    }
    scripts = {row["id"]: row for row in assets if row.get("asset_kind") == "script"}
    step_slot = st.empty() if show_steps else None
    if not narration or not scripts:
        if step_slot is not None:
            with step_slot.container():
                _render_steps(tr, 0)
        render_empty_state(
            tr("Acoustic add narration"), tr("Acoustic narration and script required")
        )
        return None
    st.caption(tr("Acoustic narration alignment help"))
    narration_labels = {
        identifier: _label(row) for identifier, row in narration.items()
    }
    script_labels = {identifier: _label(row) for identifier, row in scripts.items()}
    audio_col, script_col = st.columns(2)
    with audio_col:
        narration_id = st.selectbox(
            tr("Acoustic narration WAV"),
            list(narration),
            format_func=narration_labels.get,
            key=_key(case_id, "narration"),
        )
    with script_col:
        script_id = st.selectbox(
            tr("Acoustic final script"),
            list(scripts),
            format_func=script_labels.get,
            key=_key(case_id, "script"),
        )
    alignments = {
        row["id"]: row
        for row in workspace.search_service.list_artifacts()
        if row.get("kind") == "narration_alignment"
        and (row.get("metadata") or {}).get("case_id") == case_id
        and (row.get("metadata") or {}).get("asset_id") == narration_id
    }
    labels = {
        identifier: _alignment_label(row, number, tr)
        for number, (identifier, row) in enumerate(alignments.items(), 1)
    }
    labels[None] = tr("Acoustic latest matching alignment")
    with st.expander(tr("Acoustic choose word timing")):
        transcript_id = st.selectbox(
            tr("Acoustic narration word alignment"),
            [None] + list(alignments),
            format_func=labels.get,
            key=_key(case_id, "alignment_" + narration_id),
        )
    base_options = {
        "narration_asset_id": narration_id,
        "script_asset_id": script_id,
        "transcript_artifact_id": transcript_id,
    }
    readiness, blocked = {}, False
    try:
        readiness = pipeline.readiness(case_id, base_options)
        if readiness.get("alignment_ready"):
            st.success(
                tr("Acoustic alignment ready") + f": {readiness.get('word_count', 0)}"
            )
            if readiness.get("unaligned_words"):
                st.warning(
                    tr("Acoustic unaligned words") + f": {readiness['unaligned_words']}"
                )
        else:
            st.info(tr("Acoustic alignment required"))
    except Exception as exc:
        blocked = True
        st.error(str(exc))
    if step_slot is not None:
        with step_slot.container():
            _render_steps(tr, 2 if readiness.get("alignment_ready") else 1)
    auto_align = False
    if not readiness.get("alignment_ready") and not blocked:
        auto_align = st.checkbox(
            tr("Acoustic align narration if needed"),
            value=False,
            key=_key(case_id, "auto_align"),
        )
    with st.form(_key(case_id, "analyze")):
        with st.expander(tr("Acoustic suggestion settings")):
            title = st.text_input(tr("Acoustic plan title"), value=case["name"])
            style = st.text_area(
                tr("Acoustic sound design style"),
                value=tr("Acoustic restrained style"),
            )
            max_cues = st.number_input(
                tr("Acoustic maximum sound cues"), min_value=1, max_value=60, value=20
            )
        options = {
            "title": title.strip(),
            "narration_asset_id": narration_id,
            "script_asset_id": script_id,
            "transcript_artifact_id": transcript_id,
            "style": style,
            "max_cues": max_cues,
            "auto_align": auto_align,
        }
        generate = st.form_submit_button(
            tr("Analyze narration tension")
            if readiness.get("alignment_ready")
            else tr("Acoustic align and suggest cues"),
            type="primary",
            disabled=blocked
            or (not readiness.get("alignment_ready") and not auto_align),
        )
        if generate:
            try:
                readiness = pipeline.readiness(case_id, options)
                if not readiness.get("alignment_ready") and not auto_align:
                    raise ValueError(tr("Acoustic alignment required"))
                pipeline.enqueue(case_id, options)
                _start_worker(workspace)
                st.success(tr("Acoustic analysis queued"))
            except Exception as exc:
                st.error(str(exc))
    return narration_id


def _render_registration(workspace, case_id, assets, narration_id, tr):
    from app.services.targeted_search.sound_assets import register_sound

    cited_assets = {
        citation.get("asset_id")
        for claim in workspace.list_claims(case_id)
        for citation in claim.get("citations", [])
    }
    candidates = {
        row["id"]: row
        for row in assets
        if row.get("asset_kind") == "audio"
        and Path(row.get("filename", "")).suffix.lower() == ".wav"
        and row.get("artifact_id")
        and row["id"] != narration_id
        and row["id"] not in cited_assets
    }
    with st.expander(tr("Acoustic sound effect library")):
        st.caption(tr("Acoustic sound registration help"))
        if not candidates:
            st.info(tr("Acoustic import sound effects first"))
            return
        asset_labels = {
            identifier: _label(row) for identifier, row in candidates.items()
        }
        asset_id = st.selectbox(
            tr("Acoustic sound effect WAV"),
            list(candidates),
            format_func=asset_labels.get,
            key=_key(case_id, "register_asset"),
        )
        with st.form(_key(case_id, "register_" + asset_id)):
            sound = (candidates[asset_id].get("metadata") or {}).get("sound_asset", {})
            category_labels = {
                value: tr("sound_category." + value) for value in SOUND_CATEGORIES
            }
            category = st.selectbox(
                tr("Acoustic effect category"),
                SOUND_CATEGORIES,
                index=SOUND_CATEGORIES.index(sound.get("category", "impact")),
                format_func=category_labels.get,
            )
            tags = st.text_input(
                tr("Acoustic sound tags"), value=", ".join(sound.get("tags", []))
            )
            description = st.text_area(
                tr("Acoustic sound description"), value=sound.get("description", "")
            )
            confirmed = st.checkbox(
                tr("Acoustic confirm editorial effect"), value=False
            )
            if st.form_submit_button(tr("Register acoustic sound effect")):
                try:
                    if not confirmed:
                        raise ValueError(tr("Acoustic editorial confirmation required"))
                    register_sound(
                        workspace,
                        asset_id,
                        tags=[
                            value.strip() for value in tags.split(",") if value.strip()
                        ],
                        description=description,
                        category=category,
                    )
                    st.success(tr("Acoustic sound effect registered"))
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))


def editable_plan(plan):
    """Project only the editable cue contract, never server anchor/hash fields."""
    return {
        "cues": [
            {key: value for key, value in cue.items() if key in CueEdit.model_fields}
            for cue in plan.get("cues", [])
        ],
        "mix": {
            key: value
            for key, value in (plan.get("mix") or {}).items()
            if key in {"narration_gain_db", "duck_db", "headroom_db"}
        },
    }


def _render_editor(pipeline, workspace, case_id, plan, tr):
    from app.services.targeted_search.sound_assets import list_sounds

    sounds = {row["id"]: row for row in list_sounds(workspace, case_id)}
    st.markdown("### " + tr("Acoustic cue sheet"))
    st.caption(tr("Acoustic estimated tension help"))
    cues = {row["cue_id"]: row for row in plan.get("cues", [])}
    if not cues:
        st.info(tr("Acoustic no sound cues"))
        return
    st.dataframe(
        [
            {
                tr("Acoustic time"): _time_label(cue.get("start_ms")),
                tr("Acoustic spoken word"): cue.get("anchor_word", ""),
                tr("Acoustic selected sound"): (
                    _label(sounds[cue["asset_id"]])
                    if cue.get("asset_id") in sounds
                    else tr("Acoustic no sound effect")
                ),
                tr("Acoustic effect category"): tr("sound_category." + cue["category"]),
                tr("Acoustic cue state"): (
                    tr("Acoustic included")
                    if cue.get("enabled")
                    else tr("Acoustic skipped")
                ),
            }
            for cue in cues.values()
        ],
        hide_index=True,
        width="stretch",
    )
    unmatched = sum(not cue.get("asset_id") for cue in cues.values())
    if unmatched:
        st.warning(tr("Acoustic unmatched summary") + f" ({unmatched})")
    with st.expander(tr("Acoustic adjust cues and levels")):
        _render_cue_controls(pipeline, workspace, case_id, plan, tr, sounds, cues)
    if plan.get("notes"):
        with st.expander(tr("Acoustic design notes")):
            for note in plan["notes"]:
                st.write(note)


def _render_cue_controls(pipeline, workspace, case_id, plan, tr, sounds, cues):
    from app.services.targeted_search.case_media import asset_content
    from app.services.targeted_search.sound_assets import validate_sound

    cue_labels = {
        identifier: (
            _time_label(cue.get("start_ms"))
            + " · "
            + cue.get("anchor_word", "")
            + " · "
            + tr("sound_category." + cue["category"])
        )
        for identifier, cue in cues.items()
    }
    cue_id = st.selectbox(
        tr("Acoustic cue to edit"),
        list(cues),
        format_func=cue_labels.get,
        key=_plan_key(case_id, plan, "cue"),
    )
    cue = cues[cue_id]
    st.write(cue["reason"])
    asset_options = [None] + list(sounds)
    sound_labels = {identifier: _label(row) for identifier, row in sounds.items()}
    sound_labels[None] = tr("Acoustic no sound effect")
    asset_id = st.selectbox(
        tr("Acoustic matched sound effect"),
        asset_options,
        index=asset_options.index(cue.get("asset_id"))
        if cue.get("asset_id") in asset_options
        else 0,
        format_func=sound_labels.get,
        key=_plan_key(case_id, plan, cue_id + "_asset"),
    )
    if asset_id and st.button(
        tr("Preview acoustic sound effect"),
        key=_plan_key(case_id, plan, cue_id + "_preview"),
    ):
        try:
            validate_sound(workspace, asset_id)
            path = asset_content(workspace, asset_id, requested_use="internal_review")
            st.audio(str(path))
        except Exception as exc:
            st.error(str(exc))
    with st.form(_plan_key(case_id, plan, "edit_" + cue_id)):
        enabled = st.checkbox(
            tr("Acoustic enable reviewed cue"),
            value=bool(cue.get("enabled") and asset_id),
            disabled=not asset_id,
            key=_plan_key(case_id, plan, cue_id + "_enabled_" + str(asset_id)),
        )
        anchor_labels = {
            value: tr("sound_anchor." + value) for value in ("start", "end")
        }
        anchor = st.selectbox(
            tr("Acoustic anchor word boundary"),
            ("start", "end"),
            index=0 if cue.get("anchor", "start") == "start" else 1,
            format_func=anchor_labels.get,
        )
        offset = st.number_input(
            tr("Acoustic cue offset seconds"),
            min_value=-5.0,
            max_value=5.0,
            value=cue.get("offset_ms", 0) / 1000,
            step=0.001,
        )
        duration = st.number_input(
            tr("Acoustic effect duration seconds"),
            min_value=0.02,
            max_value=15.0,
            value=cue.get("duration_ms", 2000) / 1000,
            step=0.01,
        )
        gain = st.number_input(
            tr("Acoustic effect gain dB"),
            min_value=-40.0,
            max_value=-4.0,
            value=float(cue.get("gain_db", -18)),
            step=0.5,
        )
        fade_in = st.number_input(
            tr("Acoustic fade in milliseconds"),
            min_value=0,
            max_value=5000,
            value=int(cue.get("fade_in_ms", 20)),
        )
        fade_out = st.number_input(
            tr("Acoustic fade out milliseconds"),
            min_value=0,
            max_value=5000,
            value=int(cue.get("fade_out_ms", 100)),
        )
        mix = plan.get("mix") or {}
        narration_gain = st.number_input(
            tr("Acoustic narration gain dB"),
            min_value=-12.0,
            max_value=6.0,
            value=float(mix.get("narration_gain_db", 0)),
            step=0.5,
        )
        duck = st.number_input(
            tr("Acoustic effect ducking dB"),
            min_value=-24.0,
            max_value=0.0,
            value=float(mix.get("duck_db", -8)),
            step=0.5,
        )
        headroom = st.number_input(
            tr("Acoustic mix headroom dB"),
            min_value=-6.0,
            max_value=-0.1,
            value=float(mix.get("headroom_db", -1)),
            step=0.1,
        )
        if st.form_submit_button(tr("Save acoustic cue revision")):
            try:
                record = deepcopy(editable_plan(plan))
                target = next(row for row in record["cues"] if row["cue_id"] == cue_id)
                target.update(
                    asset_id=asset_id,
                    enabled=enabled and bool(asset_id),
                    anchor=anchor,
                    offset_ms=round(offset * 1000),
                    duration_ms=round(duration * 1000),
                    gain_db=gain,
                    fade_in_ms=fade_in,
                    fade_out_ms=fade_out,
                )
                record["mix"] = {
                    "narration_gain_db": narration_gain,
                    "duck_db": duck,
                    "headroom_db": headroom,
                }
                pipeline.save_plan(
                    case_id, plan["id"], record, expected_revision=plan["revision"]
                )
                st.success(tr("Acoustic plan revision saved"))
                st.rerun()
            except Exception as exc:
                st.error(str(exc))


def _render_outputs(pipeline, workspace, case_id, plan, tr):
    """Authorize every delivered file before showing the completed mix."""
    delivered = {}
    kinds = {"acoustic_mix", "acoustic_timeline", "acoustic_otio"}
    artifacts = sorted(
        workspace.search_service.list_artifacts(),
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    )
    for artifact in artifacts:
        metadata = artifact.get("metadata") or {}
        kind = artifact.get("kind")
        if (
            kind not in kinds
            or kind in delivered
            or metadata.get("plan_id") != plan["id"]
            or metadata.get("case_id") != case_id
            or metadata.get("plan_revision") != plan["revision"]
        ):
            continue
        try:
            path = pipeline.mix_content(case_id, plan["id"], artifact["id"])
            delivered[kind] = (artifact, path)
        except Exception as exc:
            st.error(str(exc))
    if "acoustic_mix" in delivered:
        st.markdown("### " + tr("Acoustic your mix is ready"))
        st.caption(
            plan["title"]
            + " · "
            + _time_label(plan.get("duration_ms"))
            + " · "
            + tr("Acoustic plan revision")
            + f" {plan['revision']}"
        )
        artifact, path = delivered["acoustic_mix"]
        st.audio(str(path))
        st.download_button(
            tr("Acoustic download WAV"),
            path.read_bytes(),
            file_name="Narration_Mix.wav",
            mime="audio/wav",
            key=_plan_key(case_id, plan, "download_" + artifact["id"]),
        )
    timeline_files = {
        "acoustic_timeline": (
            "Acoustic download cue timeline",
            "Sound_Cues.json",
            "application/json",
        ),
        "acoustic_otio": (
            "Acoustic download editing timeline",
            "Editing_Timeline.otio",
            "application/json",
        ),
    }
    if any(kind in delivered for kind in timeline_files):
        with st.expander(tr("Acoustic editing files")):
            st.caption(tr("Acoustic editing files help"))
            for kind, (label, filename, mime) in timeline_files.items():
                if kind in delivered:
                    artifact, path = delivered[kind]
                    st.download_button(
                        tr(label),
                        path.read_bytes(),
                        file_name=filename,
                        mime=mime,
                        key=_plan_key(case_id, plan, "download_" + artifact["id"]),
                    )
    return "acoustic_mix" in delivered


def _render_mix(pipeline, workspace, case_id, plan, tr):
    st.caption(tr("Acoustic mix review help"))
    confirmed = st.checkbox(
        tr("Acoustic confirm reviewed sound plan"),
        key=_plan_key(case_id, plan, "reviewed"),
    )
    if st.button(
        tr("Render acoustic narration mix"),
        disabled=not confirmed or plan.get("can_mix") is False,
        type="primary",
        key=_plan_key(case_id, plan, "mix"),
    ):
        try:
            pipeline.enqueue_mix(
                case_id, plan["id"], expected_revision=plan["revision"]
            )
            _start_worker(workspace)
            st.success(tr("Acoustic mix queued"))
        except Exception as exc:
            st.error(str(exc))


@st.fragment(run_every="2s")
def _render_jobs(workspace, case_id, tr):
    jobs = [
        row
        for row in workspace.search_service.list_jobs()
        if (row.get("payload") or {}).get("case_id") == case_id
        and "acoustic" in row.get("job_type", "")
    ]
    status_key = _key(case_id, "job_statuses")
    previous = st.session_state.get(status_key)
    statuses = {row["id"]: row.get("status") for row in jobs}
    st.session_state[status_key] = statuses
    # Refresh the whole result once a background job settles. The periodic
    # fragment keeps progress light while analysis or FFmpeg is still running.
    if previous is not None and any(
        status in {"complete", "completed", "failed"}
        and previous.get(identifier) != status
        for identifier, status in statuses.items()
    ):
        st.rerun(scope="app")
    active = [
        row for row in jobs if row.get("status") in {"queued", "retry", "running"}
    ]
    for job in active:
        action = (
            tr("Acoustic rendering mix")
            if "mix" in job["job_type"]
            else tr("Acoustic suggesting cues")
        )
        st.info(action + " · " + tr("Acoustic job " + job["status"]))
    if jobs:
        with st.expander(tr("Acoustic processing history")):
            for job in jobs[:5]:
                action = (
                    tr("Acoustic rendering mix")
                    if "mix" in job["job_type"]
                    else tr("Acoustic suggesting cues")
                )
                st.caption(
                    action + " · " + tr("Acoustic job " + job.get("status", "queued"))
                )
                if job.get("last_error"):
                    st.error(job["last_error"])


def _render_steps(tr, active_index):
    render_steps(
        [
            tr("Acoustic step narration"),
            tr("Acoustic step word timing"),
            tr("Acoustic step sound cues"),
            tr("Acoustic step mix"),
        ],
        active_index,
    )


def _default_plan(pipeline, workspace, case_id, plans):
    """Prefer the latest playable result while retaining explicit plan selection."""
    for artifact in sorted(
        workspace.search_service.list_artifacts(),
        key=lambda row: str(row.get("created_at") or ""),
        reverse=True,
    ):
        metadata = artifact.get("metadata") or {}
        identifier = metadata.get("plan_id")
        summary = plans.get(identifier)
        if (
            artifact.get("kind") != "acoustic_mix"
            or metadata.get("case_id") != case_id
            or not summary
            or summary.get("content_withheld")
            or metadata.get("plan_revision") != summary["revision"]
        ):
            continue
        try:
            pipeline.mix_content(case_id, identifier, artifact["id"])
            return identifier
        except Exception:
            continue
    return next(iter(plans))


def render_acoustic_pipeline(workspace, case, tr):
    pipeline = get_pipeline(workspace)
    case_id = case["id"]
    render_section_header(tr("Cinematic Sound"), tr("Acoustic workspace help"))
    assets = workspace.list_assets(case_id)
    plans = {row["id"]: row for row in pipeline.list_plans(case_id)}
    if not plans:
        narration_id = _render_inputs(
            pipeline, workspace, case, assets, tr, show_steps=True
        )
        _render_registration(workspace, case_id, assets, narration_id, tr)
        _render_jobs(workspace, case_id, tr)
        st.info(tr("Acoustic plans will appear here"))
        return
    if len(plans) > 1:
        identifiers = list(plans)
        default = _default_plan(pipeline, workspace, case_id, plans)
        plan_labels = {
            identifier: (
                f"{index + 1}. {summary['title']} · "
                + tr(friendly_status(summary.get("status")))
            )
            for index, (identifier, summary) in enumerate(plans.items())
        }
        selected = st.selectbox(
            tr("Acoustic saved sound plan"),
            identifiers,
            index=identifiers.index(default),
            format_func=plan_labels.get,
            key=_key(case_id, "plan"),
        )
    else:
        selected = next(iter(plans))
    plan = None
    try:
        plan = pipeline.get_plan(case_id, selected)
        plan["can_mix"] = plans[selected].get("can_mix", True)
        if plan.get("stale") or plan.get("content_withheld"):
            raise ValueError(
                plan.get("guard_error") or tr("Acoustic source inputs changed")
            )
        completed = _render_outputs(pipeline, workspace, case_id, plan, tr)
        _render_steps(tr, 3 if completed else 2)
        _render_editor(pipeline, workspace, case_id, plan, tr)
        if completed:
            with st.expander(tr("Acoustic render another mix")):
                _render_mix(pipeline, workspace, case_id, plan, tr)
        else:
            st.markdown("### " + tr("Acoustic ready to mix"))
            _render_mix(pipeline, workspace, case_id, plan, tr)
    except Exception as exc:
        st.error(str(exc))
        st.info(tr("Acoustic rebuild guidance"))
    _render_jobs(workspace, case_id, tr)
    with st.expander(tr("Acoustic start a new sound plan")):
        narration_id = _render_inputs(pipeline, workspace, case, assets, tr)
    _render_registration(
        workspace,
        case_id,
        assets,
        narration_id or (plan or {}).get("narration_asset_id"),
        tr,
    )


ACOUSTIC_TRANSLATION_KEYS = frozenset(
    {
        "Acoustic add narration",
        "Acoustic adjust cues and levels",
        "Acoustic align and suggest cues",
        "Acoustic choose word timing",
        "Acoustic cue sheet",
        "Acoustic cue state",
        "Acoustic design notes",
        "Acoustic download WAV",
        "Acoustic download cue timeline",
        "Acoustic download editing timeline",
        "Acoustic editing files",
        "Acoustic editing files help",
        "Acoustic included",
        "Acoustic job complete",
        "Acoustic job completed",
        "Acoustic job failed",
        "Acoustic job queued",
        "Acoustic job retry",
        "Acoustic job running",
        "Acoustic processing history",
        "Acoustic ready to mix",
        "Acoustic rebuild guidance",
        "Acoustic render another mix",
        "Acoustic rendering mix",
        "Acoustic selected sound",
        "Acoustic skipped",
        "Acoustic spoken word",
        "Acoustic start a new sound plan",
        "Acoustic step mix",
        "Acoustic step narration",
        "Acoustic step sound cues",
        "Acoustic step word timing",
        "Acoustic suggesting cues",
        "Acoustic suggestion settings",
        "Acoustic time",
        "Acoustic unmatched summary",
        "Acoustic word timing",
        "Acoustic workspace help",
        "Acoustic your mix is ready",
        "Cues ready",
        "Missing sound effects",
        "Inputs changed",
        "Cinematic Sound",
        "Acoustic narration alignment help",
        "Acoustic narration and script required",
        "Acoustic narration WAV",
        "Acoustic final script",
        "Acoustic narration word alignment",
        "Acoustic latest matching alignment",
        "Acoustic plan title",
        "Acoustic sound design style",
        "Acoustic restrained style",
        "Acoustic maximum sound cues",
        "Acoustic align narration if needed",
        "Check acoustic alignment",
        "Analyze narration tension",
        "Acoustic alignment ready",
        "Acoustic unaligned words",
        "Acoustic alignment required",
        "Acoustic analysis queued",
        "Acoustic sound effect library",
        "Acoustic sound registration help",
        "Acoustic import sound effects first",
        "Acoustic sound effect WAV",
        "Acoustic effect category",
        "Acoustic sound tags",
        "Acoustic sound description",
        "Acoustic confirm editorial effect",
        "Register acoustic sound effect",
        "Acoustic editorial confirmation required",
        "Acoustic sound effect registered",
        "Acoustic unmatched cue disabled",
        "Acoustic no sound cues",
        "Acoustic cue to edit",
        "Acoustic matched sound effect",
        "Acoustic no sound effect",
        "Preview acoustic sound effect",
        "Acoustic enable reviewed cue",
        "Acoustic anchor word boundary",
        "Acoustic cue offset seconds",
        "Acoustic effect duration seconds",
        "Acoustic effect gain dB",
        "Acoustic fade in milliseconds",
        "Acoustic fade out milliseconds",
        "Acoustic narration gain dB",
        "Acoustic effect ducking dB",
        "Acoustic mix headroom dB",
        "Save acoustic cue revision",
        "Acoustic plan revision saved",
        "Acoustic mix review help",
        "Acoustic confirm reviewed sound plan",
        "Render acoustic narration mix",
        "Acoustic mix queued",
        "Download acoustic mix file",
        "Acoustic processing job",
        "Acoustic plans will appear here",
        "Acoustic saved sound plan",
        "Acoustic plan revision",
        "Acoustic source inputs changed",
        "Acoustic estimated tension help",
        "sound_anchor.start",
        "sound_anchor.end",
    }
) | frozenset("sound_category." + value for value in SOUND_CATEGORIES)
