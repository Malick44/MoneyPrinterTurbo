"""Documentary controls exercise evidence boundaries and revision-aware actions."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import ast
import json

import pytest
from streamlit.testing.v1 import AppTest

from webui import case_workspace
from webui import documentary_writer as ui


def button(app, label):
    return next(item for item in app.button if item.label == label)


def view(app, label):
    return next(
        item for item in app.radio if item.label == "Documentary desk view"
    ).set_value(label).run()


class FakeWorkspace:
    def __init__(self):
        self.repo = SimpleNamespace(root=Path("/synthetic-search-root"))
        self.case = {"id": "case-a", "name": "Owned documentary fixture"}
        self.list_claims = Mock(
            return_value=[
                {
                    "id": "claim-a",
                    "text": "The reviewed filing dates the hearing.",
                    "status": "reviewed",
                    "reviewed_by": "fixture-reviewer",
                    "assertion_class": "court_finding",
                    "citations": [{"unit_id": "unit-a", "relation": "supports"}],
                    "has_stale_citations": False,
                },
                {
                    "id": "claim-proposed",
                    "text": "Unreviewed allegation",
                    "status": "proposed",
                    "citations": [{"unit_id": "unit-a"}],
                },
                {
                    "id": "claim-stale",
                    "text": "An old reviewed passage",
                    "status": "reviewed",
                    "reviewed_by": "fixture-reviewer",
                    "assertion_class": "court_finding",
                    "citations": [{"unit_id": "unit-old", "relation": "supports"}],
                    "has_stale_citations": True,
                },
            ]
        )
        self.search_service = SimpleNamespace(list_jobs=Mock(return_value=[]))


class FakeWriter:
    def __init__(self, tmp_path):
        self.packet = {
            "claims": [
                {
                    "id": "claim-a",
                    "text": "The reviewed filing dates the hearing.",
                    "assertion_class": "court_finding",
                    "reviewed_by": "fixture-reviewer",
                    "citation_ids": ["dcite_page"],
                }
            ],
            "citations": [
                {
                    "id": "dcite_page",
                    "asset_id": "asset-a",
                    "filename": "Original_Filing.pdf",
                    "asset_version_id": "version-a",
                    "unit_id": "unit-a",
                    "locator": {"kind": "page", "page_index": 2, "page_label": "A-3"},
                    "quote": "The hearing was held on the stated date.",
                    "text": "The hearing was held on the stated date.",
                },
                {
                    "id": "dcite_time",
                    "asset_id": "audio-a",
                    "filename": "Original_Audio.wav",
                    "locator": {"kind": "time", "start_ms": 1250, "end_ms": 4567},
                    "quote": "This is the original recording.",
                },
            ],
            "gaps": ["Locate exterior courthouse footage."],
            "packet_hash": "a" * 64,
        }
        self.document = {
            "id": "document-a",
            "title": "Owned documentary fixture",
            "revision": 1,
            "status": "draft",
            "stale": False,
            "options": {
                "title": "Owned documentary fixture",
                "target_minutes": 25,
                "language": "English",
                "claim_ids": ["claim-a"],
                "instructions": "",
                "stage": "draft",
            },
            "packet": deepcopy(self.packet),
            "outline": None,
            "draft": {
                "chapters": [
                    {
                        "chapter_id": "chapter-1",
                        "title": "The hearing",
                        "scenes": [
                            {
                                "scene_id": "scene-1",
                                "title": "The filing",
                                "passages": [
                                    {
                                        "passage_id": "passage-1",
                                        "text": "The filing records when the hearing took place. [dcite_page]",
                                        "claim_ids": ["claim-a"],
                                        "citation_ids": ["dcite_page"],
                                        "quotes": [
                                            {
                                                "citation_id": "dcite_page",
                                                "text": self.packet["citations"][0][
                                                    "quote"
                                                ],
                                            }
                                        ],
                                    }
                                ],
                                "footage_queries": ["courthouse exterior"],
                                "evidence_gaps": [],
                            }
                        ],
                    }
                ]
            },
            "factual_review": {
                "passages": [
                    {
                        "passage_id": "passage-1",
                        "status": "supported",
                        "reason": "Retained filing supports the narration.",
                        "citation_ids": ["dcite_page"],
                    }
                ],
                "notes": [],
            },
            "human_review": None,
        }
        self.path = tmp_path / "Final_Script.md"
        self.path.write_text("The filing records when the hearing took place.\n")
        self.build_packet = Mock(
            side_effect=lambda *_args, **_kwargs: deepcopy(self.packet)
        )
        self.enqueue = Mock(return_value={"id": "writing-job-a"})
        self.list_documents = Mock(side_effect=lambda *_args: [deepcopy(self.document)])
        self.get_document = Mock(side_effect=lambda *_args: deepcopy(self.document))
        self.save_revision = Mock(side_effect=self._save_revision)
        self.review = Mock(side_effect=self._review)
        self.export = Mock(side_effect=self._export)
        self.export_content = Mock(return_value=self.path)

    def _save_revision(self, _case_id, _document_id, draft, expected_revision=None):
        assert expected_revision == self.document["revision"]
        self.document.update(
            draft=deepcopy(draft),
            revision=self.document["revision"] + 1,
            human_review=None,
            factual_review=None,
        )
        return deepcopy(self.document)

    def _review(self, _case_id, _document_id, **values):
        assert values["expected_revision"] == self.document["revision"]
        self.document["human_review"] = {
            "approved": values["approved"],
            "reviewed_by": values["reviewed_by"],
            "notes": values["notes"],
        }
        return deepcopy(self.document)

    def _export(self, _case_id, document_id, final=False, expected_revision=None):
        assert expected_revision == self.document["revision"]
        return {
            "document_id": document_id,
            "revision": expected_revision,
            "final": final,
            "files": [{"filename": "Final_Script.md" if final else "Cited_Draft.md"}],
            "script_asset_id": "script-a" if final else None,
        }


APP = """
from webui import case_workspace
from webui import documentary_writer as ui
workspace = case_workspace.get_workspace(None)
ui.render_documentary_writer(workspace, workspace.case, lambda key: key)
"""


def test_empty_reviewed_evidence_blocks_generation(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    workspace.list_claims.return_value = []
    writer.list_documents.return_value = []
    writer.list_documents.side_effect = None
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert button(app, "Generate documentary outline").disabled
        assert button(app, "Preview documentary evidence").disabled
        writer.enqueue.assert_not_called()
        writer.build_packet.assert_not_called()


def test_reviewed_claim_selection_preview_and_queue_preserve_exact_evidence(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
        patch("app.services.targeted_search.worker.ensure_worker_running") as worker,
    ):
        app = AppTest.from_string(APP).run()
        button(app, "New documentary").click().run()
        claims = app.multiselect(key=ui._key("case-a", "claim_ids"))
        assert claims.options == ["The reviewed filing dates the hearing."]
        button(app, "Preview documentary evidence").click().run()
        assert not list(app.exception)
        writer.build_packet.assert_called_with("case-a", claim_ids=["claim-a"])
        captions = [item.value for item in app.caption]
        assert any("Original_Filing.pdf · p. A-3" in value for value in captions)
        assert any("Original_Audio.wav · 1.250–4.567s" in value for value in captions)
        assert writer.packet["citations"][0]["quote"] in [
            item.value for item in app.text
        ]
        app.text_input(key=ui._key("case-a", "language")).set_value("French")
        app.number_input(key=ui._key("case-a", "minutes")).set_value(28)
        button(app, "Generate documentary outline").click().run()
        assert not list(app.exception)
        options = writer.enqueue.call_args.args[1]
        assert options["stage"] == "outline"
        assert options["language"] == "French"
        assert options["target_minutes"] == 28
        assert options["claim_ids"] == ["claim-a"]
        worker.assert_called_once_with(root_dir=workspace.repo.root)


def test_new_documentary_defaults_to_25_minutes_with_22_to_28_bounds(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.list_documents.side_effect = None
    writer.list_documents.return_value = []
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        minutes = app.number_input(key=ui._key("case-a", "minutes"))
        assert minutes.value == 25
        assert minutes.proto.min == 22
        assert minutes.proto.max == 28
        writer.enqueue.assert_not_called()


def test_legacy_creation_preferences_and_widget_values_fall_back_to_25(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    original = deepcopy(writer.document)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        app.session_state[ui._key("case-a", "saved_options")] = {"target_minutes": 12}
        app.session_state[ui._key("case-a", "minutes")] = 12
        button(app, "New documentary").click().run()
        assert not list(app.exception)
        assert app.number_input(key=ui._key("case-a", "minutes")).value == 25
        assert writer.document == original
        writer.enqueue.assert_not_called()
        writer.save_revision.assert_not_called()


def test_continuing_legacy_outline_submits_25_without_rewriting_saved_document(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document.update(
        outline={
            "chapters": [
                {
                    "chapter_id": "chapter-1",
                    "title": "The hearing",
                    "scenes": [{"scene_id": "scene-1", "title": "The filing"}],
                }
            ]
        },
        draft=None,
        factual_review=None,
        status="outline_ready",
    )
    writer.document["options"]["target_minutes"] = 2
    original = deepcopy(writer.document)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
        patch("app.services.targeted_search.worker.ensure_worker_running"),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert writer.document == original
        button(app, "Write cited documentary draft").click().run()
        assert not list(app.exception)
        options = writer.enqueue.call_args.args[1]
        assert options["target_minutes"] == 25
        assert options["document_id"] == "document-a"
        assert options["stage"] == "draft"
        assert writer.document == original
        writer.save_revision.assert_not_called()


def test_reviewed_editorial_unclassified_and_counterevidence_only_claims_are_not_selected(
    tmp_path,
):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    eligible = deepcopy(workspace.list_claims.return_value[0])
    excluded = [
        {
            **deepcopy(eligible),
            "id": "claim-unclassified",
            "text": "An unclassified reviewed statement.",
            "assertion_class": "unclassified",
        },
        {
            **deepcopy(eligible),
            "id": "claim-editorial",
            "text": "A reviewed editorial note.",
            "assertion_class": "editorial",
        },
        {
            **deepcopy(eligible),
            "id": "claim-opposing",
            "text": "A reviewed statement with only opposing evidence.",
            "citations": [{"unit_id": "unit-a", "relation": "contradicts"}],
        },
        {
            **deepcopy(eligible),
            "id": "claim-mentioned",
            "text": "A reviewed statement with only a passing mention.",
            "citations": [{"unit_id": "unit-a", "relation": "mentions"}],
        },
    ]
    workspace.list_claims.return_value = [eligible, *excluded]
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        app.session_state[ui._key("case-a", "saved_options")] = {
            "claim_ids": [row["id"] for row in workspace.list_claims.return_value],
        }
        button(app, "New documentary").click().run()
        assert not list(app.exception)
        picker = app.multiselect(key=ui._key("case-a", "claim_ids"))
        assert picker.options == [eligible["text"]]
        assert picker.value == ["claim-a"]
        assert not button(app, "Generate documentary outline").disabled
        assert any(
            item.value == "Documentary reviewed claims need evidence"
            for item in app.warning
        )
        assert {row["text"] for row in excluded} <= {
            item.value for item in app.markdown
        }
        writer.enqueue.assert_not_called()
        writer.build_packet.assert_not_called()
        button(app, "Open documentary evidence review").click().run()
        assert not list(app.exception)
        assert app.session_state["case_workspace_pending_case"] == "case-a"
        assert app.session_state["case_workspace_pending_view"] == "Timeline / Claims"
        writer.enqueue.assert_not_called()
        writer.build_packet.assert_not_called()


def test_reviewed_but_ineligible_evidence_disables_generation(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    workspace.list_claims.return_value = [
        {
            **deepcopy(workspace.list_claims.return_value[0]),
            "assertion_class": "unclassified",
            "citations": [{"unit_id": "unit-a", "relation": "contradicts"}],
        }
    ]
    writer.list_documents.side_effect = None
    writer.list_documents.return_value = []
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert app.multiselect(key=ui._key("case-a", "claim_ids")).options == []
        assert button(app, "Generate documentary outline").disabled
        assert button(app, "Preview documentary evidence").disabled
        assert sum(
            item.label == "Open documentary evidence review" for item in app.button
        ) == 1
        writer.enqueue.assert_not_called()
        writer.build_packet.assert_not_called()


def test_cited_and_spoken_previews_keep_source_quotes_and_footage_explicit(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert [tab.label for tab in app.tabs] == [
            "Documentary spoken narration",
            "Documentary cited narration",
        ]
        spoken = app.tabs[0]
        assert [item.value for item in spoken.markdown] == [
            "The filing records when the hearing took place."
        ]
        assert not list(spoken.text)
        assert writer.packet["citations"][0]["quote"] in [
            item.value for item in app.tabs[1].text
        ]
        assert "courthouse exterior" in [item.value for item in app.tabs[1].text]
        assert all("dcite_" not in item.value for item in app.tabs[1].caption)


def test_narration_stats_count_only_cleaned_passage_bodies(tmp_path):
    writer = FakeWriter(tmp_path)
    chapter = writer.document["draft"]["chapters"][0]
    chapter["title"] = "Chapter title is not spoken narration"
    scene = chapter["scenes"][0]
    scene["title"] = "Scene title is not spoken narration"
    scene["passages"][0]["text"] = (
        "The court's record is clear. [dcite_a]\nStill supported."
    )
    scene["passages"][0]["quotes"] = [{"text": "Unspoken source excerpt " * 100}]
    scene["passages"].append({"text": "A second passage [dcite_b-2]."})
    chapter["scenes"].append({"passages": [{"text": "One last word. [dcite_c]"}]})
    original = deepcopy(writer.document)
    words, minutes = ui._narration_stats(writer.document)
    assert words == 13
    assert minutes == words / ui.DOCUMENTARY_PLANNING_WORDS_PER_MINUTE
    assert writer.document == original


@pytest.mark.parametrize("target_minutes", [25, 2])
def test_short_saved_draft_warns_from_spoken_length_without_changing_approval(
    tmp_path, target_minutes
):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document["options"]["target_minutes"] = target_minutes
    writer.document["draft"]["chapters"][0]["scenes"][0]["passages"][0]["text"] = (
        "court " * 295 + "[dcite_page]"
    )
    writer.document["human_review"] = {"approved": True, "reviewed_by": "reviewer-a"}
    original = deepcopy(writer.document)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        metrics = {item.label: item.value for item in app.metric}
        assert metrics["Documentary spoken word count"] == "295"
        assert metrics["Documentary estimated narration minutes"] == "2.0"
        assert "Documentary narration estimate help" in [
            item.value for item in app.caption
        ]
        assert "Documentary narration estimate outside band" in [
            item.value for item in app.warning
        ]
        view(app, "Documentary export view")
        assert not button(app, "Export documentary final script").disabled
        assert writer.document == original
        writer.enqueue.assert_not_called()
        writer.save_revision.assert_not_called()
        writer.review.assert_not_called()
        writer.export.assert_not_called()


@pytest.mark.parametrize("language", ["Chinese", "French", None])
def test_non_english_or_unspecified_legacy_language_has_no_time_estimate(
    tmp_path, language
):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    if language is None:
        writer.document["options"].pop("language")
    else:
        writer.document["options"]["language"] = language
    original = deepcopy(writer.document)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        metrics = {item.label: item.value for item in app.metric}
        assert metrics["Documentary spoken word count"] == "8"
        assert (
            metrics["Documentary estimated narration minutes"]
            == "Documentary estimate unavailable"
        )
        assert "Documentary narration estimate unavailable help" in [
            item.value for item in app.caption
        ]
        assert "Documentary narration estimate outside band" not in [
            item.value for item in app.warning
        ]
        assert writer.document == original


@pytest.mark.parametrize(
    ("words", "warns"), [(3190, False), (4060, False), (3189, True), (4061, True)]
)
def test_english_narration_estimate_uses_inclusive_shared_duration_band(
    tmp_path, words, warns
):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document["options"]["language"] = "en-US"
    writer.document["draft"]["chapters"][0]["scenes"][0]["passages"][0]["text"] = (
        "court " * words
    )
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert any(
            item.value == "Documentary narration estimate outside band"
            for item in app.warning
        ) == warns


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_narration_stats_translate_units_target_band_and_precise_warning(
    tmp_path, locale
):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document["draft"]["chapters"][0]["scenes"][0]["passages"][0]["text"] = (
        "court " * 3189
    )
    translations = json.loads(Path(f"webui/i18n/{locale}.json").read_text())["Translation"]
    translated_app = f"""
import json
from pathlib import Path
from webui import case_workspace
from webui import documentary_writer as ui
translations = json.loads(Path('webui/i18n/{locale}.json').read_text())['Translation']
workspace = case_workspace.get_workspace(None)
ui.render_documentary_writer(workspace, workspace.case, lambda key: translations.get(key, key))
"""
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(translated_app).run()
        assert not list(app.exception)
        metric = next(
            item
            for item in app.metric
            if item.label == translations["Documentary estimated narration minutes"]
        )
        assert metric.value == "22.0"
        band = {
            "min_minutes": ui.DOCUMENTARY_MIN_MINUTES,
            "max_minutes": ui.DOCUMENTARY_MAX_MINUTES,
            "words_per_minute": ui.DOCUMENTARY_PLANNING_WORDS_PER_MINUTE,
        }
        assert translations["Documentary narration estimate help"].format(**band) in [
            item.value for item in app.caption
        ]
        warning = translations["Documentary narration estimate outside band"].format(
            minutes=3189 / ui.DOCUMENTARY_PLANNING_WORDS_PER_MINUTE, **band
        )
        assert "21.99" in warning
        assert warning in [item.value for item in app.warning]


def test_edit_revision_preserves_structured_source_refs_and_clears_approval(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document["human_review"] = {"approved": True, "reviewed_by": "reviewer-a"}
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        view(app, "Documentary edit view")
        narration = next(
            item
            for item in app.text_area
            if item.label == "Documentary narration passage"
        )
        narration.set_value("The retained filing gives the hearing date.")
        next(
            item
            for item in app.text_area
            if item.label == "Documentary footage search queries"
        ).set_value("courthouse exterior\ncourtroom empty")
        button(app, "Save documentary revision").click().run()
        assert not list(app.exception)
        assert not list(app.error)
        saved = writer.save_revision.call_args.args[2]
        scene = saved["chapters"][0]["scenes"][0]
        assert (
            scene["passages"][0]["text"]
            == "The retained filing gives the hearing date."
        )
        assert scene["passages"][0]["claim_ids"] == ["claim-a"]
        assert scene["passages"][0]["citation_ids"] == ["dcite_page"]
        assert (
            scene["passages"][0]["quotes"][0]["text"]
            == writer.packet["citations"][0]["quote"]
        )
        assert scene["footage_queries"] == ["courthouse exterior", "courtroom empty"]
        assert writer.save_revision.call_args.kwargs == {"expected_revision": 1}
        view(app, "Documentary review view")
        assert button(app, "Approve documentary final script").disabled
        view(app, "Documentary export view")
        assert button(app, "Export documentary final script").disabled


def test_human_rejection_keeps_final_export_disabled_and_review_pins_revision(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        view(app, "Documentary review view")
        next(
            item for item in app.text_input if item.label == "Documentary reviewer"
        ).set_value("reviewer-a")
        next(
            item for item in app.text_area if item.label == "Documentary review notes"
        ).set_value("Clarify attribution.")
        button(app, "Request documentary revision").click().run()
        assert not list(app.exception)
        assert app.radio[0].value == "Documentary edit view"
        assert writer.review.call_args.args == ("case-a", "document-a")
        assert writer.review.call_args.kwargs == {
            "reviewed_by": "reviewer-a",
            "notes": "Clarify attribution.",
            "approved": False,
            "expected_revision": 1,
        }
        view(app, "Documentary export view")
        assert button(app, "Export documentary final script").disabled
        writer.export.assert_not_called()


def test_unsupported_review_disables_human_approval(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document["factual_review"]["passages"][0]["status"] = "insufficient"
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        view(app, "Documentary review view")
        assert button(app, "Approve documentary final script").disabled
        assert not button(app, "Request documentary revision").disabled


def test_stale_or_revoked_document_withholds_narration_and_export_controls(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document = {
        "id": "document-a",
        "title": "Withheld fixture",
        "revision": 1,
        "status": "draft",
        "stale": True,
        "content_withheld": True,
        "guard_error": "Source permission revoked",
    }
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert [item.value for item in app.error] == ["Source permission revoked"]
        assert not list(app.metric)
        assert not list(app.tabs)
        assert not any("Export documentary" in item.label for item in app.button)
        writer.export.assert_not_called()


def test_final_download_and_handoff_reauthorize_content_and_pin_revision(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document["human_review"] = {"approved": True, "reviewed_by": "reviewer-a"}
    script = (
        APP
        + """
import streamlit as st
st.text(st.session_state.get('case_workspace_pending_view', ''))
"""
    )
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(script).run()
        view(app, "Documentary export view")
        button(app, "Export documentary final script").click().run()
        assert not list(app.exception)
        assert writer.export.call_args.kwargs == {"final": True, "expected_revision": 1}
        assert len(app.get("download_button")) == 1
        writer.export_content.assert_called_with(
            "case-a", "document-a", "Final_Script.md", final=True, expected_revision=1
        )
        button(app, "Send documentary script to Production").click().run()
        assert not list(app.exception)
        assert app.session_state["case_case-a_script_asset"] == "script-a"
        assert (
            app.session_state["case_case-a_storyboard_script"]
            == writer.path.read_text()
        )
        assert app.session_state["case_workspace_pending_view"] == "Production"
        assert "case_case-b_script_asset" not in app.session_state


def test_download_authorization_failure_reads_no_content_and_blocks_handoff(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document["human_review"] = {"approved": True, "reviewed_by": "reviewer-a"}
    writer.export_content.side_effect = ValueError("Export permission revoked")
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        view(app, "Documentary export view")
        button(app, "Export documentary final script").click().run()
        assert not list(app.exception)
        assert not list(app.get("download_button"))
        button(app, "Send documentary script to Production").click().run()
        assert any(item.value == "Export permission revoked" for item in app.error)
        assert "case_case-a_script_asset" not in app.session_state


def test_case_keys_isolate_options_exports_and_jobs(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    workspace.search_service.list_jobs.return_value = [
        {
            "id": "job-a",
            "job_type": "case_documentary",
            "status": "queued",
            "payload": {"case_id": "case-a"},
        },
        {
            "id": "job-b",
            "job_type": "case_documentary",
            "status": "failed",
            "payload": {"case_id": "case-b"},
            "last_error": "Other case private error",
        },
    ]
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        button(app, "New documentary").click().run()
        app.text_input(key=ui._key("case-a", "language")).set_value("French").run()
        assert any("job-a" in item.value for item in app.caption)
        assert not list(app.error)
        workspace.case = {"id": "case-b", "name": "Another owned case"}
        app.run()
        assert not list(app.exception)
        button(app, "New documentary").click().run()
        assert app.text_input(key=ui._key("case-b", "language")).value == "English"
        assert all("job-a" not in item.value for item in app.caption)
        workspace.case = {"id": "case-a", "name": "Owned documentary fixture"}
        app.run()
        assert app.text_input(key=ui._key("case-a", "language")).value == "French"


def test_saved_script_keeps_an_unrelated_outline_failure_in_case_history(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    failed_outline = {
        "id": "old-outline-job",
        "job_type": "case_documentary",
        "status": "failed",
        "payload": {"case_id": "case-a", "options": {"stage": "outline"}},
        "last_error": "Configured text provider is unavailable",
    }
    workspace.search_service.list_jobs.return_value = [
        deepcopy(failed_outline),
        {
            "id": "private-other-case-job",
            "job_type": "case_documentary",
            "status": "failed",
            "payload": {"case_id": "case-b"},
            "last_error": "Other case private error",
        },
    ]
    original = deepcopy(writer.document)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert not list(app.error)
        assert not any(
            item.value.startswith("Documentary generation job:") for item in app.caption
        )
        history = next(
            item for item in app.expander if item.label == "Documentary other case jobs"
        )
        assert not history.proto.expanded
        assert "Documentary other case jobs help" in [
            item.value for item in history.caption
        ]
        assert failed_outline["last_error"] in [item.value for item in history.text]
        assert all("Other case private error" != item.value for item in app.text)
        assert button(app, "Review documentary for approval")
        assert writer.document == original
        assert workspace.search_service.list_jobs.return_value[0] == failed_outline
        writer.enqueue.assert_not_called()


def test_current_script_failure_is_visible_while_older_version_jobs_are_history(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    workspace.search_service.list_jobs.return_value = [
        {
            "id": "unrelated-running-job",
            "job_type": "case_documentary",
            "status": "running",
            "payload": {
                "case_id": "case-a",
                "options": {"document_id": "another-document"},
                "document_revision": 1,
            },
        },
        {
            "id": "earlier-version-job",
            "job_type": "case_documentary",
            "status": "failed",
            "payload": {
                "case_id": "case-a",
                "options": {"document_id": "document-a"},
                "document_revision": 0,
            },
            "last_error": "Earlier version failure",
        },
        {
            "id": "current-failed-job",
            "job_type": "case_documentary",
            "status": "failed",
            "payload": {
                "case_id": "case-a",
                "options": {"document_id": "document-a"},
                "document_revision": 1,
            },
            "last_error": "Current version failure",
        },
    ]
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert [item.value for item in app.error] == ["Current version failure"]
        assert [
            item.value
            for item in app.caption
            if item.value.startswith("Documentary generation job:")
        ] == ["Documentary generation job: Needs attention"]
        history = next(
            item for item in app.expander if item.label == "Documentary other case jobs"
        )
        assert "Earlier version failure" in [item.value for item in history.text]
        assert "Current version failure" not in [item.value for item in history.text]


def test_completed_worker_result_matches_saved_revision_without_old_retry_error(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    completed = {
        "id": "completed-writing-job",
        "job_type": "case_documentary",
        "status": "complete",
        "payload": {
            "case_id": "case-a",
            "options": {"document_id": "document-a"},
            "document_revision": 0,
        },
        "result": {"id": "document-a", "revision": 1},
        "last_error": "Earlier retry failure",
    }
    workspace.search_service.list_jobs.return_value = [completed]
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert not list(app.error)
        assert "Documentary generation job: Complete" in [
            item.value for item in app.caption
        ]
        assert not any(
            item.label == "Documentary other case jobs" for item in app.expander
        )
        details = next(
            item for item in app.expander if item.label == "Documentary job details"
        )
        assert completed["last_error"] in [item.value for item in details.text]
        writer.document["revision"] = 2
        app.run()
        assert not list(app.exception)
        assert not any(
            item.value.startswith("Documentary generation job:") for item in app.caption
        )
        assert any(item.label == "Documentary other case jobs" for item in app.expander)


@pytest.mark.parametrize(
    "job",
    [
        {
            "status": "complete",
            "payload": {"options": {"stage": "outline"}},
            "result": {"id": "document-a", "revision": 1},
        },
        {
            "status": "complete",
            "result": {"document_id": "document-a", "revision": 1},
        },
        {
            "status": "queued",
            "payload": {"options": {"document_id": "document-a"}},
        },
    ],
)
def test_job_document_matching_supports_outline_results_and_legacy_job_shapes(job):
    assert ui._job_matches_document(job, {"id": "document-a", "revision": 1})
    assert not ui._job_matches_document(job, {"id": "document-b", "revision": 1})


def test_completed_job_stays_visible_before_and_after_refreshing_its_saved_revision():
    job = {
        "status": "complete",
        "payload": {
            "options": {"document_id": "document-a"},
            "document_revision": 1,
        },
        "result": {"id": "document-a", "revision": 2},
    }
    assert ui._job_matches_document(job, {"id": "document-a", "revision": 1})
    assert ui._job_matches_document(job, {"id": "document-a", "revision": 2})
    assert not ui._job_matches_document(job, {"id": "document-a", "revision": 3})


def test_saved_script_opens_clean_narration_without_other_work_forms(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert app.radio[0].value == "Documentary read view"
        assert not list(app.text_input)
        assert not list(app.text_area)
        assert not any("Export documentary" in item.label for item in app.button)
        primary = [item for item in app.button if item.proto.type == "primary"]
        assert [item.label for item in primary] == ["Review documentary for approval"]
        assert "dcite_page" not in app.tabs[0].markdown[0].value
        button(app, "Review documentary for approval").click().run()
        assert app.radio[0].value == "Documentary review view"
        assert not list(app.tabs)
        assert not button(app, "Approve documentary final script").disabled
        writer.review.assert_not_called()
        writer.export.assert_not_called()


def test_translated_script_and_desk_selectors_rerun_without_session_context(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    script = """
import streamlit as st
from webui import case_workspace
from webui import documentary_writer as ui
st.session_state.setdefault('documentary_test_locale', 'en')
labels = {
    'en': {
        'Saved documentary': 'Saved scripts',
        'Documentary desk view': 'Work on this script',
        'Documentary read view': 'Read',
        'Documentary edit view': 'Edit',
        'Documentary review view': 'Review',
        'Documentary export view': 'Export',
    }
}
def tr(key):
    return labels[st.session_state['documentary_test_locale']].get(key, key)
workspace = case_workspace.get_workspace(None)
ui.render_documentary_writer(workspace, workspace.case, tr)
"""
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(script).run()
        assert not list(app.exception)
        selector = app.selectbox(key=ui._key("case-a", "selected_document"))
        assert selector.options == ["Owned documentary fixture · Draft"]
        selector.set_value("document-a").run()
        desk = app.radio(key=ui._view_key("case-a", writer.document))
        assert desk.options == ["Read", "Edit", "Review", "Export"]
        desk.set_value("Documentary review view").run()
        assert not list(app.exception)
        assert not button(app, "Approve documentary final script").disabled
        app.radio(key=ui._view_key("case-a", writer.document)).set_value(
            "Documentary read view"
        ).run()
        assert not list(app.exception)
        assert app.tabs[0].markdown[0].value == "The filing records when the hearing took place."


def test_new_script_is_a_separate_view_and_return_preserves_saved_script(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    original = deepcopy(writer.document)
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        button(app, "New documentary").click().run()
        assert app.text_input(key=ui._key("case-a", "title")).value == workspace.case["name"]
        assert not list(app.tabs)
        assert not list(app.radio)
        button(app, "Back to documentary drafts").click().run()
        assert not list(app.exception)
        assert app.radio[0].value == "Documentary read view"
        assert not list(app.text_input)
        assert writer.document == original
        writer.enqueue.assert_not_called()
        writer.save_revision.assert_not_called()


def test_outline_next_action_writes_existing_project_draft(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    writer.document.update(
        outline={
            "chapters": [
                {
                    "chapter_id": "chapter-1",
                    "title": "The hearing",
                    "scenes": [{"scene_id": "scene-1", "title": "The filing"}],
                }
            ]
        },
        draft=None,
        factual_review=None,
        status="outline_ready",
    )
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
        patch("app.services.targeted_search.worker.ensure_worker_running") as worker,
    ):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert button(app, "Write cited documentary draft").proto.type == "primary"
        assert not list(app.text_input)
        button(app, "Write cited documentary draft").click().run()
        assert writer.enqueue.call_args.args[1] == {
            **writer.document["options"],
            "stage": "draft",
            "document_id": "document-a",
        }
        worker.assert_called_once_with(root_dir=workspace.repo.root)


def test_missing_evidence_blocker_routes_to_this_cases_review(tmp_path):
    workspace, writer = FakeWorkspace(), FakeWriter(tmp_path)
    workspace.list_claims.return_value = []
    writer.list_documents.side_effect = None
    writer.list_documents.return_value = []
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(APP).run()
        button(app, "Open documentary evidence review").click().run()
        assert not list(app.exception)
        assert app.session_state["case_workspace_pending_case"] == "case-a"
        assert app.session_state["case_workspace_pending_view"] == "Timeline / Claims"
        writer.enqueue.assert_not_called()


def test_real_documentary_review_export_and_revision_keep_spoken_script_and_evidence_separate(
    tmp_path,
):
    from dataclasses import replace

    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    from app.services.targeted_search.case_media import index_asset
    from app.services.targeted_search.case_workspace import CaseWorkspace
    from app.services.targeted_search.documentary import DocumentaryWriter
    from app.services.targeted_search.service import SearchService

    service = SearchService(tmp_path / "library")
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Owned documentary UI integration fixture")
    incoming = service.repo.root / "owned" / "documentary-ui"
    incoming.mkdir(parents=True)
    pdf = PdfWriter()
    page = pdf.add_blank_page(width=612, height=792)
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {
                    NameObject("/F1"): DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    ),
                }
            ),
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 72 720 Td (The court found the hearing occurred on Monday.) Tj ET"
    )
    page[NameObject("/Contents")] = pdf._add_object(stream)
    with (incoming / "Original_Opinion.pdf").open("wb") as handle:
        pdf.write(handle)
    asset = workspace.import_folder(case["id"], incoming)["assets"][0]
    service.set_policy(
        asset["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Authored PDF evidence",
        "fixture-reviewer",
    )
    index_asset(workspace, asset["id"])
    with workspace.repo.connect() as connection:
        unit_id = connection.execute(
            "SELECT id FROM evidence_units WHERE asset_id=? AND unit_kind='document_passage' AND is_active=1",
            (asset["id"],),
        ).fetchone()["id"]
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "The court found the hearing occurred on Monday.",
            "status": "reviewed",
            "assertion_class": "court_finding",
            "reviewed_by": "fixture-reviewer",
            "citations": [
                {"unit_id": unit_id, "quote": "the hearing occurred on Monday"}
            ],
        },
    )
    writer = DocumentaryWriter(workspace)
    citation_id = writer.build_packet(case["id"])["citations"][0]["id"]
    outline = {
        "chapters": [
            {
                "chapter_id": "chapter-1",
                "title": "The finding",
                "scenes": [
                    {
                        "scene_id": "scene-1",
                        "title": "The record",
                        "purpose": "Attribute the finding to the court.",
                        "claim_ids": [claim["id"]],
                        "citation_ids": [citation_id],
                        "footage_queries": ["courthouse exterior"],
                        "evidence_gaps": [],
                    }
                ],
            }
        ]
    }
    text = f'The court found that "the hearing occurred on Monday". [{citation_id}]'
    draft = {
        "chapters": [
            {
                "chapter_id": "chapter-1",
                "title": "The finding",
                "scenes": [
                    {
                        "scene_id": "scene-1",
                        "title": "The record",
                        "passages": [
                            {
                                "passage_id": "passage-1",
                                "text": text,
                                "claim_ids": [claim["id"]],
                                "citation_ids": [citation_id],
                                "quotes": [
                                    {
                                        "citation_id": citation_id,
                                        "text": "the hearing occurred on Monday",
                                    }
                                ],
                            }
                        ],
                        "footage_queries": ["courthouse exterior"],
                        "evidence_gaps": [],
                    }
                ],
            }
        ]
    }
    assessment = {
        "passages": [
            {
                "passage_id": "passage-1",
                "status": "supported",
                "reason": "The court finding is explicitly attributed.",
                "citation_ids": [citation_id],
            }
        ],
        "notes": [],
    }
    responses = {"outline": outline, "draft": draft, "factual_review": assessment}
    writer.response_generator = lambda prompt: json.dumps(
        responses[json.loads(prompt.split("\n", 1)[1])["stage"]]
    )
    options = {"title": "The finding", "target_minutes": 25, "claim_ids": [claim["id"]]}
    document = writer.generate(case["id"], options)
    document = writer.generate(
        case["id"], {**options, "document_id": document["id"], "stage": "draft"}
    )
    document = writer.generate(
        case["id"],
        {**options, "document_id": document["id"], "stage": "factual_review"},
    )
    real_app = f"""
from webui import case_workspace
from webui import documentary_writer as ui
workspace = case_workspace.get_workspace(None)
ui.render_documentary_writer(workspace, workspace.get_case({case["id"]!r}), lambda key: key)
"""
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_writer", return_value=writer),
    ):
        app = AppTest.from_string(real_app).run()
        assert not list(app.exception)
        assert not list(app.error)
        spoken_text = app.tabs[0].markdown[0].value
        assert spoken_text == 'The court found that "the hearing occurred on Monday".'
        assert citation_id not in spoken_text
        view(app, "Documentary review view")
        next(
            item for item in app.text_input if item.label == "Documentary reviewer"
        ).set_value("human-editor")
        button(app, "Approve documentary final script").click().run()
        assert not list(app.exception)
        assert not list(app.error)
        approved = writer.get_document(case["id"], document["id"])
        assert approved["revision"] == 4
        assert approved["human_review"]["approved"]
        view(app, "Documentary export view")
        button(app, "Export documentary final script").click().run()
        assert not list(app.exception)
        assert not list(app.error)
        assert len(app.get("download_button")) == 5
        button(app, "Send documentary script to Production").click().run()
        assert (
            app.session_state[f"case_{case['id']}_storyboard_script"].strip()
            == spoken_text
        )
        exported_script = workspace.get_asset(
            app.session_state[f"case_{case['id']}_script_asset"]
        )
        assert exported_script["rights_status"] == "unknown"
        view(app, "Documentary edit view")
        next(
            item
            for item in app.text_area
            if item.label == "Documentary narration passage"
        ).set_value(text)
        button(app, "Save documentary revision").click().run()
        assert not list(app.exception)
        assert not list(app.error)
        assert writer.get_document(case["id"], document["id"])["revision"] == 5
        view(app, "Documentary export view")
        assert button(app, "Export documentary final script").disabled
        assert not list(app.get("download_button"))


def test_documentary_view_keeps_footage_first_and_translation_keys_complete():
    assert case_workspace.VIEWS[0] == "Footage Search"
    assert "Documentary Writer" in case_workspace.VIEWS
    tree = ast.parse(Path(ui.__file__).read_text())
    used = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "tr"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }
    assert used <= ui.DOCUMENTARY_TRANSLATION_KEYS
    for locale in ("en", "zh"):
        translations = json.loads(
            (Path("webui/i18n") / (locale + ".json")).read_text()
        )["Translation"]
        assert ui.DOCUMENTARY_TRANSLATION_KEYS <= translations.keys()
