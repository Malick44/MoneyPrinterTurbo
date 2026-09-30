"""Navigation regressions for the full-page documentary workspace."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, Mock, patch

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from webui import case_workspace as ui
from webui import targeted_search as footage


class NavigationWorkspace:
    def __init__(self, cases=None):
        self.cases = (
            cases
            if cases is not None
            else [
                {
                    "id": "case-a",
                    "name": "Case A",
                    "collection_id": "collection-a",
                    "source_ids": ["video-a"],
                },
                {
                    "id": "case-b",
                    "name": "Case B",
                    "collection_id": "collection-b",
                    "source_ids": ["video-b"],
                },
            ]
        )
        self.search_service = SimpleNamespace(
            repo=SimpleNamespace(root=Path("/search-root")),
            settings={"enabled": True},
            list_collections=Mock(return_value=[]),
            list_sources=Mock(return_value=[]),
            list_jobs=Mock(return_value=[]),
            list_artifacts=Mock(return_value=[]),
            search=Mock(return_value={"results": []}),
        )
        self.list_cases = Mock(side_effect=lambda: self.cases)
        self.get_case = Mock(
            side_effect=lambda case_id: next(
                row for row in self.cases if row["id"] == case_id
            )
        )
        self.list_assets = Mock(return_value=[])
        self.list_claims = Mock(return_value=[])
        self.list_events = Mock(return_value=[])
        self.save_claim = Mock()
        self.save_event = Mock()


SHELL_SCRIPT = """
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui.render_workspace(workspace.search_service, lambda key: key)
"""


def test_application_navigation_honors_pending_route_before_rendering_widget():
    app = AppTest.from_string("""
import streamlit as st
from webui.case_design import render_application_navigation
st.text(render_application_navigation(lambda key: key))
""")
    app.session_state["application_workspace"] = "video"
    app.session_state["application_pending_workspace"] = "documentary"
    app.run()
    assert not list(app.exception)
    assert app.radio(key="application_workspace").value == "documentary"
    assert app.text[0].value == "documentary"
    assert "application_pending_workspace" not in app.session_state

    app.session_state["application_pending_workspace"] = "video"
    app.run()
    assert not list(app.exception)
    assert app.radio(key="application_workspace").value == "video"


def test_documentary_deep_link_sets_first_visit_without_overriding_user_choice():
    app = AppTest.from_string("""
from webui.case_design import render_application_navigation
render_application_navigation(lambda key: key)
""")
    app.query_params["workspace"] = "documentary"
    app.run()
    assert not list(app.exception)
    assert app.radio(key="application_workspace").value == "documentary"
    app.radio(key="application_workspace").set_value("video").run()
    assert not list(app.exception)
    assert app.radio(key="application_workspace").value == "video"


def test_first_visit_opens_real_case_and_footage_without_hiding_source_library():
    workspace = NavigationWorkspace()
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch.object(ui, "_render_workspace_body") as render,
    ):
        app = AppTest.from_string(SHELL_SCRIPT).run()
        assert not list(app.exception)
        assert app.selectbox(key="targeted_search_case").value == "case-a"
        assert (
            "Source library workspace"
            in app.selectbox(key="targeted_search_case").options
        )
        assert app.radio(key="case_workspace_view").value == "Footage Search"
        assert len(app.radio(key="case_workspace_view").options) == 6
        render.assert_called_with(
            workspace,
            workspace.search_service,
            workspace.cases[0],
            "Footage Search",
            ANY,
        )


def test_saved_source_library_selection_is_not_overridden_by_first_case():
    workspace = NavigationWorkspace()
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch.object(ui, "_render_workspace_body") as render,
    ):
        app = AppTest.from_string(SHELL_SCRIPT)
        app.session_state["targeted_search_case"] = None
        app.run()
        assert not list(app.exception)
        assert app.selectbox(key="targeted_search_case").value is None
        assert render.call_args.args[2] is None


def test_case_navigation_and_selected_footage_are_restored_independently():
    workspace = NavigationWorkspace()
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch.object(ui, "_render_workspace_body"),
    ):
        app = AppTest.from_string(SHELL_SCRIPT).run()
        app.radio(key="case_workspace_view").set_value("Library").run()
        app.session_state[footage.SELECTED_ARTIFACTS_KEY] = ["clip-a"]

        app.selectbox(key="targeted_search_case").select("case-b").run()
        assert not list(app.exception)
        assert app.radio(key="case_workspace_view").value == "Footage Search"
        assert app.session_state[footage.SELECTED_ARTIFACTS_KEY] == []
        app.radio(key="case_workspace_view").set_value("Cinematic Sound").run()
        app.session_state[footage.SELECTED_ARTIFACTS_KEY] = ["clip-b"]

        app.selectbox(key="targeted_search_case").select("case-a").run()
        assert not list(app.exception)
        assert app.radio(key="case_workspace_view").value == "Library"
        assert app.session_state[footage.SELECTED_ARTIFACTS_KEY] == ["clip-a"]

        app.selectbox(key="targeted_search_case").select("case-b").run()
        assert not list(app.exception)
        assert app.radio(key="case_workspace_view").value == "Cinematic Sound"
        assert app.session_state[footage.SELECTED_ARTIFACTS_KEY] == ["clip-b"]


@pytest.mark.parametrize(
    "destination", ["Documentary Writer", "Cinematic Sound", "Production"]
)
def test_pending_handoff_selects_target_case_and_stage(destination):
    workspace = NavigationWorkspace()
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch.object(ui, "_render_workspace_body") as render,
    ):
        app = AppTest.from_string(SHELL_SCRIPT).run()
        app.session_state["case_workspace_pending_case"] = "case-b"
        app.session_state["case_workspace_pending_view"] = destination
        app.run()
        assert not list(app.exception)
        assert app.selectbox(key="targeted_search_case").value == "case-b"
        assert app.radio(key="case_workspace_view").value == destination
        assert render.call_args.args[2]["id"] == "case-b"
        assert render.call_args.args[3] == destination
        assert "case_workspace_pending_case" not in app.session_state
        assert "case_workspace_pending_view" not in app.session_state


def test_legacy_evidence_search_handoff_opens_find_evidence_in_sources():
    workspace = NavigationWorkspace()
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch.object(ui, "_render_search_all") as search,
        patch.object(ui, "_render_library") as browse,
        patch.object(ui, "_render_case_jobs"),
    ):
        app = AppTest.from_string(SHELL_SCRIPT)
        app.session_state["case_workspace_pending_case"] = "case-b"
        app.session_state["case_workspace_pending_view"] = "Search Everything"
        app.run()
        assert not list(app.exception)
        assert app.radio(key="case_workspace_view").value == "Library"
        assert app.radio(key=ui._key("case-b", "source_tab")).value == "Find evidence"
        assert search.call_args.args[1]["id"] == "case-b"
        browse.assert_not_called()


def test_empty_workspace_offers_case_creation_and_working_footage_search():
    workspace = NavigationWorkspace(cases=[])
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(SHELL_SCRIPT).run()
        assert not list(app.exception)
        assert any(
            "Build your first documentary case" in row.value for row in app.markdown
        )
        assert any(row.label == "Case name" for row in app.text_input)
        assert any(row.label == "Create case" for row in app.button)
        assert any(row.label == "Search video evidence" for row in app.text_input)

        app.radio(key="case_workspace_view").set_value("Documentary Writer").run()
        assert not list(app.exception)
        assert any("Choose a case to continue" in row.value for row in app.markdown)


def test_readiness_distinguishes_video_leads_evidence_and_production():
    workspace = NavigationWorkspace()
    workspace.list_assets.return_value = [
        {"asset_kind": "video", "artifact_id": None, "metadata": {"linked": True}},
        {"asset_kind": "video", "artifact_id": None, "metadata": {"linked": True}},
        {"asset_kind": "video", "artifact_id": "original-video", "metadata": {}},
        {"asset_kind": "document", "artifact_id": "original-pdf", "metadata": {}},
        {"asset_kind": "script", "artifact_id": "draft-script", "metadata": {}},
        {
            "asset_kind": "audio",
            "artifact_id": "narration",
            "metadata": {"role": "narration"},
        },
        {
            "asset_kind": "audio",
            "artifact_id": "sfx",
            "metadata": {"role": "sound_effect"},
        },
    ]
    workspace.list_claims.return_value = [
        {
            "status": "reviewed",
            "reviewed_by": "Editor",
            "citations": [{"unit_id": "current"}],
        },
        {
            "status": "reviewed",
            "reviewed_by": "Editor",
            "citations": [{"unit_id": "stale"}],
            "has_stale_citations": True,
        },
        {
            "status": "reviewed",
            "reviewed_by": "",
            "citations": [{"unit_id": "unnamed"}],
        },
        {"status": "reviewed", "reviewed_by": "Editor", "citations": []},
        {
            "status": "proposed",
            "reviewed_by": "Editor",
            "citations": [{"unit_id": "proposed"}],
        },
    ]
    assert ui.case_readiness(workspace, "case-a") == {
        "retained": 2,
        "footage_leads": 2,
        "reviewed_claims": 1,
    }


def _actual_application_script(material_source):
    """Run the real shell function without importing configuration-heavy Main."""
    path = Path(__file__).resolve().parents[2] / "webui" / "Main.py"
    source = path.read_text(encoding="utf-8")
    node = next(
        item
        for item in ast.parse(source).body
        if isinstance(item, ast.FunctionDef) and item.name == "_render_application"
    )
    function = ast.get_source_segment(source, node)
    return f"""
import streamlit as st
from types import SimpleNamespace
from webui import targeted_search as targeted_search_ui
tr = lambda key: key
VideoParams = lambda **kwargs: SimpleNamespace(video_source={material_source!r}, **kwargs)
_render_top_bar = lambda: None
_apply_pending_settings_preset = lambda: False
_apply_pending_task_restore = lambda: False
_render_script_settings = lambda *args: st.text('video-script-form')
_render_video_settings = lambda *args: st.text('video-material-form')
_render_audio_settings = lambda *args: (None, None, 'tts')
_render_subtitle_settings = lambda *args: None
_render_generation_controls = lambda *args: False
def _save_runtime_config():
    st.session_state["config_save_count"] = st.session_state.get("config_save_count", 0) + 1
{function}
_render_application()
"""


@pytest.mark.parametrize("material_source", ["local", "pexels", "builtin", "wavespeed"])
def test_actual_application_exposes_documentary_before_material_forms(material_source):
    def render_workspace(service, tr):
        st.text("documentary-page")

    with (
        patch.object(
            footage, "get_search_service", return_value="test-service"
        ) as service,
        patch.object(ui, "render_workspace", side_effect=render_workspace) as render,
    ):
        app = AppTest.from_string(_actual_application_script(material_source)).run()
        assert not list(app.exception)
        assert app.radio(key="application_workspace").value == "video"
        assert {row.value for row in app.text} == {
            "video-script-form",
            "video-material-form",
        }
        service.assert_not_called()
        assert app.session_state["config_save_count"] == 1

        app.radio(key="application_workspace").set_value("documentary").run()
        assert not list(app.exception)
        assert {row.value for row in app.text} == {"documentary-page"}
        service.assert_called_once_with()
        assert render.call_args.args[0] == "test-service"
        assert app.session_state["config_save_count"] == 2


FACTS_SCRIPT = """
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui._render_claims_timeline(workspace, workspace.cases[0], lambda key: key)
"""


def test_switching_workspaces_preserves_unfinished_video_content():
    script = _actual_application_script("pexels").replace(
        "_render_script_settings = lambda *args: st.text('video-script-form')",
        """def _render_script_settings(*args):
    for key in ("video_subject", "video_script", "video_terms"):
        st.session_state.setdefault(key, "")
        st.text_area(key, key=key)""",
    )
    with (
        patch.object(footage, "get_search_service", return_value="test-service"),
        patch.object(ui, "render_workspace"),
    ):
        app = AppTest.from_string(script).run()
        values = {
            "video_subject": "A film subject",
            "video_script": "Unfinished narration.",
            "video_terms": "courthouse, oil field",
        }
        for key, value in values.items():
            app.text_area(key=key).set_value(value)
        app.run()
        app.radio(key="application_workspace").set_value("documentary").run()
        assert not list(app.exception)
        app.run()
        app.radio(key="application_workspace").set_value("video").run()
        assert not list(app.exception)
        for key, value in values.items():
            assert app.text_area(key=key).value == value


def test_task_restore_opens_video_and_replaces_the_saved_draft():
    with patch.object(ui, "render_workspace"):
        app = AppTest.from_string(_actual_application_script("pexels"))
        app.session_state["application_workspace"] = "documentary"
        app.session_state["application_active_workspace"] = "documentary"
        app.session_state["application_video_draft"] = {"video_subject": "Old draft"}
        restored = {
            "video_subject": "Restored subject",
            "video_script": "Restored narration",
            "video_terms": "restored footage",
        }
        for key, value in restored.items():
            app.session_state[key] = value
        app.session_state["task_restore_succeeded"] = True
        app.run()
        assert not list(app.exception)
        assert app.radio(key="application_workspace").value == "video"
        assert app.session_state["application_video_draft"] == restored
        for key, value in restored.items():
            assert app.session_state[key] == value


def _fact_workspace():
    workspace = NavigationWorkspace()
    workspace.list_assets.return_value = [
        {
            "id": "asset-a",
            "filename": "Court_Opinion.pdf",
            "relative_path": "01_Legal_Docs/Court_Opinion.pdf",
            "metadata": {},
        }
    ]
    workspace.list_claims.return_value = [
        {
            "id": "fact-a",
            "text": "The court record describes a disputed assertion.",
            "status": "disputed",
            "assertion_class": "court_finding",
            "reviewed_by": "Editor",
            "has_stale_citations": True,
            "citations": [
                {
                    "unit_id": "unit-support",
                    "asset_id": "asset-a",
                    "asset_version_id": "version-a",
                    "locator": {"kind": "page", "page_index": 2},
                    "relation": "supports",
                    "quote": "The source describes the assertion.",
                    "is_current": True,
                },
                {
                    "unit_id": "unit-conflict",
                    "asset_id": "asset-a",
                    "asset_version_id": "version-old",
                    "locator": {"kind": "page", "page_index": 4},
                    "relation": "contradicts",
                    "quote": "The source also reports conflicting evidence.",
                    "is_current": False,
                },
                {
                    "unit_id": "unit-mention",
                    "asset_id": "asset-a",
                    "relation": "mentions",
                    "is_current": True,
                },
            ],
        }
    ]
    return workspace


def test_facts_browse_is_readable_and_creation_form_is_collapsed():
    workspace = _fact_workspace()
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(FACTS_SCRIPT).run()
        assert not list(app.exception)
        assert app.radio(key=ui._key("case-a", "facts_section")).value == "Facts"
        assert app.text[0].value == workspace.list_claims.return_value[0]["text"]
        assert any("Fact disputed" in row.value for row in app.caption)
        assert any(
            "Court_Opinion.pdf" in row.value and "p. 3" in row.value
            for row in app.caption
        )
        assert any(row.value == "Fact source changed help" for row in app.warning)
        assert any(row.value == "Evidence source changed" for row in app.caption)
        assert not list(app.get("json"))
        editor = next(
            row
            for row in app.get("expander")
            if row.label == "Add or review a case fact"
        )
        assert not editor.proto.expanded
        assert not any(row.label == "Event title" for row in app.text_input)


def test_fact_review_action_loads_selected_fact_and_keeps_citation_relations():
    workspace = _fact_workspace()
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(FACTS_SCRIPT).run()
        app.button(key=ui._key("case-a", "review_fact_fact-a")).click().run()
        assert not list(app.exception)
        assert app.selectbox(key=ui._key("case-a", "claim_id")).value == "fact-a"
        assert (
            next(row for row in app.text_area if row.label == "Claim text").value
            == workspace.list_claims.return_value[0]["text"]
        )
        assert next(
            row
            for row in app.get("expander")
            if row.label == "Add or review a case fact"
        ).proto.expanded

        next(row for row in app.button if row.label == "Save claim").click().run()
        assert not list(app.exception)
        record = workspace.save_claim.call_args.args[1]
        assert record["id"] == "fact-a"
        assert record["status"] == "disputed"
        assert record["reviewed_by"] == "Editor"
        assert [row["relation"] for row in record["citations"]] == [
            "supports",
            "contradicts",
            "mentions",
        ]
        assert record["citations"][1]["asset_version_id"] == "version-old"
        assert all("is_current" not in row for row in record["citations"])


def test_timeline_switch_separates_event_creation_and_preserves_uncertain_date():
    workspace = NavigationWorkspace()
    workspace.list_events.return_value = [
        {
            "id": "event-a",
            "title": "An event with an uncertain date",
            "event_at": None,
            "citations": [],
        }
    ]
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(FACTS_SCRIPT).run()
        app.radio(key=ui._key("case-a", "facts_section")).set_value("Timeline").run()
        assert not list(app.exception)
        assert any(row.value == "Event date unknown" for row in app.caption)
        assert any(row.value == "An event with an uncertain date" for row in app.text)
        assert not any(row.label == "Claim text" for row in app.text_area)
        assert not next(
            row for row in app.get("expander") if row.label == "Add a timeline event"
        ).proto.expanded

        next(row for row in app.text_input if row.label == "Event title").set_value(
            "Second event"
        )
        next(
            row for row in app.selectbox if row.label == "Event time precision"
        ).select("month")
        next(
            row for row in app.button if row.label == "Save timeline event"
        ).click().run()
        assert not list(app.exception)
        record = workspace.save_event.call_args.args[1]
        assert record == {
            "title": "Second event",
            "event_at": None,
            "time_precision": "month",
            "notes": "",
            "citations": [],
        }


def test_fact_save_denial_remains_visible_without_success_or_auto_review():
    from app.models.search import SearchError

    workspace = NavigationWorkspace()
    workspace.save_claim.side_effect = SearchError(
        "Reviewed claims require a reviewer and retained evidence citations."
    )
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(FACTS_SCRIPT).run()
        next(row for row in app.text_area if row.label == "Claim text").set_value(
            "A fact needing review"
        )
        next(row for row in app.selectbox if row.label == "Claim status").select(
            "reviewed"
        )
        next(row for row in app.button if row.label == "Save claim").click().run()
        assert not list(app.exception)
        assert any("require a reviewer" in row.value for row in app.error)
        assert not list(app.success)
        assert workspace.save_claim.call_args.args[1]["reviewed_by"] == ""
        assert workspace.save_claim.call_args.args[1]["citations"] == []


def test_translated_fact_widgets_keep_internal_values_on_edit_and_rerun():
    workspace = _fact_workspace()
    script = """
import streamlit as st
from webui import case_workspace as ui
labels = {
    'en': {'Facts': 'Facts', 'Timeline': 'Timeline', 'Fact needs review': 'Needs review',
           'Fact reviewed': 'Reviewed', 'Fact disputed': 'Disputed',
           'Fact needs evidence': 'More evidence needed',
           'Fact class unclassified': 'Unclassified', 'Fact class allegation': 'Allegation',
           'Fact class court finding': 'Court finding'},
    'zh': {'Facts': '事实', 'Timeline': '时间线', 'Fact needs review': '待审核',
           'Fact reviewed': '已审核', 'Fact disputed': '有争议',
           'Fact needs evidence': '需要更多证据',
           'Fact class unclassified': '未分类', 'Fact class allegation': '指控',
           'Fact class court finding': '法院认定', 'New claim': '新增事实'},
}
st.session_state['fact_test_language'] = 'zh'
def tr(key):
    return labels[st.session_state.get('fact_test_language', 'en')].get(key, key)
workspace = ui.get_workspace(None)
ui._render_claims_timeline(workspace, workspace.cases[0], tr)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(script).run()
        assert not list(app.exception)
        assert app.radio(key=ui._key("case-a", "facts_section")).options == [
            "事实",
            "时间线",
        ]
        status = next(row for row in app.selectbox if row.label == "Claim status")
        assert status.options[0] == "待审核"
        status.select("reviewed")
        next(
            row for row in app.selectbox if row.label == "Claim assertion class"
        ).select("court_finding")
        next(
            row for row in app.text_input if row.label == "Claim reviewed by"
        ).set_value("Editor")
        next(row for row in app.text_area if row.label == "Claim text").set_value(
            "Source-backed statement"
        )
        next(row for row in app.button if row.label == "Save claim").click().run()
        assert not list(app.exception)
        saved = workspace.save_claim.call_args.args[1]
        assert saved["status"] == "reviewed"
        assert saved["assertion_class"] == "court_finding"
        assert saved["reviewed_by"] == "Editor"

        app.button(key=ui._key("case-a", "review_fact_fact-a")).click().run()
        assert not list(app.exception)
        assert app.selectbox(key=ui._key("case-a", "claim_id")).value == "fact-a"
        app.radio(key=ui._key("case-a", "facts_section")).set_value("Timeline").run()
        assert not list(app.exception)
        assert app.radio(key=ui._key("case-a", "facts_section")).value == "Timeline"
        app.radio(key=ui._key("case-a", "facts_section")).set_value("Facts").run()
        assert not list(app.exception)
