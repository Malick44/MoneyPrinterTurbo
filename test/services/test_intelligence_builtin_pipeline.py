"""Built-in acquisition, targeted repair, and mandatory technical QA boundaries."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.intelligence.contracts import RepairAction, RepairProposal, ReviewIssue
from app.intelligence.pipeline import ProductionIntelligence
from app.intelligence.qa_contracts import MediaInspection
from app.intelligence.runtime import IntelligenceError
from app.intelligence.visual_contracts import BuiltinVisualSpec
from app.models.schema import MaterialInfo, VideoParams
from test.services.test_intelligence_pipeline import FakeRuntime, example_plan, review


def builtin_pipeline(tmp_path, **overrides):
    plan = example_plan()
    for scene in plan.scenes:
        scene.preferred_visual_type = "text_card"
        scene.builtin_visual = BuiltinVisualSpec(
            title=scene.purpose, body=scene.narration
        )
    params = VideoParams(
        video_subject="Coffee",
        production_intelligence="codex",
        video_source="builtin",
        codex_material_review_enabled=False,
        **overrides,
    )
    pipeline = ProductionIntelligence(
        "builtin", params, task_dir=tmp_path, runtime=FakeRuntime(plan=plan)
    )
    pipeline.prepare()
    pipeline.set_timeline(2)
    return pipeline


def test_builtin_acquisition_needs_no_media_provider_and_reuses_cached_assets(tmp_path):
    pipeline = builtin_pipeline(tmp_path)
    acquire = MagicMock(
        side_effect=AssertionError("A built-in image must not call a media provider")
    )
    pipeline.acquire_materials(acquire)
    paths = pipeline.material_paths[:]
    assert len(paths) == 2
    assert "screenshot" not in pipeline.brief.supported_visual_types
    assert {item.value for item in pipeline.brief.supported_visual_types} == {
        "diagram",
        "chart",
        "text_card",
        "icon_composition",
    }
    pipeline.acquire_materials(acquire)
    assert pipeline.material_paths == paths
    assert all(Path(p).is_file() for p in paths)
    acquire.assert_not_called()


def test_builtin_repair_changes_only_rejected_visual_and_keeps_narration(tmp_path):
    pipeline = builtin_pipeline(tmp_path)
    acquire = MagicMock(side_effect=AssertionError("No media provider"))
    pipeline.acquire_materials(acquire)
    original_paths = pipeline.material_paths[:]
    narration = pipeline.plan.narration
    updated = BuiltinVisualSpec(title="Roast the beans", body="Finish with a warm cup.")
    pipeline.runtime.responses["RepairPlanner"] = [
        RepairProposal(
            actions=[
                RepairAction(
                    stage="materials",
                    scene_id="scene_2",
                    action="revise_visual",
                    reason="Make the message concise",
                    builtin_visual=updated,
                )
            ]
        )
    ]
    actions = pipeline._repair(review("material_review", False), 1)
    pipeline._apply_media_repairs(actions, acquire)
    assert pipeline.material_paths[0] == original_paths[0]
    assert pipeline.material_paths[1] != original_paths[1]
    assert pipeline.plan.narration == narration
    assert pipeline.plan.scenes[1].builtin_visual == updated


def test_builtin_repair_rejects_identical_spec(tmp_path):
    pipeline = builtin_pipeline(tmp_path)
    pipeline.runtime.responses["RepairPlanner"] = [
        RepairProposal(
            actions=[
                RepairAction(
                    stage="materials",
                    scene_id="scene_2",
                    action="revise_visual",
                    reason="No change",
                    builtin_visual=pipeline.plan.scenes[1].builtin_visual,
                )
            ]
        )
    ]
    with pytest.raises(IntelligenceError, match="changed visual specification"):
        pipeline._repair(review("material_review", False), 1)


def test_later_acquisition_failure_preserves_completed_scene_references(tmp_path):
    pipeline = builtin_pipeline(tmp_path)
    with patch(
        "app.intelligence.builtin_visuals.render_builtin_visual",
        side_effect=["scene-1.png", ValueError("Cannot fit second scene text")],
    ):
        with pytest.raises(IntelligenceError, match="second scene"):
            pipeline.acquire_materials(MagicMock())
    assert pipeline.material_paths == ["scene-1.png"]
    assert json.loads((tmp_path / "scene-materials.json").read_text())[0]["paths"] == [
        "scene-1.png"
    ]


def test_screenshot_path_is_confined_to_uploaded_materials(tmp_path):
    outside = tmp_path / "private.png"
    outside.write_bytes(b"not an uploaded image")
    with pytest.raises(IntelligenceError, match="uploaded local images"):
        builtin_pipeline(
            tmp_path / "task",
            video_materials=[MaterialInfo(provider="local", url=str(outside))],
        )


def test_technical_failure_blocks_even_when_semantic_review_disabled(tmp_path):
    pipeline = builtin_pipeline(tmp_path, codex_render_review_enabled=False)
    final = tmp_path / "final.mp4"
    final.write_bytes(b"preserved for inspection")
    inspection = MediaInspection(
        passed=False,
        summary="Decode failed",
        issues=[
            ReviewIssue(
                severity="error",
                category="decode",
                description="The final video cannot be decoded.",
            )
        ],
    )
    with patch(
        "app.intelligence.media_qa.inspect_render", return_value=inspection
    ) as inspect:
        with pytest.raises(IntelligenceError, match="technical failure"):
            pipeline.review_renders(
                [str(final)],
                [],
                [],
                MagicMock(),
                MagicMock(),
                narration_path="audio.wav",
            )
    assert inspect.call_args.kwargs["narration_path"] == "audio.wav"
    assert final.read_bytes() == b"preserved for inspection"
    assert not json.loads((tmp_path / "media-inspection.json").read_text())["passed"]
    assert not json.loads((tmp_path / "render-review.json").read_text())["approved"]
    assert not any(role == "RenderReviewer" for role, _ in pipeline.runtime.calls)


def test_custom_narration_requires_audio_despite_stale_no_voice_selection(tmp_path):
    from app.services.voice import NO_VOICE_NAME

    pipeline = builtin_pipeline(
        tmp_path,
        codex_render_review_enabled=False,
        voice_name=NO_VOICE_NAME,
        custom_audio_file="narration.wav",
    )
    with (
        patch(
            "app.intelligence.media_qa.inspect_render",
            return_value=MediaInspection(passed=True, summary="OK"),
        ) as inspect,
        patch(
            "app.intelligence.contact_sheets.create_render_contact_sheet",
            return_value="sheet.jpg",
        ),
    ):
        pipeline.review_renders(["final.mp4"], [], [], MagicMock(), MagicMock())
    assert inspect.call_args.kwargs["expected_audio"] is True


def test_technical_caution_does_not_reopen_a_semantically_accepted_scene(tmp_path):
    pipeline = builtin_pipeline(tmp_path)
    inspection = MediaInspection(
        passed=True,
        summary="Technical warning",
        issues=[
            ReviewIssue(
                scene_id="scene_1",
                severity="warning",
                category="black_frames",
                description="Dark interval",
            )
        ],
    )
    pipeline.runtime.responses.update(
        {
            "RenderReviewer": [review("render_review", False, scene_id="scene_2")],
            "RepairPlanner": [
                RepairProposal(
                    actions=[
                        RepairAction(
                            stage="materials",
                            scene_id="scene_1",
                            action="revise_visual",
                            reason="Unrequested repair",
                            builtin_visual=BuiltinVisualSpec(
                                title="Do not replace accepted content"
                            ),
                        )
                    ]
                )
            ],
        }
    )
    with (
        patch("app.intelligence.media_qa.inspect_render", return_value=inspection),
        patch(
            "app.intelligence.contact_sheets.create_render_contact_sheet",
            return_value="sheet.jpg",
        ),
    ):
        with pytest.raises(IntelligenceError, match="accepted or unknown scene"):
            pipeline.review_renders(["final.mp4"], [], [], MagicMock(), MagicMock())
