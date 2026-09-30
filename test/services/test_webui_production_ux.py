"""Edit review stays accessible while production controls keep their bindings."""

from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from test.services.test_webui_case_workspace import FakeWorkspace, app_for, button
from webui import case_workspace as ui


def test_empty_edit_has_script_and_footage_navigation_and_optional_setup():
    workspace = FakeWorkspace()
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch("webui.case_design.request_view") as navigate,
    ):
        app = app_for("_render_production", workspace)
        assert not list(app.exception)
        assert button(app, "Render saved storyboard").disabled
        assert all(
            not item.proto.expanded
            for item in app.expander
            if item.label
            in {"Production build scenes", "Production narration and save"}
        )
        button(app, "Production open script").click().run()
        navigate.assert_called_once_with("case-a", "Documentary Writer")
        navigate.reset_mock()
        button(app, "Production find footage").click().run()
        navigate.assert_called_once_with("case-a", "Footage Search")
        workspace.enqueue_render.assert_not_called()


def test_preview_and_current_failure_precede_setup_and_reauthorize_export(tmp_path):
    workspace = FakeWorkspace()
    workspace.list_storyboards.return_value = [
        {
            "id": "story-a",
            "title": "Documentary edit",
            "scenes": [
                {"scene_id": "scene_001", "asset_id": "asset-a", "duration_ms": 5000}
            ],
        }
    ]
    workspace.search_service.list_jobs.return_value = [
        {
            "id": "other-case-render",
            "job_type": "case_render",
            "status": "complete",
            "payload": {"case_id": "case-b", "storyboard_id": "other-story"},
            "result": {"artifact_id": "other-case-artifact"},
        },
        {
            "id": "failed-render",
            "job_type": "case_render",
            "status": "failed",
            "payload": {"case_id": "case-a", "storyboard_id": "story-a"},
            "last_error": "Render stopped: source permission changed",
        },
        {
            "id": "completed-render",
            "job_type": "case_render",
            "status": "complete",
            "payload": {"case_id": "case-a", "storyboard_id": "story-a"},
            "result": {"artifact_id": "video-a", "manifest_artifact_id": "manifest-a"},
        },
    ]
    video = tmp_path / "Review.mp4"
    video.write_bytes(b"owned streamlit preview fixture")
    manifest = tmp_path / "Provenance.json"
    manifest.write_text("{}")
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch(
            "app.services.targeted_search.case_media.preview_content",
            side_effect=lambda _workspace, identifier, requested_use: {
                "video-a": video,
                "manifest-a": manifest,
            }[identifier],
        ) as deliver,
    ):
        app = app_for("_render_production", workspace)
        assert not list(app.exception)
        assert len(app.get("video")) == 1
        assert len(app.get("download_button")) == 2
        assert any("source permission changed" in item.value for item in app.error)
        elements = list(app.main)
        setup_index = next(
            index
            for index, element in enumerate(elements)
            if element.type == "expander" and element.label == "Production build scenes"
        )
        assert (
            next(
                index
                for index, element in enumerate(elements)
                if element.type == "video"
            )
            < setup_index
        )
        assert (
            next(
                index
                for index, element in enumerate(elements)
                if element.type == "error"
            )
            < setup_index
        )
        assert [call.args[1] for call in deliver.call_args_list] == [
            "video-a",
            "manifest-a",
        ]
        assert all(
            call.kwargs == {"requested_use": "generated_export"}
            for call in deliver.call_args_list
        )
        deliver.side_effect = ValueError("Preview permission revoked")
        app.run()
        assert not list(app.exception)
        assert not list(app.get("video"))
        assert not list(app.get("download_button"))
        assert any(item.value == "Preview permission revoked" for item in app.error)


def test_production_choices_use_state_translation_without_losing_scene_precision():
    workspace = FakeWorkspace()
    script = """
import streamlit as st
from webui import case_workspace as ui
st.session_state['production_test_labels'] = {'New storyboard': 'Start an edit', 'New scene': 'Add a scene', 'No visual overlay': 'No background'}
def tr(key):
    return st.session_state['production_test_labels'].get(key, key)
workspace = ui.get_workspace(None)
ui._render_production(workspace, workspace.case, tr)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(script).run()
        button(app, "Save storyboard scene").click().run()
        assert not list(app.exception)
        assert not list(app.error)
        scene = app.session_state[ui._key("case-a", "draft_scenes")][0]
        assert scene["locator"] == {"kind": "page", "page_index": 0}
        assert scene["asset_id"] == "asset-a"
        assert (
            app.selectbox(key=ui._key("case-a", "storyboard_id")).options[0]
            == "Start an edit"
        )
        assert "asset-a" not in app.dataframe[0].value.to_json()
        assert "scene_001" not in app.dataframe[0].value.to_json()
        app.run()
        assert not list(app.exception)
