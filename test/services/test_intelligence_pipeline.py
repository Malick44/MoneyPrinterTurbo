"""Offline contract, bounded quality-loop, and legacy-stage integration tests."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.intelligence.contracts import (
    EditedScene,
    EditedScript,
    ProductionPlan,
    ProductionReview,
    RepairAction,
    RepairProposal,
    ReviewIssue,
    ScenePlan,
    SceneVisual,
    VisualPlan,
    VisualType,
)
from app.intelligence.pipeline import ProductionIntelligence
from app.intelligence.qa_contracts import MediaInspection
from app.intelligence.roles import ProductionDirector
from app.intelligence.runtime import CodexAuthError, IntelligenceError
from app.models import const
from app.models.schema import VideoParams
from app.services import task
from app.services.state import MemoryState


def example_plan():
    return ProductionPlan(
        title="Coffee",
        summary="From plant to cup",
        scenes=[
            ScenePlan(
                scene_id=f"scene_{index}",
                narration=narration,
                purpose=purpose,
                target_duration=5,
                visual_intent=purpose,
                preferred_visual_type="stock_video",
                search_query=query,
            )
            for index, narration, purpose, query in [
                (
                    1,
                    "Coffee starts with a plant.",
                    "Introduce coffee plants",
                    "coffee plants",
                ),
                (
                    2,
                    "Roast the beans for a warm cup.",
                    "Show roasting",
                    "coffee roasting",
                ),
            ]
        ],
    )


def review(stage, approved=True, scene_id="scene_2"):
    return ProductionReview(
        stage=stage,
        score=9.2 if approved else 6,
        approved=approved,
        summary="Clear visuals" if approved else "Second scene is unrelated",
        issues=[]
        if approved
        else [
            ReviewIssue(
                scene_id=scene_id,
                severity="error",
                category="relevance",
                description="Unrelated scene",
            )
        ],
    )


class FakeRuntime:
    def __init__(self, responses=None, plan=None):
        self.responses = responses or {}
        self.calls = []
        self.plan = plan or example_plan()

    def generate(self, prompt, output_type, images=()):
        role = prompt.split("\nRole: ")[1].split("\n")[0]
        self.calls.append((role, images))
        queued = self.responses.get(role)
        if queued:
            result = queued.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        plan = self.plan.model_copy(deep=True)
        if output_type is ProductionPlan:
            return plan
        if output_type is EditedScript:
            return EditedScript(
                scenes=[
                    EditedScene(scene_id=item.scene_id, narration=item.narration)
                    for item in plan.scenes
                ]
            )
        if output_type is VisualPlan:
            return VisualPlan(
                scenes=[
                    SceneVisual(
                        **{
                            key: value
                            for key, value in item.model_dump().items()
                            if key in SceneVisual.model_fields
                        }
                    )
                    for item in plan.scenes
                ]
            )
        if output_type is ProductionReview:
            return review(
                {
                    "PlanReviewer": "plan_review",
                    "MaterialReviewer": "material_review",
                    "RenderReviewer": "render_review",
                }[role]
            )
        raise AssertionError(f"Unexpected {role} call")


@pytest.fixture
def params():
    return VideoParams(
        video_subject="Coffee",
        production_intelligence="codex",
        bgm_type="",
        subtitle_enabled=False,
    )


@pytest.fixture
def sheets():
    with (
        patch(
            "app.intelligence.contact_sheets.create_material_contact_sheet",
            return_value="materials-contact-sheet.jpg",
        ) as materials,
        patch(
            "app.intelligence.contact_sheets.create_render_contact_sheet",
            return_value="render-contact-sheet.jpg",
        ) as renders,
        patch(
            "app.intelligence.media_qa.inspect_render",
            return_value=MediaInspection(passed=True, summary="Fixture technical check"),
        ),
    ):
        yield materials, renders


def prepared(tmp_path, params, runtime=None):
    pipeline = ProductionIntelligence(
        "unit", params, task_dir=tmp_path, runtime=runtime or FakeRuntime()
    )
    pipeline.prepare()
    pipeline.set_timeline(10)
    return pipeline


def test_contracts_reject_unknown_fields_duplicate_ids_and_invalid_duration():
    plan = example_plan().model_dump()
    plan["unexpected"] = "ignored?"
    with pytest.raises(ValidationError):
        ProductionPlan.model_validate(plan)
    plan.pop("unexpected")
    plan["scenes"][1]["scene_id"] = "scene_1"
    with pytest.raises(ValidationError):
        ProductionPlan.model_validate(plan)
    plan["scenes"][1]["scene_id"] = "scene_2"
    plan["scenes"][0]["target_duration"] = 0
    with pytest.raises(ValidationError):
        ProductionPlan.model_validate(plan)


def test_all_visual_types_are_explicit_contract_values():
    assert {kind.value for kind in VisualType} == {
        "stock_video",
        "ai_video",
        "ai_image",
        "local_asset",
        "diagram",
        "chart",
        "screenshot",
        "text_card",
        "icon_composition",
    }


def test_roles_revalidate_outputs_without_leaking_validation_input():
    runtime = MagicMock()
    runtime.generate.return_value = {"secret": "sk-do-not-print-this"}
    with pytest.raises(IntelligenceError) as error:
        ProductionDirector(runtime).run(brief={})
    assert error.value.stage == "production_plan"
    assert "sk-do-not" not in str(error.value)
    assert "invalid structured" in str(error.value)


def test_production_plan_generation_artifacts_and_role_order(tmp_path, params):
    runtime = FakeRuntime()
    pipeline = prepared(tmp_path, params, runtime)
    assert [role for role, _ in runtime.calls] == [
        "ProductionDirector",
        "PlanReviewer",
        "ScriptEditor",
        "VisualDirector",
    ]
    assert pipeline.params.match_materials_to_script
    assert not params.match_materials_to_script
    assert pipeline.terms == ["coffee plants", "coffee roasting"]
    assert pipeline.plan.narration == example_plan().narration
    assert json.loads((tmp_path / "production-review.json").read_text())["approved"]
    assert json.loads((tmp_path / "repair-history.json").read_text()) == []
    assert {path.name for path in tmp_path.iterdir()} >= {
        "production-brief.json",
        "production-plan.json",
        "production-review.json",
        "material-review.json",
        "render-review.json",
        "repair-history.json",
        "visual-plan.json",
    }


def test_plan_review_repairs_only_rejected_scene(tmp_path, params):
    replacement = (
        example_plan()
        .scenes[1]
        .model_copy(update={"purpose": "Make the roast process specific"})
    )
    proposal = RepairProposal(
        actions=[
            RepairAction(
                stage="production_plan",
                scene_id="scene_2",
                action="revise_scene",
                reason="Clarify",
                replacement_scene=replacement,
            )
        ]
    )
    runtime = FakeRuntime(
        {
            "PlanReviewer": [review("plan_review", False), review("plan_review")],
            "RepairPlanner": [proposal],
        }
    )
    pipeline = prepared(tmp_path, params, runtime)
    assert pipeline.plan.scenes[0] == example_plan().scenes[0]
    assert pipeline.plan.scenes[1].purpose == replacement.purpose
    assert len(pipeline.history) == 1
    assert pipeline.history[0].stage == "plan_review"


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_max_plan_repair_passes_are_enforced(tmp_path, params, budget):
    params.codex_max_repair_passes = budget
    proposal = RepairProposal(
        actions=[
            RepairAction(
                stage="production_plan",
                scene_id="scene_2",
                action="revise_scene",
                reason="Clarify",
                replacement_scene=example_plan().scenes[1],
            )
        ]
    )
    runtime = FakeRuntime(
        {
            "PlanReviewer": [review("plan_review", False)] * (budget + 1),
            "RepairPlanner": [proposal] * budget,
        }
    )
    pipeline = ProductionIntelligence(
        "unit", params, task_dir=tmp_path, runtime=runtime
    )
    with pytest.raises(IntelligenceError) as error:
        pipeline.prepare()
    assert error.value.stage == "plan_review"
    assert len([role for role, _ in runtime.calls if role == "RepairPlanner"]) == budget
    assert (
        len([role for role, _ in runtime.calls if role == "PlanReviewer"]) == budget + 1
    )
    assert len(json.loads((tmp_path / "repair-history.json").read_text())) == budget


def test_quality_threshold_requires_approval_and_score(tmp_path, params):
    params.codex_max_repair_passes = 0
    low = review("plan_review")
    low.score = 8
    with pytest.raises(IntelligenceError, match="did not meet"):
        prepared(tmp_path, params, FakeRuntime({"PlanReviewer": [low]}))


def test_unsupported_visual_execution_fails_actionably(tmp_path, params):
    scene = (
        example_plan()
        .scenes[0]
        .model_copy(update={"preferred_visual_type": VisualType.chart})
    )
    visuals = VisualPlan(
        scenes=[
            SceneVisual(
                **{
                    key: value
                    for key, value in item.model_dump().items()
                    if key in SceneVisual.model_fields
                }
            )
            for item in [scene, example_plan().scenes[1]]
        ]
    )
    with pytest.raises(IntelligenceError) as error:
        prepared(tmp_path, params, FakeRuntime({"VisualDirector": [visuals]}))
    assert error.value.stage == "visual_plan"
    assert "cannot execute" in str(error.value)


def test_visual_director_cannot_change_scene_order(tmp_path, params):
    visuals = VisualPlan(
        scenes=[
            SceneVisual(
                **{
                    key: value
                    for key, value in item.model_dump().items()
                    if key in SceneVisual.model_fields
                }
            )
            for item in reversed(example_plan().scenes)
        ]
    )
    with pytest.raises(IntelligenceError, match="reordered scene"):
        prepared(tmp_path, params, FakeRuntime({"VisualDirector": [visuals]}))


def test_subtitle_timeline_tracks_actual_narration_boundaries(tmp_path, params):
    pipeline = prepared(tmp_path, params)
    srt = tmp_path / "subtitle.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:03,000\nCoffee starts with a plant.\n\n"
        "2\n00:00:03,000 --> 00:00:10,000\nRoast the beans for a warm cup.\n"
    )
    pipeline.set_timeline(10, str(srt))
    assert pipeline.timeline == [
        {"scene_id": "scene_1", "start": 0, "end": 3},
        {"scene_id": "scene_2", "start": 3, "end": 10},
    ]
    assert (
        json.loads((tmp_path / "scene-timeline.json").read_text())["timing_source"]
        == "narration_subtitles"
    )


def test_missing_subtitles_use_explicit_duration_estimate(tmp_path, params):
    pipeline = prepared(tmp_path, params)
    pipeline.set_timeline(13)
    assert pipeline.timeline[-1]["end"] == 13
    assert pipeline.timeline[1]["start"] == 6.5
    assert (
        json.loads((tmp_path / "scene-timeline.json").read_text())["timing_source"]
        == "target_duration_scaled_to_audio"
    )


def test_material_review_sees_image_and_replaces_only_bad_scene(
    tmp_path, params, sheets
):
    proposal = RepairProposal(
        actions=[
            RepairAction(
                stage="materials",
                scene_id="scene_2",
                action="change_query",
                reason="Wrong subject",
                search_query="industrial coffee roasting closeup",
            )
        ]
    )
    runtime = FakeRuntime(
        {
            "MaterialReviewer": [
                review("material_review", False),
                review("material_review"),
            ],
            "RepairPlanner": [proposal],
        }
    )
    pipeline = prepared(tmp_path, params, runtime)
    acquire = MagicMock(side_effect=[["plant.mp4"], ["bad.mp4"], ["roasting.mp4"]])
    pipeline.acquire_materials(acquire)
    assert pipeline.material_paths == ["plant.mp4", "roasting.mp4"]
    assert acquire.call_count == 3
    assert acquire.call_args.args[1] == ["industrial coffee roasting closeup"]
    assert [images for role, images in runtime.calls if role == "MaterialReviewer"] == [
        (str(Path("materials-contact-sheet.jpg").resolve()),)
    ] * 2
    assert len([role for role, _ in runtime.calls if role == "ProductionDirector"]) == 1


def test_material_repair_cannot_modify_good_scene(tmp_path, params, sheets):
    proposal = RepairProposal(
        actions=[
            RepairAction(
                stage="materials",
                scene_id="scene_1",
                action="change_query",
                reason="Wrong subject",
                search_query="flowers",
            )
        ]
    )
    runtime = FakeRuntime(
        {
            "MaterialReviewer": [review("material_review", False)],
            "RepairPlanner": [proposal],
        }
    )
    pipeline = prepared(tmp_path, params, runtime)
    acquire = MagicMock(side_effect=[["plant.mp4"], ["bad.mp4"]])
    with pytest.raises(IntelligenceError, match="accepted or unknown"):
        pipeline.acquire_materials(acquire)
    assert acquire.call_count == 2


def test_material_repair_rejects_same_asset(tmp_path, params, sheets):
    proposal = RepairProposal(
        actions=[
            RepairAction(
                stage="materials",
                scene_id="scene_2",
                action="change_query",
                reason="Wrong subject",
                search_query="different coffee roasting",
            )
        ]
    )
    runtime = FakeRuntime(
        {
            "MaterialReviewer": [review("material_review", False)],
            "RepairPlanner": [proposal],
        }
    )
    pipeline = prepared(tmp_path, params, runtime)
    acquire = MagicMock(side_effect=[["plant.mp4"], ["bad.mp4"], ["bad.mp4"]])
    with pytest.raises(IntelligenceError, match="No new usable material"):
        pipeline.acquire_materials(acquire)
    assert pipeline.material_paths == ["plant.mp4", "bad.mp4"]


def test_material_review_budget_exhaustion_retains_materials(tmp_path, params, sheets):
    params.codex_max_repair_passes = 0
    runtime = FakeRuntime({"MaterialReviewer": [review("material_review", False)]})
    pipeline = prepared(tmp_path, params, runtime)
    with pytest.raises(IntelligenceError) as error:
        pipeline.acquire_materials(
            MagicMock(side_effect=[["plant.mp4"], ["roast.mp4"]])
        )
    assert error.value.stage == "material_review"
    assert pipeline.material_paths == ["plant.mp4", "roast.mp4"]
    assert (tmp_path / "scene-materials.json").exists()


def test_disabled_reviews_skip_codex_but_keep_sheets_and_artifacts(
    tmp_path, params, sheets
):
    params.codex_review_enabled = params.codex_material_review_enabled = (
        params.codex_render_review_enabled
    ) = False
    runtime = FakeRuntime()
    pipeline = prepared(tmp_path, params, runtime)
    acquire = MagicMock(side_effect=[["plant.mp4"], ["roast.mp4"]])
    pipeline.acquire_materials(acquire)
    pipeline.review_renders(["final.mp4"], ["combined.mp4"], [], acquire, MagicMock())
    assert not any("Reviewer" in role for role, _ in runtime.calls)
    for name in [
        "production-review.json",
        "material-review.json",
        "render-review.json",
    ]:
        assert json.loads((tmp_path / name).read_text()) == {"status": "disabled"}
    sheets[0].assert_called_once()
    sheets[1].assert_called_once()


def test_render_repair_preserves_narration_good_materials_and_previous_video(
    tmp_path, params, sheets
):
    proposal = RepairProposal(
        actions=[
            RepairAction(
                stage="video",
                scene_id="scene_2",
                action="rerender",
                reason="Cropping hides the roaster",
                video_fit_mode="contain",
            )
        ]
    )
    runtime = FakeRuntime(
        {
            "RenderReviewer": [review("render_review", False), review("render_review")],
            "RepairPlanner": [proposal],
        }
    )
    pipeline = prepared(tmp_path, params, runtime)
    acquire = MagicMock(side_effect=[["plant.mp4"], ["roast.mp4"]])
    pipeline.acquire_materials(acquire)
    final, combined = tmp_path / "final-1.mp4", tmp_path / "combined-1.mp4"
    final.write_bytes(b"existing usable video")
    combined.write_bytes(b"existing base")
    render = MagicMock(return_value=([str(final)], [str(combined)], []))
    with patch(
        "app.intelligence.execution.prepare_scene_clips",
        return_value=["prepared-1.mp4", "prepared-2.mp4"],
    ) as prepare:
        pipeline.review_renders([str(final)], [str(combined)], [], acquire, render)
    assert acquire.call_count == 2
    assert render.call_count == 1
    assert pipeline.scene_fit_modes == {"scene_2": "contain"}
    assert pipeline.params.video_fit_mode == "cover"
    assert prepare.call_args.kwargs["fit_modes"] == {"scene_2": "contain"}
    assert (
        tmp_path / "final-1-before-repair-1.mp4"
    ).read_bytes() == b"existing usable video"
    assert [role for role, _ in runtime.calls].count("ScriptEditor") == 1


def test_render_review_checks_every_output_and_preserves_failed_video(
    tmp_path, params, sheets
):
    params.codex_max_repair_passes = 0
    runtime = FakeRuntime(
        {"RenderReviewer": [review("render_review"), review("render_review", False)]}
    )
    pipeline = prepared(tmp_path, params, runtime)
    final = tmp_path / "final-2.mp4"
    final.write_bytes(b"still usable")
    with pytest.raises(IntelligenceError) as error:
        pipeline.review_renders(
            ["final-1.mp4", str(final)], [], [], MagicMock(), MagicMock()
        )
    assert error.value.stage == "render_review"
    assert sheets[1].call_count == 2
    assert final.read_bytes() == b"still usable"
    assert not json.loads((tmp_path / "render-review.json").read_text())["approved"]


def test_task_script_boundary_uses_codex_and_never_legacy_llm(tmp_path, params):
    runtime = FakeRuntime()
    with (
        patch("app.intelligence.pipeline.CodexRuntime", return_value=runtime),
        patch.object(task.utils, "task_dir", return_value=str(tmp_path)),
        patch.object(task.sm, "state", MemoryState()),
        patch.object(task, "generate_script") as legacy,
    ):
        result = task.start("codex-script", params, stop_at="script")
    assert result == {"script": example_plan().narration}
    legacy.assert_not_called()
    assert (tmp_path / "script.json").exists()


def test_task_auth_failure_stage_is_actionable_and_has_no_key(
    tmp_path, params, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-never-log-this")
    runtime = FakeRuntime(
        {
            "ProductionDirector": [
                CodexAuthError("Sign in with ChatGPT. token=sk-test-never-log-this")
            ]
        }
    )
    state = MemoryState()
    with (
        patch("app.intelligence.pipeline.CodexRuntime", return_value=runtime),
        patch.object(task.utils, "task_dir", return_value=str(tmp_path)),
        patch.object(task.sm, "state", state),
    ):
        result = task.start("auth-fail", params, stop_at="script")
    assert result["failed_stage"] == "codex_auth"
    assert "ChatGPT" in result["error"]
    assert "sk-test-never-log-this" not in json.dumps(result)
    assert state.get_task("auth-fail")["state"] == const.TASK_STATE_FAILED


def test_task_material_review_failure_returns_prior_artifacts(tmp_path, params, sheets):
    params.codex_max_repair_passes = 0
    runtime = FakeRuntime({"MaterialReviewer": [review("material_review", False)]})
    state = MemoryState()
    with (
        patch("app.intelligence.pipeline.CodexRuntime", return_value=runtime),
        patch.object(task.utils, "task_dir", return_value=str(tmp_path)),
        patch.object(task.sm, "state", state),
        patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task, "generate_audio", return_value=("audio.mp3", 10, None)),
        patch.object(task, "generate_subtitle", return_value="subtitle.srt"),
        patch.object(
            task, "get_video_materials", side_effect=[["plant.mp4"], ["roast.mp4"]]
        ),
    ):
        result = task.start("failed-material-review", params)
    assert result["failed_stage"] == "material_review"
    assert result["audio_file"] == "audio.mp3"
    assert result["materials"] == ["plant.mp4", "roast.mp4"]


def test_task_render_failure_returns_usable_video_and_audio(tmp_path, params, sheets):
    params.codex_max_repair_passes = 0
    runtime = FakeRuntime({"RenderReviewer": [review("render_review", False)]})
    state = MemoryState()
    with (
        patch("app.intelligence.pipeline.CodexRuntime", return_value=runtime),
        patch(
            "app.intelligence.execution.prepare_scene_clips",
            return_value=["prepared.mp4"],
        ),
        patch.object(task.utils, "task_dir", return_value=str(tmp_path)),
        patch.object(task.sm, "state", state),
        patch.object(task.utils, "check_ffmpeg_ready", return_value=True),
        patch.object(task, "generate_audio", return_value=("audio.mp3", 10, None)),
        patch.object(task, "generate_subtitle", return_value="subtitle.srt"),
        patch.object(
            task, "get_video_materials", side_effect=[["plant.mp4"], ["roast.mp4"]]
        ),
        patch.object(
            task,
            "generate_final_videos",
            return_value=(["final.mp4"], ["combined.mp4"], []),
        ),
    ):
        result = task.start("failed-render-review", params)
    assert result["failed_stage"] == "render_review"
    assert result["videos"] == ["final.mp4"]
    assert result["audio_file"] == "audio.mp3"
    assert state.get_task("failed-render-review")["videos"] == ["final.mp4"]


def test_task_legacy_pipeline_still_uses_existing_stages(params):
    params.production_intelligence = "legacy"
    with (
        patch.object(task, "generate_script", return_value="Legacy script") as legacy,
        patch.object(task.sm, "state", MemoryState()),
        patch("app.intelligence.pipeline.CodexRuntime") as runtime,
    ):
        assert task.start("legacy-script", params, stop_at="script") == {
            "script": "Legacy script"
        }
    legacy.assert_called_once()
    runtime.assert_not_called()


def test_artifacts_redact_credentials_in_prompts_and_model_output(
    tmp_path, params, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value-123")
    params.custom_system_prompt = "Do not reveal sk-secret-value-123"
    plan = example_plan()
    plan.summary = "sk-secret-value-123"
    prepared(tmp_path, params, FakeRuntime({"ProductionDirector": [plan]}))
    for path in tmp_path.glob("*.json"):
        assert "sk-secret-value-123" not in path.read_text()


def test_codex_script_artifact_redacts_supplied_params(tmp_path, params, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-private-model-key")
    params.custom_system_prompt = "sk-private-model-key"
    with patch.object(task.utils, "task_dir", return_value=str(tmp_path)):
        task.save_script_data("redacted-script", "test", [], params)
    assert "sk-private-model-key" not in (tmp_path / "script.json").read_text()


def test_refined_plan_is_reviewed_with_the_same_total_repair_budget(tmp_path, params):
    params.codex_max_repair_passes = 1
    repair = RepairProposal(
        actions=[
            RepairAction(
                stage="production_plan",
                scene_id="scene_2",
                action="revise_scene",
                reason="Fix pacing",
                replacement_scene=example_plan().scenes[1],
            )
        ]
    )
    edited = EditedScript(
        scenes=[
            EditedScene(
                scene_id="scene_1", narration="Coffee begins with a living plant."
            ),
            EditedScene(
                scene_id="scene_2", narration=example_plan().scenes[1].narration
            ),
        ]
    )
    runtime = FakeRuntime(
        {
            "PlanReviewer": [
                review("plan_review", False),
                review("plan_review"),
                review("plan_review", False),
            ],
            "RepairPlanner": [repair],
            "ScriptEditor": [edited],
        }
    )
    with pytest.raises(IntelligenceError) as error:
        prepared(tmp_path, params, runtime)
    assert error.value.stage == "plan_review"
    assert [role for role, _ in runtime.calls].count("RepairPlanner") == 1
    assert [role for role, _ in runtime.calls].count("PlanReviewer") == 3


def test_supplied_script_cannot_be_silently_rewritten(tmp_path, params):
    params.video_script = "The user provided these exact words."
    with pytest.raises(IntelligenceError, match="changed the supplied script"):
        prepared(tmp_path, params)


def test_cumulative_timeline_quantization_prevents_drift(tmp_path, params):
    pipeline = prepared(tmp_path, params)
    scene = example_plan().scenes[0]
    pipeline.plan = ProductionPlan(
        title="Test",
        summary="Many scenes",
        scenes=[
            scene.model_copy(
                update={"scene_id": f"scene_{index}", "target_duration": 1.001}
            )
            for index in range(50)
        ],
    )
    pipeline.set_timeline(50.05)
    from app.services import video

    assert pipeline.timeline[-1]["end"] <= 50.05 + 1 / video.fps
    assert all(
        abs(row["start"] * video.fps - round(row["start"] * video.fps)) < 1e-7
        for row in pipeline.timeline
    )
    assert all(
        pipeline.timeline[i]["end"] == pipeline.timeline[i + 1]["start"]
        for i in range(49)
    )


def test_local_material_repair_uses_an_alternative_without_redownloading(
    tmp_path, params, sheets
):
    params.video_source = "local"
    plan = example_plan()
    for scene in plan.scenes:
        scene.preferred_visual_type = VisualType.local_asset
    proposal = RepairProposal(
        actions=[
            RepairAction(
                stage="materials",
                scene_id="scene_2",
                action="replace_material",
                reason="Wrong subject",
            )
        ]
    )
    runtime = FakeRuntime(
        {
            "MaterialReviewer": [
                review("material_review", False),
                review("material_review"),
            ],
            "RepairPlanner": [proposal],
        },
        plan=plan,
    )
    pipeline = prepared(tmp_path, params, runtime)
    acquire = MagicMock(return_value=["plant.mp4", "wrong.mp4", "roasting.mp4"])
    pipeline.acquire_materials(acquire)
    acquire.assert_called_once()
    assert pipeline.material_paths == ["plant.mp4", "roasting.mp4"]


@pytest.mark.parametrize("stage", ["material_review", "render_review"])
def test_media_quality_loops_stop_at_configured_budget(tmp_path, params, sheets, stage):
    params.codex_max_repair_passes = 1
    proposal = RepairProposal(
        actions=[
            RepairAction(
                stage="materials",
                scene_id="scene_2",
                action="change_query",
                reason="Wrong subject",
                search_query="more specific roasting",
            )
        ]
    )
    role = "MaterialReviewer" if stage == "material_review" else "RenderReviewer"
    runtime = FakeRuntime(
        {
            role: [review(stage, False), review(stage, False)],
            "RepairPlanner": [proposal],
        }
    )
    pipeline = prepared(tmp_path, params, runtime)
    acquire = MagicMock(side_effect=[["plant.mp4"], ["wrong.mp4"], ["new.mp4"]])
    with patch(
        "app.intelligence.execution.prepare_scene_clips", return_value=["prepared.mp4"]
    ):
        with pytest.raises(IntelligenceError) as error:
            pipeline.acquire_materials(acquire)
            pipeline.review_renders(
                ["final.mp4"],
                ["combined.mp4"],
                [],
                acquire,
                MagicMock(return_value=(["fixed.mp4"], ["base.mp4"], [])),
            )
    assert error.value.stage == stage
    assert [name for name, _ in runtime.calls].count("RepairPlanner") == 1
    assert [name for name, _ in runtime.calls].count(role) == 2
    assert acquire.call_count == 3
