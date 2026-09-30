import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from streamlit.testing.v1 import AppTest

from webui import case_workspace as ui
from webui import targeted_search as footage


def button(app, label):
    return next(item for item in app.button if item.label == label)


class FakeWorkspace:
    def __init__(self):
        self.case = {
            "id": "case-a",
            "name": "Case A",
            "collection_id": "collection-a",
            "source_ids": ["video-a"],
        }
        self.asset = {
            "id": "asset-a",
            "case_id": "case-a",
            "source_id": "source-a",
            "filename": "Warrant_Affidavit.pdf",
            "relative_path": "01_Legal_Docs/Warrant_Affidavit.pdf",
            "asset_kind": "document",
            "category": "Legal",
            "sha256": "a" * 64,
            "version": 1,
            "state": "imported",
            "metadata": {},
        }
        self.search_service = SimpleNamespace(
            repo=SimpleNamespace(root=Path("/search-root")),
            settings={"enabled": True},
            list_collections=Mock(return_value=[]),
            list_sources=Mock(return_value=[]),
            list_jobs=Mock(return_value=[]),
            list_artifacts=Mock(return_value=[]),
            get_source=Mock(return_value={"id": "source-a", "policy": {}}),
            search=Mock(return_value={"results": []}),
        )
        self.result = {
            "id": "unit-a",
            "unit_id": "unit-a",
            "asset_id": "asset-a",
            "filename": self.asset["filename"],
            "asset_kind": "document",
            "locator": {"kind": "page", "page_index": 2},
            "evidence": "The filing describes the search.",
            "scores": {"lexical": 1},
        }
        self.list_cases = Mock(return_value=[self.case])
        self.get_case = Mock(return_value=self.case)
        self.list_assets = Mock(return_value=[self.asset])
        self.get_asset = Mock(return_value=self.asset)
        self.list_requests = Mock(return_value=[])
        self.list_claims = Mock(return_value=[])
        self.list_events = Mock(return_value=[])
        self.list_storyboards = Mock(return_value=[])
        self.export_case = Mock(return_value={"case": self.case})
        self.save_request = Mock()
        self.save_claim = Mock()
        self.save_event = Mock()
        self.enqueue_index = Mock(return_value={"id": "index-a"})
        self.import_folder = Mock(return_value={"imported": 1})
        self.link_source = Mock()
        self.search_supporting = Mock(
            return_value={
                "groups": {"document": [self.result]},
                "results": [self.result],
            }
        )
        self.enqueue_render = Mock(return_value={"id": "render-a"})

        def save_storyboard(case_id, record):
            from app.models.case_workspace import StoryboardRecord

            StoryboardRecord.model_validate(record)
            return {**record, "id": "story-a"}

        self.save_storyboard = Mock(side_effect=save_storyboard)


def app_for(function, workspace):
    script = f"""
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui.{function}(workspace, workspace.case, {"workspace.search_service, " if function in {"_render_library", "_render_search_all"} else ""}lambda key: key)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        return AppTest.from_string(script).run()


def test_case_switch_retains_only_current_case_selections_and_clears_evidence():
    state = {
        footage.SELECTED_ARTIFACTS_KEY: ["library-clip"],
        "targeted_search_results": ["old"],
        "targeted_validation_old": {"decision": "approve"},
    }
    footage.switch_scope("case-a", state)
    assert footage.selected_artifact_ids(state) == []
    assert "targeted_search_results" not in state
    assert "targeted_validation_old" not in state
    footage.select_artifact("case-a-clip", state)
    footage.switch_scope("case-b", state)
    footage.select_artifact("case-b-clip", state)
    footage.switch_scope("case-a", state)
    assert footage.selected_artifact_ids(state) == ["case-a-clip"]
    footage.switch_scope(None, state)
    assert footage.selected_artifact_ids(state) == ["library-clip"]


def test_restore_artifacts_resets_case_scope_without_mixing_case_selections():
    state = {
        footage.ACTIVE_SCOPE_KEY: "case-a",
        footage.SELECTED_ARTIFACTS_KEY: ["case-a-clip"],
    }
    footage.restore_artifact_selection(
        {"search_artifact_ids": ["restored-clip"]}, state
    )
    assert state[footage.ACTIVE_SCOPE_KEY] == "source-library"
    assert state["targeted_search_case"] is None
    footage.switch_scope("case-a", state)
    assert footage.selected_artifact_ids(state) == ["case-a-clip"]


def test_saved_citations_are_case_scoped_and_keep_exact_location():
    workspace = FakeWorkspace()
    state = {}
    identifier = ui.save_evidence("case-a", workspace.result, state)
    ui.save_evidence("case-a", workspace.result, state)
    assert len(state[ui._key("case-a", "evidence")]) == 1
    assert ui._key("case-b", "evidence") not in state
    assert state[ui._key("case-a", "evidence")][identifier]["citation"] == {
        "unit_id": "unit-a",
        "relation": "supports",
        "quote": workspace.result["evidence"],
    }
    assert ui.locator_label(workspace.result["locator"]) == "p. 3"
    assert (
        ui.locator_label({"kind": "time", "start_ms": 1200, "end_ms": 4567})
        == "1.200–4.567s"
    )


def test_asset_labels_show_linked_titles_with_unique_source_ids_and_original_import_paths():
    linked = {
        "id": "asset-a",
        "filename": "TEGNA News",
        "relative_path": "linked/web:source-a",
        "source_id": "web:source-a",
        "metadata": {"linked": True},
    }
    assert ui.asset_label(linked) == "TEGNA News · web:source-a"
    other = {**linked, "id": "asset-b", "source_id": "youtube:source-b"}
    assert ui.asset_label(other) == "TEGNA News · youtube:source-b"
    assert ui.asset_label(linked) != ui.asset_label(other)
    imported = {
        **linked,
        "relative_path": "03_Video_Footage/Original_Name.mp4",
        "filename": "Original_Name.mp4",
        "metadata": {},
    }
    assert ui.asset_label(imported) == "03_Video_Footage/Original_Name.mp4"


def test_workspace_default_is_footage_and_case_query_has_fixed_collection():
    workspace = FakeWorkspace()
    script = """
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui.render_workspace(workspace.search_service, lambda key: key)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(script).run()
        assert not list(app.exception)
        assert app.radio[0].value == "Footage Search"
        app.selectbox(key="targeted_search_case").select("case-a").run()
        assert not list(app.exception)
        workspace.search_service.list_sources.assert_called_with(
            collection_id="collection-a"
        )
        assert not any(item.label == "Source collection" for item in app.selectbox)


def test_supporting_search_omits_production_and_keeps_separate_video_search():
    workspace = FakeWorkspace()
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch.object(ui, "render_asset_preview"),
    ):
        app = app_for("_render_search_all", workspace)
        next(
            item for item in app.text_input if item.label == "Search all case evidence"
        ).set_value("search")
        button(app, "Search case workspace").click().run()
        assert not list(app.exception)
        workspace.search_supporting.assert_called_once_with(
            "case-a",
            "search",
            filters={
                "include_production": False,
                "asset_kinds": [
                    "document",
                    "audio",
                    "video",
                    "image",
                    "map",
                    "transcript",
                ],
            },
        )
        workspace.search_service.search.assert_called_once_with(
            "search",
            filters={
                "collection_id": "collection-a",
                "source_ids": [],
                "include_production": False,
            },
        )
        assert any(item.value == "p. 3" for item in app.caption)
        button(app, "Save evidence citation").click().run()
        assert app.session_state[ui._key("case-a", "evidence")]
        next(
            item for item in app.checkbox if item.label == "Include production drafts"
        ).check()
        button(app, "Search case workspace").click().run()
        assert not list(app.exception)
        assert (
            workspace.search_service.search.call_args.kwargs["filters"][
                "include_production"
            ]
            is True
        )
        assert (
            workspace.search_supporting.call_args.kwargs["filters"][
                "include_production"
            ]
            is True
        )


def test_library_preserves_filename_and_import_does_not_auto_index():
    workspace = FakeWorkspace()
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch.object(ui, "render_asset_preview"),
    ):
        app = app_for("_render_library", workspace)
        assert not list(app.exception)
        assert (
            workspace.asset["relative_path"]
            in app.dataframe[0].value["Original filename"].tolist()
        )
        app.text_input(key=ui._key("case-a", "folder")).set_value("/owned/case")
        button(app, "Import originals").click().run()
        workspace.import_folder.assert_called_once_with(
            "case-a", "/owned/case", index=False
        )
        workspace.enqueue_index.assert_not_called()
        app.multiselect(key=ui._key("case-a", "index_assets")).select("asset-a").run()
        button(app, "Index selected assets").click().run()
        workspace.enqueue_index.assert_called_once_with("asset-a")


def test_claim_submission_keeps_exact_citation_and_review_status():
    workspace = FakeWorkspace()
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = app_for("_render_claims_timeline", workspace)
        identifier = ui.save_evidence("case-a", workspace.result, {})
        basket = {
            identifier: {
                "label": "Warrant_Affidavit.pdf · p. 3",
                "citation": ui.citation_from_result(workspace.result),
            }
        }
        app.session_state[ui._key("case-a", "evidence")] = basket
        app.run()
        next(item for item in app.text_area if item.label == "Claim text").set_value(
            "The filing describes the search."
        )
        next(
            item for item in app.text_input if item.label == "Claim reviewed by"
        ).set_value("editor")
        next(item for item in app.selectbox if item.label == "Claim status").select(
            "reviewed"
        )
        app.multiselect(key=ui._key("case-a", "claim_citations_None")).select(
            identifier
        )
        button(app, "Save claim").click().run()
        assert not list(app.exception)
        record = workspace.save_claim.call_args.args[1]
        assert record["status"] == "reviewed"
        assert record["reviewed_by"] == "editor"
        assert record["assertion_class"] == "unclassified"
        assert record["citations"] == [ui.citation_from_result(workspace.result)]


def test_storyboard_saves_explicit_static_asset_and_unit_references():
    workspace = FakeWorkspace()
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = app_for("_render_production", workspace)
        next(item for item in app.selectbox if item.label == "Scene role").select(
            "document"
        )
        button(app, "Save storyboard scene").click().run()
        assert not list(app.exception)
        scenes = app.session_state[ui._key("case-a", "draft_scenes")]
        assert scenes[0]["asset_id"] == "asset-a"
        assert scenes[0]["source_start_ms"] is None
        assert scenes[0]["locator"] == {"kind": "page", "page_index": 0}
        app.text_input(key=ui._key("case-a", "storyboard_title")).set_value("Case edit")
        app.text_area(key=ui._key("case-a", "storyboard_script")).set_value("Narration")
        button(app, "Save storyboard").click().run()
        assert not list(app.exception)
        record = workspace.save_storyboard.call_args.args[1]
        assert record["scenes"][0]["asset_id"] == "asset-a"
        assert (
            record["metadata"]["script_sha256"]
            == hashlib.sha256(b"Narration").hexdigest()
        )
        button(app, "Render saved storyboard").click().run()
        workspace.enqueue_render.assert_called_once_with(
            "story-a", requested_use="generated_export"
        )


def test_case_job_panel_excludes_other_cases():
    workspace = FakeWorkspace()
    workspace.search_service.list_jobs.return_value = [
        {
            "id": "job-a",
            "job_type": "case_index",
            "status": "queued",
            "payload": {"asset_id": "asset-a", "case_id": "case-a"},
        },
        {
            "id": "job-b",
            "job_type": "case_index",
            "status": "failed",
            "last_error": "Other case error",
            "payload": {"asset_id": "asset-b", "case_id": "case-b"},
        },
    ]
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = app_for("_render_case_jobs", workspace)
        assert not list(app.exception)
        assert [item.value for item in app.caption] == ["case_index · queued"]
        assert not list(app.error)


def test_preview_denied_by_policy_never_reads_or_displays_asset():
    workspace = FakeWorkspace()
    script = """
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui.render_asset_preview(workspace, workspace.asset, lambda key: key)
"""
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch(
            "app.services.targeted_search.case_media.asset_content",
            side_effect=ValueError("Source permission revoked"),
        ),
        patch("app.services.targeted_search.case_media.preview_asset") as preview,
    ):
        app = AppTest.from_string(script).run()
        assert not list(app.exception)
        assert app.error[0].value == "Source permission revoked"
        preview.assert_not_called()


def test_invalid_scene_cannot_use_other_case_asset_or_unbounded_range():
    assets = {"audio-a": {"asset_kind": "audio"}}
    scene = {
        "scene_id": "scene_001",
        "asset_id": "audio-a",
        "role": "original_sound",
        "duration_ms": 5000,
        "source_start_ms": 1000,
        "source_end_ms": 500,
        "speed": 1,
    }
    with pytest.raises(ValueError, match="end time"):
        ui.validate_scene(scene, assets)
    with pytest.raises(ValueError, match="case asset"):
        ui.validate_scene({**scene, "asset_id": "other-case-audio"}, assets)


def test_actual_complete_render_worker_result_shows_policy_checked_preview(tmp_path):
    from dataclasses import replace
    from PIL import Image
    from app.services.targeted_search.case_workspace import CaseWorkspace
    from app.services.targeted_search.service import SearchService
    from app.services.targeted_search.worker import process_once

    service = SearchService(tmp_path / "library")
    service.settings = replace(
        service.settings,
        enabled=True,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Owned UI render fixture")
    incoming = service.repo.root / "owned" / "incoming"
    incoming.mkdir(parents=True)
    Image.new("RGB", (160, 120), "blue").save(incoming / "Original_Image.png")
    asset = workspace.import_folder(case["id"], incoming)["assets"][0]
    service.set_policy(
        asset["source_id"],
        "allowed_export",
        "internal_review,analysis,generated_export",
        "Owned synthetic image",
        "test-owner",
    )
    storyboard = workspace.save_storyboard(
        case["id"],
        {
            "title": "Preview proof",
            "scenes": [
                {
                    "scene_id": "scene_001",
                    "asset_id": asset["id"],
                    "duration_ms": 500,
                    "role": "still",
                }
            ],
        },
    )
    queued = workspace.enqueue_render(storyboard["id"])
    process_once(service)
    assert service.get_job(queued["id"])["status"] == "complete"
    script = f"""
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui._render_case_jobs(workspace, workspace.get_case({case["id"]!r}), lambda key: key)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(script).run()
        assert not list(app.exception)
        assert not list(app.error)
        assert len(app.get("video")) == 1
        assert len(app.get("download_button")) == 2


def test_editing_claim_preserves_conflicting_citations_and_strips_server_fields():
    workspace = FakeWorkspace()
    citation = {
        **ui.citation_from_result(workspace.result),
        "relation": "contradicts",
        "asset_id": "asset-a",
        "locator": workspace.result["locator"],
        "asset_version_id": "version-a",
        "evidence_hash": "hash-a",
        "is_current": True,
    }
    workspace.list_claims.return_value = [
        {
            "id": "claim-a",
            "text": "Contested assertion",
            "status": "disputed",
            "citations": [citation],
        }
    ]
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = app_for("_render_claims_timeline", workspace)
        app.selectbox(key=ui._key("case-a", "claim_id")).select("claim-a").run()
        button(app, "Save claim").click().run()
        assert not list(app.exception)
        retained = workspace.save_claim.call_args.args[1]["citations"]
        assert retained[0]["relation"] == "contradicts"
        assert retained[0]["unit_id"] == "unit-a"
        assert retained[0]["asset_version_id"] == "version-a"
        assert not {"evidence_hash", "is_current"} & set(retained[0])


def test_footage_citation_uses_asset_range_instead_of_candidate_as_evidence_unit():
    workspace = FakeWorkspace()
    workspace.list_assets.return_value = [
        {
            **workspace.asset,
            "id": "video-asset",
            "source_id": "video-a",
            "asset_kind": "video",
            "filename": "Original_Footage.mp4",
        }
    ]
    workspace.result = {
        "id": "candidate-a",
        "candidate_id": "candidate-a",
        "source_id": "video-a",
        "start_ms": 1250,
        "end_ms": 4567,
        "evidence": "Search snippet",
    }
    script = """
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui._render_footage_citation(workspace, workspace.case, workspace.result, lambda key: key)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(script).run()
        button(app, "Save footage range citation").click().run()
        assert not list(app.exception)
        citation = next(
            iter(app.session_state[ui._key("case-a", "evidence")].values())
        )["citation"]
        assert citation == {
            "asset_id": "video-asset",
            "locator": {"kind": "time", "start_ms": 1250, "end_ms": 4567},
            "relation": "supports",
        }


def test_prepare_case_folders_supplies_import_path_without_fake_evidence(tmp_path):
    from app.services.targeted_search.case_workspace import CaseWorkspace
    from app.services.targeted_search.service import SearchService

    service = SearchService(tmp_path / "library")
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Owned case preparation fixture")
    script = f"""
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui._render_library(workspace, workspace.get_case({case["id"]!r}), workspace.search_service, lambda key: key)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(script).run()
        button(app, "Prepare case folders").click().run()
        assert not list(app.exception)
        folder = Path(app.session_state[ui._key(case["id"], "folder")])
        assert folder.is_relative_to(workspace.repo.root / "owned")
        assert all(
            (folder / bucket).is_dir()
            for bucket in (
                "01_Legal_Docs",
                "02_Audio_Raw",
                "03_Video_Footage",
                "04_Still_Assets",
                "05_Production",
            )
        )
        assert not list(folder.rglob("*.wav")) and not list(folder.rglob("*.mp4"))
        assert app.text_input(key=ui._key(case["id"], "folder")).value == str(folder)


def test_visual_match_cites_asset_without_nonexistent_unit_and_rejects_changed_version(
    tmp_path,
):
    from PIL import Image
    from app.models.search import SearchError
    from app.services.targeted_search.case_workspace import CaseWorkspace
    from app.services.targeted_search.service import SearchService

    service = SearchService(tmp_path / "library")
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Owned visual citation fixture")
    incoming = service.repo.root / "owned" / "incoming"
    incoming.mkdir(parents=True)
    source = incoming / "Original_Image.png"
    Image.new("RGB", (32, 32), "blue").save(source)
    asset = workspace.import_folder(case["id"], incoming)["assets"][0]
    result = {
        "id": "visual:" + asset["id"],
        "unit_id": None,
        "asset_id": asset["id"],
        "asset_version_id": asset["asset_version_id"],
        "locator": {"kind": "image"},
        "evidence_type": "visual",
        "evidence": "Visual similarity to the query",
    }
    citation = ui.citation_from_result(result)
    assert "unit_id" not in citation and "quote" not in citation
    assert citation["asset_version_id"] == asset["asset_version_id"]
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "Image selected for review",
            "status": "proposed",
            "citations": [citation],
        },
    )
    assert claim["citations"][0]["asset_id"] == asset["id"]
    assert claim["citations"][0]["unit_id"] is None
    Image.new("RGB", (32, 32), "red").save(source)
    workspace.import_folder(case["id"], incoming)
    with pytest.raises(SearchError, match="obsolete asset version"):
        workspace.save_claim(
            case["id"], {"text": "Unreviewed replacement", "citations": [citation]}
        )


def test_audio_background_choices_exclude_unsupported_video_assets():
    workspace = FakeWorkspace()
    workspace.list_assets.return_value = [
        {
            **workspace.asset,
            "id": "audio-a",
            "asset_kind": "audio",
            "filename": "Recording.wav",
            "relative_path": "Recording.wav",
        },
        {
            **workspace.asset,
            "id": "video-a",
            "asset_kind": "video",
            "filename": "Footage.mp4",
            "relative_path": "Footage.mp4",
        },
        {
            **workspace.asset,
            "id": "image-a",
            "asset_kind": "image",
            "filename": "Image.png",
            "relative_path": "Image.png",
        },
    ]
    script = """
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui._render_scene_editor(workspace.case["id"], workspace.list_assets(workspace.case["id"]), [], lambda key: key)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(script).run()
        visual = next(
            item
            for item in app.selectbox
            if item.label == "Visual asset for original sound"
        )
        assert visual.options == ["No visual overlay", "Image.png"]


def test_scene_source_selection_reruns_outside_form_and_updates_roles_backgrounds():
    workspace = FakeWorkspace()
    workspace.list_assets.return_value = [
        {
            **workspace.asset,
            "id": "video-a",
            "asset_kind": "video",
            "relative_path": "Footage.mp4",
        },
        {
            **workspace.asset,
            "id": "audio-a",
            "asset_kind": "audio",
            "relative_path": "Recording.wav",
        },
        {
            **workspace.asset,
            "id": "image-a",
            "asset_kind": "image",
            "relative_path": "Image.png",
        },
    ]
    script = """
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui._render_scene_editor(workspace.case["id"], workspace.list_assets(workspace.case["id"]), [], lambda key: key)
"""
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = AppTest.from_string(script).run()
        source = next(
            item for item in app.selectbox if item.label == "Scene source asset"
        )
        assert source.proto.form_id == ""
        assert next(
            item for item in app.selectbox if item.label == "Scene role"
        ).options == ["broll", "original_sound"]
        source.select("audio-a").run()
        assert not list(app.exception)
        assert next(
            item for item in app.selectbox if item.label == "Scene role"
        ).options == ["original_sound"]
        visual = next(
            item
            for item in app.selectbox
            if item.label == "Visual asset for original sound"
        )
        assert len(visual.options) == 2
        button(app, "Save storyboard scene").click().run()
        assert not list(app.error)
        assert (
            app.session_state[ui._key("case-a", "draft_scenes")][0]["asset_id"]
            == "audio-a"
        )


def test_word_timing_form_can_submit_first_selection_and_validates_missing_json(
    tmp_path,
):
    import json

    workspace = FakeWorkspace()
    workspace.list_assets.return_value = [
        {**workspace.asset, "id": "audio-a", "asset_kind": "audio", "sha256": "a" * 64},
        {
            **workspace.asset,
            "id": "script-a",
            "asset_kind": "script",
            "sha256": "b" * 64,
        },
        {
            **workspace.asset,
            "id": "timing-a",
            "filename": "Word_Timestamps.json",
            "asset_kind": "transcript",
        },
    ]
    incoming = tmp_path / "Word_Timestamps.json"
    incoming.write_text(
        json.dumps({"words": [{"word": "Hello", "start": None, "end": None}]})
    )
    script = """
from webui import case_workspace as ui
workspace = ui.get_workspace(None)
ui._render_timing_import(workspace, workspace.case["id"], workspace.list_assets(workspace.case["id"]), lambda key: key)
"""
    with (
        patch.object(ui, "get_workspace", return_value=workspace),
        patch(
            "app.services.targeted_search.case_media.asset_content",
            return_value=incoming,
        ),
        patch(
            "app.services.targeted_search.case_media.import_whisperx",
            return_value={"unaligned_words": 1},
        ) as import_words,
    ):
        app = AppTest.from_string(script).run()
        assert not button(app, "Bind word timestamps").disabled
        button(app, "Bind word timestamps").click().run()
        assert app.error[0].value == "Word timestamps JSON required"
        import_words.assert_not_called()
        next(
            item for item in app.selectbox if item.label == "Aligned script asset"
        ).select("script-a")
        next(
            item
            for item in app.selectbox
            if item.label == "Imported word timestamps asset"
        ).select("timing-a")
        next(
            item
            for item in app.checkbox
            if item.label == "Confirm word timing asset binding"
        ).check()
        button(app, "Bind word timestamps").click().run()
        assert not list(app.error) and not list(app.exception)
        assert import_words.call_args.args[1] == "audio-a"
        assert import_words.call_args.args[2]["audio_sha256"] == "a" * 64
        assert import_words.call_args.args[2]["script_sha256"] == "b" * 64
        assert import_words.call_args.args[2]["words"][0]["start"] is None
        assert import_words.call_args.kwargs == {
            "scope": "narration",
            "script_asset_id": "script-a",
        }


@pytest.mark.parametrize(
    "source_kind,role,locator,visual_locator",
    [
        (
            "audio",
            "original_sound",
            {
                "kind": "time",
                "start_ms": 1000,
                "end_ms": 6000,
                "channel": 1,
                "speaker": "Unverified speaker label",
            },
            {"kind": "page", "page_index": 3, "bbox": [0.1, 0.2, 0.8, 0.9]},
        ),
        ("video", "broll", {"kind": "image", "bbox": [0.1, 0.2, 0.8, 0.9]}, None),
        (
            "document",
            "map",
            {"kind": "page", "page_index": 2, "bbox": [0.1, 0.2, 0.8, 0.9]},
            None,
        ),
        ("image", "map", {"kind": "image", "bbox": [0.1, 0.2, 0.8, 0.9]}, None),
    ],
)
def test_loaded_scene_edit_roundtrip_preserves_crop_channel_and_background_page(
    source_kind, role, locator, visual_locator
):
    workspace = FakeWorkspace()
    primary = {
        **workspace.asset,
        "id": "source-asset",
        "asset_kind": source_kind,
        "filename": "Original_Source",
        "relative_path": "Original_Source",
    }
    background = {
        **workspace.asset,
        "id": "background-asset",
        "filename": "Background.pdf",
        "relative_path": "Background.pdf",
    }
    workspace.list_assets.return_value = [primary, background]
    scene = {
        "scene_id": "source_scene",
        "asset_id": primary["id"],
        "role": role,
        "source_start_ms": 1000,
        "source_end_ms": 6000,
        "duration_ms": 5000,
        "volume": 3.0,
        "locator": locator,
        "asset_version_id": "server-version",
        "input_sha256": "server-hash",
    }
    if visual_locator:
        scene.update(
            visual_asset_id=background["id"],
            visual_locator=visual_locator,
            visual_asset_version_id="server-bg-version",
            visual_sha256="server-bg-hash",
        )
    workspace.list_storyboards.return_value = [
        {"id": "story-a", "title": "Existing edit", "scenes": [scene]}
    ]
    with patch.object(ui, "get_workspace", return_value=workspace):
        app = app_for("_render_production", workspace)
        app.selectbox(key=ui._key("case-a", "storyboard_id")).select("story-a").run()
        button(app, "Load storyboard for editing").click().run()
        app.selectbox(key=ui._key("case-a", "editing_scene")).select(0).run()
        button(app, "Save storyboard scene").click().run()
        assert not list(app.exception)
        assert not list(app.error)
        button(app, "Save storyboard").click().run()
        assert not list(app.exception)
        saved = workspace.save_storyboard.call_args.args[1]["scenes"][0]
        assert saved["locator"] == locator
        assert saved["role"] == role
        assert saved["volume"] == 3.0
        if visual_locator:
            assert saved["visual_locator"] == visual_locator
        assert "asset_version_id" not in saved and "input_sha256" not in saved
