from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from streamlit.testing.v1 import AppTest

from webui import targeted_search as ui


def test_timestamp_link_preserves_video_identity_and_has_no_metadata_timestamp():
    url = "https://www.youtube.com/watch?v=lesson&list=library"
    assert ui.timestamp_link(url) == url
    assert ui.timestamp_link(url, 465123) == url + "&t=465"
    assert ui.timestamp_link("https://user:secret@example.com/video") is None
    assert ui.timestamp_link("file:///private/video.mp4") is None


def test_selected_range_rejects_invalid_or_out_of_source_bounds():
    assert ui.checked_range(4.125, 5.567, 6000) == (4125, 5567)
    for start, end in [(5, 5), (6, 5), (-1, 5), (0, 6.1)]:
        with pytest.raises(ValueError):
            ui.checked_range(start, end, 6000)


def test_selected_artifacts_survive_restore_without_upload_paths():
    state = {ui.SELECTED_ARTIFACTS_KEY: ["old"]}
    ui.restore_artifact_selection(
        {"search_artifact_ids": ["clip-1", "clip-1", "clip-2"]}, state
    )
    ui.select_artifact("clip-1", state)
    assert ui.selected_artifact_ids(state) == ["clip-1", "clip-2"]
    ui.remove_artifact("clip-1", state)
    assert ui.selected_artifact_ids(state) == ["clip-2"]
    ui.restore_artifact_selection({}, state)
    assert ui.selected_artifact_ids(state) == []


def test_search_backend_is_lazy_and_artifact_refs_pass_to_generation():
    script = """
import streamlit as st
from app.models.schema import VideoParams
from webui import targeted_search as ui
params = VideoParams(video_subject='fractions', video_source='local')
ui.render_search_button(lambda key: key, params)
st.session_state['generation_refs'] = params.search_artifact_ids
"""
    with patch.object(ui, "get_search_service") as service:
        app = AppTest.from_string(script)
        app.session_state[ui.SELECTED_ARTIFACTS_KEY] = ["clip-1"]
        app.run()
        assert list(app.exception) == []
        service.assert_not_called()
        assert app.session_state["generation_refs"] == ["clip-1"]


def test_footage_shortcut_routes_to_workspace_and_source_form_queues_discovery():
    service = SimpleNamespace(
        settings={"enabled": True},
        list_collections=Mock(return_value=[]),
        list_sources=Mock(return_value=[]),
        list_jobs=Mock(return_value=[]),
        list_artifacts=Mock(return_value=[]),
        discover=Mock(return_value={"id": "source-1", "job_id": "discovery-1"}),
    )
    script = """
from app.models.schema import VideoParams
from webui import targeted_search as ui
ui.render_search_button(lambda key: key, VideoParams(video_subject='fractions'))
"""
    with patch.object(ui, "get_search_service", return_value=service):
        app = AppTest.from_string(script).run()
        _button(app, "Search clips from source library").click().run()
        assert list(app.exception) == []
        assert app.session_state["application_pending_workspace"] == "documentary"
        assert app.session_state["case_workspace_pending_view"] == "Footage Search"
        service.list_sources.assert_not_called()
        # Source setup remains available on the full-page footage desk.
        app = AppTest.from_string("""
from webui import targeted_search as ui
ui._render_library(ui.get_search_service(), lambda key: key)
""").run()
        app.text_input(key="targeted_discover_url").set_value(
            "https://www.youtube.com/watch?v=lesson"
        )
        _button(app, "Discover metadata and captions").click().run()
        assert list(app.exception) == []
    service.discover.assert_called_once_with(
        "https://www.youtube.com/watch?v=lesson", collection_id=None
    )
    assert app.session_state["targeted_search_job_ids"] == ["discovery-1"]


def test_real_local_library_and_caption_search_render_without_models(tmp_path):
    from app.services.targeted_search.service import SearchService

    service = SearchService(tmp_path / "search")
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
    )
    service.repo.settings = service.settings
    source = service.register_metadata(
        "https://www.youtube.com/watch?v=ownedclip01",
        "Arithmetic lesson",
        "Owned recording",
        metadata={"duration_ms": 60000},
    )
    service.import_captions(
        source["id"],
        "WEBVTT\n\n00:00:01.000 --> 00:00:06.000\nFind the least common denominator.\n",
        format="vtt",
    )
    script = """
from webui import targeted_search as ui
service = ui.get_search_service()
ui._render_capabilities(service, lambda key: key)
collection = ui._render_library(service, lambda key: key)
ui._render_query(service, collection, lambda key: key)
ui._render_jobs_and_clips(service, lambda key: key)
"""
    with patch.object(ui, "get_search_service", return_value=service):
        app = AppTest.from_string(script).run()
        assert list(app.exception) == []
        next(
            item for item in app.text_input if item.label == "Search video evidence"
        ).set_value("least common denominator")
        _button(app, "Search clips").click().run()
        assert list(app.exception) == []
        assert any(item.value == "evidence_type.transcript" for item in app.caption)
        assert service.list_artifacts() == []


def test_attachment_selects_only_after_server_policy_check_succeeds():
    service = SimpleNamespace(repo=SimpleNamespace(root=Path("/search-root")))
    state = {}
    with patch(
        "app.services.targeted_search.attachments.attach_clip",
        return_value={"artifact_id": "clip-1"},
    ) as attach:
        ui.attach_selected_clip(service, "clip-1", state)
        attach.assert_called_once_with(
            "clip-1", requested_use="generated_export", root_dir=Path("/search-root")
        )
    assert ui.selected_artifact_ids(state) == ["clip-1"]
    with patch(
        "app.services.targeted_search.attachments.attach_clip",
        side_effect=ValueError("permission expired"),
    ):
        with pytest.raises(ValueError, match="expired"):
            ui.attach_selected_clip(service, "clip-2", state)
    assert ui.selected_artifact_ids(state) == ["clip-1"]


class FakeService:
    def __init__(self):
        self.source = {
            "id": "source-1",
            "title": "Fraction lesson",
            "duration_ms": 60000,
            "canonical_url": "https://www.youtube.com/watch?v=lesson",
            "policy": {
                "rights_status": "allowed_export",
                "permitted_use": "generated_export",
            },
        }
        self.result = {
            "id": "candidate-1",
            "candidate_id": "candidate-1",
            "source_id": "source-1",
            "title": "Fraction lesson",
            "canonical_url": self.source["canonical_url"],
            "evidence": "Lesson description mentions fractions",
            "evidence_type": "metadata",
            "start_ms": None,
            "end_ms": None,
            "scores": {"metadata": 1.0},
        }
        self.search = Mock(return_value={"results": [self.result]})
        self.approve_download = Mock()
        self.enqueue_clip = Mock(
            return_value={"id": "job-1", "job_type": "extract_clip"}
        )
        self.validate = Mock(
            return_value={"decision": "review", "reason": "Review source evidence."}
        )
        self.set_policy = Mock()

    def get_source(self, source_id):
        assert source_id == "source-1"
        return self.source


@pytest.fixture
def query_app():
    service = FakeService()
    script = """
from webui import targeted_search as ui
ui._render_query(ui.get_search_service(), 'collection-1', lambda key: key)
"""
    with patch.object(ui, "get_search_service", return_value=service):
        app = AppTest.from_string(script).run()
        yield app, service


def _button(app, label):
    return next(button for button in app.button if button.label == label)


def test_query_filters_metadata_evidence_and_approval_uses_reviewed_range(query_app):
    app, service = query_app
    app.text_input[0].set_value("subtract fractions")
    app.text_input[1].set_value("en")
    _button(app, "Search clips").click().run()
    assert list(app.exception) == []
    service.search.assert_called_once_with(
        "subtract fractions",
        filters={"collection_id": "collection-1", "language": "en"},
    )
    assert any(item.value == "Metadata evidence range help" for item in app.info)
    assert _button(app, "Approve and extract selected clip").disabled
    assert service.approve_download.call_count == 0
    app.number_input(key="targeted_start_candidate-1").set_value(12.5)
    app.number_input(key="targeted_end_candidate-1").set_value(20.25)
    app.checkbox(key="targeted_relevance_candidate-1").check().run()
    _button(app, "Approve and extract selected clip").click().run()
    assert list(app.exception) == []
    service.approve_download.assert_called_once_with(
        "candidate-1",
        requested_use="internal_review",
        reviewed_by="local-user",
        start_ms=12500,
        end_ms=20250,
    )
    service.enqueue_clip.assert_called_once_with(
        "candidate-1",
        start_ms=12500,
        end_ms=20250,
        requested_use="internal_review",
    )
    assert app.session_state["targeted_search_job_ids"] == ["job-1"]


def test_failed_policy_approval_never_enqueues_acquisition(query_app):
    app, service = query_app
    app.text_input[0].set_value("fractions")
    _button(app, "Search clips").click().run()
    app.checkbox(key="targeted_relevance_candidate-1").check().run()
    service.approve_download.side_effect = ValueError("rights do not permit this use")
    _button(app, "Approve and extract selected clip").click().run()
    service.enqueue_clip.assert_not_called()
    assert any("rights do not permit" in item.value for item in app.error)
    assert list(app.exception) == []


def test_invalid_source_range_never_records_approval(query_app):
    app, service = query_app
    app.text_input[0].set_value("fractions")
    _button(app, "Search clips").click().run()
    app.number_input(key="targeted_end_candidate-1").set_value(61)
    app.checkbox(key="targeted_relevance_candidate-1").check().run()
    _button(app, "Approve and extract selected clip").click().run()
    service.approve_download.assert_not_called()
    service.enqueue_clip.assert_not_called()
    assert any("beyond the source duration" in item.value for item in app.error)


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_translated_search_filters_keep_internal_values_across_reruns(locale):
    service = SimpleNamespace(search=Mock(return_value={"results": []}))
    script = f"""
import json
from pathlib import Path
import streamlit as st
from webui import targeted_search as ui
translations = json.loads(Path({str(Path(__file__).resolve().parents[2] / "webui/i18n")!r}, {locale + ".json"!r}).read_text())["Translation"]
st.session_state.setdefault("ui_language", {locale!r})
def tr(key):
    return translations.get(key, key) if st.session_state["ui_language"] == {locale!r} else key
ui._render_query(ui.get_search_service(), "collection-a", tr, source_ids=["video-a"])
"""
    with patch.object(ui, "get_search_service", return_value=service):
        app = AppTest.from_string(script).run()
        assert not list(app.exception)
        app.text_input[0].set_value("a witness")
        app.selectbox[0].select("allowed_internal")
        app.button[0].click().run()
        assert not list(app.exception)
        service.search.assert_called_once_with(
            "a witness",
            filters={
                "collection_id": "collection-a",
                "source_ids": ["video-a"],
                "rights_status": "allowed_internal",
            },
        )
