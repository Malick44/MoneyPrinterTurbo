"""Regressions from independent scene timing and offline-isolation review."""

from unittest.mock import patch

import pytest
from PIL import Image

from app.config import config
from app.intelligence.contact_sheets import media_duration
from app.intelligence.contracts import ProductionPlan, SceneMaterial, ScenePlan
from app.intelligence.execution import prepare_scene_clips
from app.intelligence.pipeline import ProductionIntelligence
from app.intelligence.smoke import offline_smoke
from app.models.schema import VideoAspect, VideoParams
from app.services import state, video


def test_offline_smoke_isolates_saved_preferences_and_application_state():
    # The explicit offline smoke promises a self-contained fixture, regardless
    # of what the developer last selected in the WebUI.
    application_state = state.MemoryState()
    application_state.update_task(
        "user-task", state=1, progress=100, videos=["user.mp4"]
    )
    original_state = application_state.get_all_tasks(1, 100)
    with (
        patch.dict(
            config.app,
            {
                "enable_redis": True,
                "production_intelligence": "codex",
                "codex_review_enabled": False,
                "codex_material_review_enabled": False,
                "codex_render_review_enabled": False,
                "codex_quality_threshold": 10,
                "codex_max_repair_passes": 0,
                "upload_post_enabled": True,
                "upload_post_auto_upload": True,
                "twelvelabs_api_keys": ["mock-service-key"],
                "api_key": "mock-api-key",
            },
        ),
        patch.object(state, "state", application_state),
        patch(
            "redis.StrictRedis",
            side_effect=AssertionError("Offline smoke constructed a Redis client"),
        ) as redis_client,
        patch(
            "requests.sessions.Session.request",
            side_effect=AssertionError("Offline smoke attempted a network request"),
        ),
    ):
        result = offline_smoke()
        assert config.app["codex_quality_threshold"] == 10
        assert config.app["enable_redis"] is True
        assert state.state is application_state
    redis_client.assert_not_called()
    assert application_state.get_all_tasks(1, 100) == original_state
    assert result["api_settings"] is True
    assert result["reviews_and_contact_sheets"] is True
    assert result["legacy_video"] is True
    assert result["external_ai_calls"] == 0


def test_fractional_scene_durations_do_not_accumulate_encoded_timeline_drift(tmp_path):
    plan = ProductionPlan(
        title="Colors",
        summary="Four fractional scenes",
        scenes=[
            ScenePlan(
                scene_id=f"s{index}",
                narration=f"Scene {index}.",
                purpose="A color",
                target_duration=0.15,
                visual_intent="A color card",
                preferred_visual_type="local_asset",
            )
            for index in range(4)
        ],
    )
    materials = []
    for scene, color in zip(plan.scenes, ("red", "blue", "green", "yellow")):
        path = tmp_path / f"{scene.scene_id}.png"
        Image.new("RGB", (160, 90), color).save(path)
        materials.append(
            SceneMaterial(
                scene_id=scene.scene_id,
                paths=[str(path)],
                source="local",
                visual_type="local_asset",
                target_duration=0.15,
            )
        )
    params = VideoParams(
        video_subject="Colors", production_intelligence="codex", n_threads=1
    )
    production = ProductionIntelligence(
        "rounding", params, runtime=object(), task_dir=tmp_path
    )
    production.plan = plan
    with (
        patch.object(VideoAspect, "to_resolution", return_value=(160, 90)),
        patch.object(video, "fps", 10),
    ):
        production.set_timeline(0.6)
        paths = prepare_scene_clips(
            materials, tmp_path, params, production.timeline, plan
        )
    elapsed = 0.0
    for segment, path in zip(production.timeline, paths):
        assert segment["start"] == pytest.approx(elapsed, abs=0.01)
        elapsed += media_duration(path)
        assert segment["end"] == pytest.approx(elapsed, abs=0.01)
    assert elapsed == pytest.approx(0.6, abs=0.01)
