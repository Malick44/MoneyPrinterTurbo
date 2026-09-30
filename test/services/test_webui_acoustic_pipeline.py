"""Sound controls keep editorial assets, verified words and revisions separate."""

import ast
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import wave

import pytest
from streamlit.testing.v1 import AppTest

from app.models.acoustic import AcousticPlanEdit
from webui import acoustic_pipeline as ui
from webui import case_workspace


def button(app, label):
    return next(item for item in app.button if item.label == label)


class FakeWorkspace:
    def __init__(self):
        self.repo = SimpleNamespace(root=Path("/synthetic-acoustic-root"))
        self.case = {"id": "case-a", "name": "Owned narration fixture"}
        base = {
            "case_id": "case-a",
            "asset_kind": "audio",
            "artifact_id": "original-a",
            "metadata": {},
        }
        self.narration = {
            **base,
            "id": "narration-a",
            "filename": "Narration.wav",
            "relative_path": "05_Production/Narration.wav",
        }
        self.effect = {
            **base,
            "id": "effect-a",
            "filename": "Impact.wav",
            "relative_path": "05_Production/Sounds/Impact.wav",
            "metadata": {
                "role": "sound_effect",
                "sound_asset": {"category": "impact", "tags": ["impact", "reveal"]},
            },
        }
        self.source = {
            **base,
            "id": "source-a",
            "filename": "Primary_Recording.wav",
            "relative_path": "02_Audio_Raw/Primary_Recording.wav",
        }
        self.script = {
            "id": "script-a",
            "case_id": "case-a",
            "asset_kind": "script",
            "filename": "Final_Script.md",
            "relative_path": "05_Production/Final_Script.md",
        }
        self.assets = [self.narration, self.effect, self.source, self.script]
        self.list_assets = Mock(side_effect=lambda *_args: self.assets)
        self.list_claims = Mock(
            return_value=[{"citations": [{"asset_id": "source-a"}]}]
        )
        self.search_service = SimpleNamespace(
            list_jobs=Mock(return_value=[]),
            list_artifacts=Mock(
                return_value=[
                    {
                        "id": "alignment-a",
                        "kind": "narration_alignment",
                        "metadata": {"case_id": "case-a", "asset_id": "narration-a"},
                    },
                    {
                        "id": "other-case-alignment",
                        "kind": "narration_alignment",
                        "metadata": {"case_id": "case-b", "asset_id": "narration-a"},
                    },
                    {
                        "id": "source-transcript",
                        "kind": "source_transcript",
                        "metadata": {"case_id": "case-a", "asset_id": "source-a"},
                    },
                ]
            ),
        )


class FakePipeline:
    def __init__(self, tmp_path):
        self.path = tmp_path / "Narration_Mix.wav"
        with wave.open(str(self.path), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(8000)
            recording.writeframes(b"\0\0" * 8000)
        self.plan = {
            "id": "plan-a",
            "title": "Owned narration fixture",
            "revision": 1,
            "status": "ready",
            "can_mix": True,
            "duration_ms": 10000,
            "narration_asset_id": "narration-a",
            "script_asset_id": "script-a",
            "transcript_artifact_id": "alignment-a",
            "cues": [
                {
                    "cue_id": "cue-a",
                    "anchor_word_index": 3,
                    "anchor_word": "hearing",
                    "anchor": "start",
                    "anchor_ms": 2000,
                    "start_ms": 2100,
                    "source_start_ms": 0,
                    "offset_ms": 100,
                    "category": "impact",
                    "tension": 0.7,
                    "reason": "A restrained accent at the narrative turning point.",
                    "query": "soft impact reveal",
                    "duration_ms": 1000,
                    "gain_db": -18.0,
                    "fade_in_ms": 20,
                    "fade_out_ms": 100,
                    "asset_id": "effect-a",
                    "asset_version_id": "effect-version",
                    "sha256": "a" * 64,
                    "enabled": True,
                    "match": {"score": 0.9},
                }
            ],
            "mix": {"narration_gain_db": 0.0, "duck_db": -8.0, "headroom_db": -1.0},
            "notes": [],
        }
        self.readiness = Mock(
            return_value={
                "alignment_ready": True,
                "word_count": 12,
                "unaligned_words": 0,
            }
        )
        self.enqueue = Mock(return_value={"id": "analysis-job"})
        self.list_plans = Mock(side_effect=lambda *_args: [deepcopy(self.plan)])
        self.get_plan = Mock(side_effect=lambda *_args: deepcopy(self.plan))
        self.save_plan = Mock(side_effect=self._save)
        self.enqueue_mix = Mock(return_value={"id": "mix-job"})
        self.mix_content = Mock(return_value=self.path)

    def _save(self, _case_id, _plan_id, record, expected_revision=None):
        assert expected_revision == self.plan["revision"]
        AcousticPlanEdit.model_validate(record)
        original = {cue["cue_id"]: cue for cue in self.plan["cues"]}
        self.plan["cues"] = [
            {**original[cue["cue_id"]], **cue} for cue in record["cues"]
        ]
        self.plan["mix"] = deepcopy(record["mix"])
        self.plan["revision"] += 1
        return deepcopy(self.plan)


APP = """
from webui import case_workspace
from webui import acoustic_pipeline as ui
workspace = case_workspace.get_workspace(None)
ui.render_acoustic_pipeline(workspace, workspace.case, lambda key: key)
"""


def controls(workspace, pipeline):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch.object(case_workspace, "get_workspace", return_value=workspace)
    )
    stack.enter_context(patch.object(ui, "get_pipeline", return_value=pipeline))
    stack.enter_context(
        patch(
            "app.services.targeted_search.sound_assets.list_sounds",
            return_value=[workspace.effect],
        )
    )
    stack.enter_context(
        patch(
            "app.services.targeted_search.sound_assets.validate_sound",
            return_value=workspace.effect,
        )
    )
    stack.enter_context(
        patch(
            "app.services.targeted_search.case_media.asset_content",
            return_value=pipeline.path,
        )
    )
    return stack


def test_narration_and_source_alignment_choices_exclude_editorial_sounds_and_other_cases(
    tmp_path,
):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    with controls(workspace, pipeline):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        narration = app.selectbox(key=ui._key("case-a", "narration"))
        assert "05_Production/Sounds/Impact.wav" not in narration.options
        alignment = app.selectbox(key=ui._key("case-a", "alignment_narration-a"))
        assert alignment.options == [
            "Acoustic latest matching alignment",
            "Acoustic word timing 1",
        ]
        registration = app.selectbox(key=ui._key("case-a", "register_asset"))
        assert registration.options == ["Impact.wav"]
        matching = next(
            item
            for item in app.selectbox
            if item.label == "Acoustic matched sound effect"
        )
        assert matching.options == [
            "Acoustic no sound effect",
            "Impact.wav",
        ]
        sheet = app.dataframe[0].value
        assert sheet.to_dict("records") == [
            {
                "Acoustic time": "00:02.100",
                "Acoustic spoken word": "hearing",
                "Acoustic selected sound": "Impact.wav",
                "Acoustic effect category": "sound_category.impact",
                "Acoustic cue state": "Acoustic included",
            }
        ]
        cue_picker = next(
            item for item in app.selectbox if item.label == "Acoustic cue to edit"
        )
        assert cue_picker.options == ["00:02.100 · hearing · sound_category.impact"]


def test_missing_alignment_cannot_enqueue_analysis_without_explicit_auto_alignment(
    tmp_path,
):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    pipeline.readiness.return_value = {
        "alignment_ready": False,
        "reason": "No matching narration alignment",
    }
    with (
        controls(workspace, pipeline),
        patch("app.services.targeted_search.worker.ensure_worker_running") as worker,
    ):
        app = AppTest.from_string(APP).run()
        assert button(app, "Acoustic align and suggest cues").disabled
        assert not list(app.exception)
        assert any(item.value == "Acoustic alignment required" for item in app.info)
        pipeline.enqueue.assert_not_called()
        worker.assert_not_called()
        next(
            item
            for item in app.checkbox
            if item.label == "Acoustic align narration if needed"
        ).check().run()
        assert not button(app, "Acoustic align and suggest cues").disabled
        button(app, "Acoustic align and suggest cues").click().run()
        assert not list(app.exception)
        assert pipeline.enqueue.call_args.args[0] == "case-a"
        options = pipeline.enqueue.call_args.args[1]
        assert options["auto_align"] is True
        assert options["narration_asset_id"] == "narration-a"
        assert options["script_asset_id"] == "script-a"
        worker.assert_called_once_with(root_dir=workspace.repo.root)


def test_existing_word_alignment_and_exact_input_assets_are_sent_for_analysis(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    with (
        controls(workspace, pipeline),
        patch("app.services.targeted_search.worker.ensure_worker_running"),
    ):
        app = AppTest.from_string(APP).run()
        app.selectbox(key=ui._key("case-a", "alignment_narration-a")).select(
            "alignment-a"
        ).run()
        button(app, "Analyze narration tension").click().run()
        assert not list(app.exception)
        options = pipeline.enqueue.call_args.args[1]
        assert options["transcript_artifact_id"] == "alignment-a"
        assert options["auto_align"] is False
        assert options["max_cues"] == 20
        assert any(item.value == "Acoustic alignment ready: 12" for item in app.success)


def test_effect_registration_requires_editorial_confirmation_and_plain_tags(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    with (
        controls(workspace, pipeline),
        patch("app.services.targeted_search.sound_assets.register_sound") as register,
    ):
        app = AppTest.from_string(APP).run()
        button(app, "Register acoustic sound effect").click().run()
        assert any(
            item.value == "Acoustic editorial confirmation required"
            for item in app.error
        )
        register.assert_not_called()
        next(
            item for item in app.text_input if item.label == "Acoustic sound tags"
        ).set_value("impact, low reveal")
        next(
            item
            for item in app.checkbox
            if item.label == "Acoustic confirm editorial effect"
        ).check()
        button(app, "Register acoustic sound effect").click().run()
        assert not list(app.exception)
        register.assert_called_once_with(
            workspace,
            "effect-a",
            tags=["impact", "low reveal"],
            description="",
            category="impact",
        )


def test_source_safe_effect_preview_rechecks_permission_before_audio_display(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    with (
        controls(workspace, pipeline),
        patch(
            "app.services.targeted_search.sound_assets.validate_sound",
            side_effect=ValueError("Sound permission revoked"),
        ),
    ):
        app = AppTest.from_string(APP).run()
        button(app, "Preview acoustic sound effect").click().run()
        assert not list(app.exception)
        assert not list(app.get("audio"))
        assert any(item.value == "Sound permission revoked" for item in app.error)


def test_edit_gain_offset_and_mix_submit_only_editable_fields_and_revision(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    with controls(workspace, pipeline):
        app = AppTest.from_string(APP).run()
        next(
            item
            for item in app.number_input
            if item.label == "Acoustic cue offset seconds"
        ).set_value(-0.2)
        next(
            item for item in app.number_input if item.label == "Acoustic effect gain dB"
        ).set_value(-24.0)
        next(
            item
            for item in app.number_input
            if item.label == "Acoustic effect ducking dB"
        ).set_value(-12.0)
        button(app, "Save acoustic cue revision").click().run()
        assert not list(app.exception)
        assert not list(app.error)
        record = pipeline.save_plan.call_args.args[2]
        cue = record["cues"][0]
        assert cue["offset_ms"] == -200
        assert cue["gain_db"] == -24.0
        assert cue["anchor_word_index"] == 3
        assert cue["asset_id"] == "effect-a"
        assert (
            not {
                "anchor_ms",
                "start_ms",
                "source_start_ms",
                "asset_version_id",
                "sha256",
                "match",
            }
            & cue.keys()
        )
        assert record["mix"]["duck_db"] == -12.0
        assert pipeline.save_plan.call_args.kwargs == {"expected_revision": 1}
        assert button(app, "Render acoustic narration mix").disabled


def test_unmatched_cue_cannot_be_enabled_until_registered_effect_is_selected(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    pipeline.plan["cues"][0].update(asset_id=None, enabled=False)
    with controls(workspace, pipeline):
        app = AppTest.from_string(APP).run()
        enabled = next(
            item
            for item in app.checkbox
            if item.label == "Acoustic enable reviewed cue"
        )
        assert enabled.disabled
        next(
            item
            for item in app.selectbox
            if item.label == "Acoustic matched sound effect"
        ).select("effect-a").run()
        enabled = next(
            item
            for item in app.checkbox
            if item.label == "Acoustic enable reviewed cue"
        )
        assert not enabled.disabled
        enabled.check()
        button(app, "Save acoustic cue revision").click().run()
        assert pipeline.save_plan.call_args.args[2]["cues"][0]["enabled"] is True


def test_explicit_review_gates_mix_and_revision_change_clears_confirmation(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    with (
        controls(workspace, pipeline),
        patch("app.services.targeted_search.worker.ensure_worker_running") as worker,
    ):
        app = AppTest.from_string(APP).run()
        assert button(app, "Render acoustic narration mix").disabled
        next(
            item
            for item in app.checkbox
            if item.label == "Acoustic confirm reviewed sound plan"
        ).check().run()
        button(app, "Render acoustic narration mix").click().run()
        pipeline.enqueue_mix.assert_called_once_with(
            "case-a", "plan-a", expected_revision=1
        )
        worker.assert_called_once_with(root_dir=workspace.repo.root)
        button(app, "Save acoustic cue revision").click().run()
        assert not list(app.exception)
        assert button(app, "Render acoustic narration mix").disabled


@pytest.mark.parametrize("locale", ["en", "zh"])
def test_translated_choices_survive_rerun_with_session_state_translation(
    locale, tmp_path
):
    """AppTest serializes option labels outside Streamlit's script context."""
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    other_plan = {
        **deepcopy(pipeline.plan),
        "id": "plan-b",
        "title": "Second sound plan",
    }
    pipeline.list_plans.side_effect = lambda *_args: [
        deepcopy(pipeline.plan),
        other_plan,
    ]
    translation = json.loads((Path("webui/i18n") / (locale + ".json")).read_text())[
        "Translation"
    ]
    script = f"""
import json
from pathlib import Path
import streamlit as st
from webui import case_workspace
from webui import acoustic_pipeline as ui
st.session_state['sound_test_translations'] = json.loads(Path('webui/i18n/{locale}.json').read_text())['Translation']
def tr(key):
    return st.session_state['sound_test_translations'].get(key, key)
workspace = case_workspace.get_workspace(None)
ui.render_acoustic_pipeline(workspace, workspace.case, tr)
"""
    with controls(workspace, pipeline):
        app = AppTest.from_string(script).run()
        assert not list(app.exception)
        app.selectbox(key=ui._key("case-a", "plan")).select("plan-a")
        app.selectbox(key=ui._key("case-a", "narration")).select("narration-a")
        app.selectbox(key=ui._key("case-a", "script")).select("script-a")
        app.selectbox(key=ui._key("case-a", "alignment_narration-a")).select(
            "alignment-a"
        )
        next(
            item
            for item in app.selectbox
            if item.label == translation["Acoustic effect category"]
        ).select("riser")
        next(
            item
            for item in app.selectbox
            if item.label == translation["Acoustic anchor word boundary"]
        ).select("end")
        button(app, translation["Save acoustic cue revision"]).click().run()
        assert not list(app.exception)
        assert not list(app.error)
        assert pipeline.save_plan.call_args.args[2]["cues"][0]["anchor"] == "end"
        assert pipeline.save_plan.call_args.kwargs == {"expected_revision": 1}
        assert button(app, translation["Render acoustic narration mix"]).disabled
        assert (
            app.selectbox(key=ui._key("case-a", "alignment_narration-a")).options[0]
            == translation["Acoustic latest matching alignment"]
        )
        assert next(
            item
            for item in app.selectbox
            if item.label == translation["Acoustic anchor word boundary"]
        ).options == [
            translation["sound_anchor.start"],
            translation["sound_anchor.end"],
        ]
        app.run()
        assert not list(app.exception)
        assert not list(app.error)


def test_stale_plan_withholds_cues_and_render_controls(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    pipeline.get_plan.side_effect = ValueError("Narration version changed")
    with controls(workspace, pipeline):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert any(item.value == "Narration version changed" for item in app.error)
        assert not any(
            item.label == "Render acoustic narration mix" for item in app.button
        )
        pipeline.enqueue_mix.assert_not_called()


def test_mix_artifact_preview_downloads_reauthorize_only_current_case_revision(
    tmp_path,
):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    workspace.search_service.list_artifacts.return_value += [
        {
            "id": "mix-a",
            "kind": "acoustic_mix",
            "metadata": {"case_id": "case-a", "plan_id": "plan-a", "plan_revision": 1},
        },
        {
            "id": "other-case-mix",
            "kind": "acoustic_mix",
            "metadata": {"case_id": "case-b", "plan_id": "plan-a", "plan_revision": 1},
        },
        {
            "id": "old-mix",
            "kind": "acoustic_mix",
            "metadata": {"case_id": "case-a", "plan_id": "plan-a", "plan_revision": 0},
        },
    ]
    with controls(workspace, pipeline):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        assert len(app.get("audio")) == 1
        assert len(app.get("download_button")) == 1
        pipeline.mix_content.assert_called_once_with("case-a", "plan-a", "mix-a")
        pipeline.mix_content.side_effect = ValueError("Sound export permission revoked")
        app.run()
        assert not list(app.get("audio"))
        assert not list(app.get("download_button"))
        assert any(
            item.value == "Sound export permission revoked" for item in app.error
        )


def test_completed_mix_precedes_setup_and_internal_ids_stay_out_of_cue_sheet(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    workspace.search_service.list_artifacts.return_value.append(
        {
            "id": "mix-a",
            "kind": "acoustic_mix",
            "metadata": {"case_id": "case-a", "plan_id": "plan-a", "plan_revision": 1},
        }
    )
    with controls(workspace, pipeline):
        app = AppTest.from_string(APP).run()
        assert not list(app.exception)
        order = [element.type for element in app.main]
        assert (
            order.index("audio") < order.index("dataframe") < order.index("selectbox")
        )
        assert "cue-a" not in app.dataframe[0].value.to_json()
        assert "effect-a" not in app.dataframe[0].value.to_json()
        assert any(
            item.label == "Acoustic start a new sound plan" and not item.proto.expanded
            for item in app.expander
        )


def test_default_plan_uses_authorized_mix_and_ignores_other_cases_and_old_revisions(
    tmp_path,
):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    plans = {
        "new-plan": {"id": "new-plan", "revision": 2},
        "plan-a": {"id": "plan-a", "revision": 1},
    }
    workspace.search_service.list_artifacts.return_value = [
        {
            "id": "other-case-mix",
            "kind": "acoustic_mix",
            "created_at": "2026-01-04",
            "metadata": {
                "case_id": "case-b",
                "plan_id": "new-plan",
                "plan_revision": 2,
            },
        },
        {
            "id": "old-revision-mix",
            "kind": "acoustic_mix",
            "created_at": "2026-01-03",
            "metadata": {
                "case_id": "case-a",
                "plan_id": "new-plan",
                "plan_revision": 1,
            },
        },
        {
            "id": "revoked-mix",
            "kind": "acoustic_mix",
            "created_at": "2026-01-02",
            "metadata": {
                "case_id": "case-a",
                "plan_id": "new-plan",
                "plan_revision": 2,
            },
        },
        {
            "id": "mix-a",
            "kind": "acoustic_mix",
            "created_at": "2026-01-01",
            "metadata": {"case_id": "case-a", "plan_id": "plan-a", "plan_revision": 1},
        },
    ]

    def deliver(_case, _plan, artifact):
        if artifact == "revoked-mix":
            raise ValueError("Permission revoked")
        return pipeline.path

    pipeline.mix_content.side_effect = deliver
    assert ui._default_plan(pipeline, workspace, "case-a", plans) == "plan-a"
    assert [call.args[-1] for call in pipeline.mix_content.call_args_list] == [
        "revoked-mix",
        "mix-a",
    ]


def test_other_case_jobs_are_not_displayed(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    workspace.search_service.list_jobs.return_value = [
        {
            "id": "job-a",
            "job_type": "acoustic_analyze",
            "status": "queued",
            "payload": {"case_id": "case-a"},
        },
        {
            "id": "job-b",
            "job_type": "acoustic_mix",
            "status": "failed",
            "payload": {"case_id": "case-b"},
            "last_error": "Other case private failure",
        },
    ]
    with controls(workspace, pipeline):
        app = AppTest.from_string(APP).run()
        assert any(
            "Acoustic suggesting cues · Acoustic job queued" == item.value
            for item in app.info
        )
        assert not any("job-a" in item.value for item in app.caption)
        assert not any("job-b" in item.value for item in app.caption)
        assert not list(app.error)


def test_completed_job_refreshes_result_once_without_replaying_finished_jobs(tmp_path):
    workspace, pipeline = FakeWorkspace(), FakePipeline(tmp_path)
    job = {
        "id": "mix-job",
        "job_type": "case_acoustic_mix",
        "status": "running",
        "payload": {"case_id": "case-a"},
    }
    workspace.search_service.list_jobs.return_value = [job]
    with controls(workspace, pipeline):
        app = AppTest.from_string(APP).run()
        assert not list(app.get("audio"))
        workspace.search_service.list_artifacts.return_value.append(
            {
                "id": "mix-a",
                "kind": "acoustic_mix",
                "metadata": {
                    "case_id": "case-a",
                    "plan_id": "plan-a",
                    "plan_revision": 1,
                },
            }
        )
        job["status"] = "complete"
        app.run()
        assert not list(app.exception)
        assert len(app.get("audio")) == 1
        assert pipeline.mix_content.call_count == 2
        app.run()
        assert not list(app.exception)
        assert pipeline.mix_content.call_count == 3


def test_real_plan_mix_download_and_revision_edit_keep_verified_word_timing(tmp_path):
    from dataclasses import replace

    from app.services.targeted_search.acoustic_compositor import render_mix
    from app.services.targeted_search.acoustic_pipeline import AcousticPipeline
    from app.services.targeted_search.case_media import import_whisperx
    from app.services.targeted_search.case_workspace import CaseWorkspace
    from app.services.targeted_search.service import SearchService
    from app.services.targeted_search.sound_assets import register_sound

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
    case = workspace.create_case("Owned sound UI integration fixture")
    incoming = service.repo.root / "owned" / "sound-ui"
    incoming.mkdir(parents=True)
    for filename in ("Narration.wav", "Effect.wav"):
        with wave.open(str(incoming / filename), "wb") as recording:
            recording.setnchannels(1)
            recording.setsampwidth(2)
            recording.setframerate(8000)
            recording.writeframes(b"\0\0" * 16000)
    (incoming / "Final_Script.md").write_text("A quiet hearing begins.")
    assets = {
        row["filename"]: row
        for row in workspace.import_folder(case["id"], incoming)["assets"]
    }
    for asset in assets.values():
        service.set_policy(
            asset["source_id"],
            "allowed_export",
            "analysis,internal_review,generated_export",
            "Owned synthetic narration, script and effect",
            "fixture-reviewer",
        )
    audio, script = assets["Narration.wav"], assets["Final_Script.md"]
    import_whisperx(
        workspace,
        audio["id"],
        {
            "audio_sha256": audio["sha256"],
            "script_sha256": script["sha256"],
            "words": [
                {"word": "A", "start": 0.1, "end": 0.2},
                {"word": "quiet", "start": 0.3, "end": 0.5},
                {"word": "hearing", "start": 0.7, "end": 1.0},
                {"word": "begins.", "start": 1.1, "end": 1.4},
            ],
        },
        scope="narration",
        script_asset_id=script["id"],
    )
    register_sound(
        workspace,
        assets["Effect.wav"]["id"],
        ["soft", "impact"],
        "A restrained accent",
        "impact",
    )
    analysis = {
        "cues": [
            {
                "cue_id": "hearing-accent",
                "anchor_word_index": 2,
                "anchor": "start",
                "category": "impact",
                "tension": 0.4,
                "reason": "A restrained editorial accent.",
                "query": "soft impact",
                "duration_ms": 500,
                "gain_db": -20,
            }
        ],
        "notes": [],
    }
    pipeline = AcousticPipeline(
        workspace, response_generator=lambda _prompt: json.dumps(analysis)
    )
    plan = pipeline.analyze(
        case["id"], {"narration_asset_id": audio["id"], "script_asset_id": script["id"]}
    )
    render_mix(workspace, plan)
    real_app = f"""
from webui import case_workspace
from webui import acoustic_pipeline as ui
workspace = case_workspace.get_workspace(None)
ui.render_acoustic_pipeline(workspace, workspace.get_case({case["id"]!r}), lambda key: key)
"""
    with (
        patch.object(case_workspace, "get_workspace", return_value=workspace),
        patch.object(ui, "get_pipeline", return_value=pipeline),
    ):
        # This integration path rechecks retained WAVs with real ffprobe and
        # hashes on each rerun; allow bounded headroom beyond mock UI's 3s.
        app = AppTest.from_string(real_app, default_timeout=10).run()
        assert not list(app.exception)
        assert not list(app.error)
        assert len(app.get("audio")) == 1
        assert len(app.get("download_button")) == 3
        next(
            item
            for item in app.number_input
            if item.label == "Acoustic cue offset seconds"
        ).set_value(0.125)
        next(
            item for item in app.number_input if item.label == "Acoustic effect gain dB"
        ).set_value(-22.0)
        button(app, "Save acoustic cue revision").click().run()
        assert not list(app.exception)
        assert not list(app.error)
        revised = pipeline.get_plan(case["id"], plan["id"])
        assert revised["revision"] == 2
        assert revised["cues"][0]["anchor_ms"] == 700
        assert revised["cues"][0]["start_ms"] == 825
        assert revised["cues"][0]["gain_db"] == -22.0
        assert not list(app.get("audio"))
        assert not list(app.get("download_button"))
        assert button(app, "Render acoustic narration mix").disabled


def test_sound_view_keeps_footage_default_and_has_exhaustive_translations():
    assert case_workspace.VIEWS[0] == "Footage Search"
    assert "Cinematic Sound" in case_workspace.VIEWS
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
    assert used <= ui.ACOUSTIC_TRANSLATION_KEYS
    for locale in ("en", "zh"):
        translation = json.loads((Path("webui/i18n") / (locale + ".json")).read_text())[
            "Translation"
        ]
        assert ui.ACOUSTIC_TRANSLATION_KEYS <= translation.keys()
