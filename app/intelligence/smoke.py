"""Explicit local smoke paths: offline media pipeline, or one live Codex turn.

Run with ``uv run python -m app.intelligence.smoke --offline`` (no accounts), or
``--live`` after ChatGPT login to verify the SDK, schema and local-image boundary.
"""

from __future__ import annotations

import argparse
import json
import tempfile
import wave
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import BaseModel, Field

from app.intelligence.contracts import (
    EditedScript,
    ProductionPlan,
    ProductionReview,
    ScenePlan,
    VisualPlan,
)
from app.intelligence.runtime import CodexRuntime


class _ImageCheck(BaseModel):
    dominant_color: str
    explanation: str = Field(min_length=1)


def live_smoke(model=None) -> dict:
    """One explicitly requested subscription turn; no media-generation providers."""
    runtime = CodexRuntime(model=model)
    status = runtime.auth_status()
    if not status["authenticated"]:
        raise RuntimeError(status["message"])
    with tempfile.TemporaryDirectory(prefix="mpt-codex-image-smoke-") as directory:
        image = Path(directory) / "red-card.png"
        Image.new("RGB", (128, 128), "red").save(image)
        result = runtime.generate(
            "Inspect the attached image. Identify its dominant color in English and explain in one short sentence. Do not use tools.",
            _ImageCheck,
            images=[str(image)],
        )
    if "red" not in result.dominant_color.lower():
        raise RuntimeError(
            "SDK returned valid JSON but did not identify the local image correctly"
        )
    return {
        "mode": "live",
        "authentication": "chatgpt",
        "structured_output": True,
        "local_image": True,
    }


def offline_smoke() -> dict:
    """Exercise real request construction and deterministic media stages locally."""
    from app.config import config

    # Set isolation before importing state, queue or provider modules; their
    # constructors otherwise inherit saved Redis/provider configuration.
    with patch.dict(
        config.app,
        {"enable_redis": False, "api_key": "", "production_intelligence": "legacy"},
        clear=True,
    ):
        return _offline_smoke()


def _offline_smoke() -> dict:
    import cli
    from fastapi.testclient import TestClient

    from app import asgi
    from app.config import config
    from app.controllers.v1 import video as controller
    from app.intelligence import pipeline
    from app.models import const
    from app.models.schema import (
        MaterialInfo,
        VideoAspect,
        VideoParams,
        get_production_intelligence_settings,
    )
    from app.services import task, video
    from app.services import state as sm
    from app.utils import utils

    plan = ProductionPlan(
        title="Colors",
        summary="Two local scenes",
        scenes=[
            ScenePlan(
                scene_id=color,
                narration=f"This is {color}.",
                purpose=f"Introduce {color}",
                target_duration=0.5,
                visual_intent=f"A {color} card",
                preferred_visual_type="local_asset",
            )
            for color in ("red", "blue")
        ],
    )
    calls = []

    def fake_generate(_runtime, prompt, output_type, images=()):
        calls.append(output_type.__name__)
        if output_type is ProductionPlan:
            return plan.model_copy(deep=True)
        if output_type is EditedScript:
            return EditedScript(
                scenes=[
                    {"scene_id": scene.scene_id, "narration": scene.narration}
                    for scene in plan.scenes
                ]
            )
        if output_type is VisualPlan:
            return VisualPlan(
                scenes=[
                    {
                        key: value
                        for key, value in scene.model_dump().items()
                        if key not in ("narration", "purpose", "target_duration")
                    }
                    for scene in plan.scenes
                ]
            )
        if output_type is ProductionReview:
            stage = (
                "material_review"
                if "Role: MaterialReviewer" in prompt
                else "render_review"
                if "Role: RenderReviewer" in prompt
                else "plan_review"
            )
            if stage != "plan_review":
                assert images and all(Path(image).is_file() for image in images)
            return ProductionReview(
                stage=stage,
                score=9.5,
                approved=True,
                summary="Smoke review accepted",
                issues=[],
            )
        raise AssertionError(f"Unexpected smoke role {output_type}")

    settings = get_production_intelligence_settings(
        {"production_intelligence": "codex"}
    )
    assert settings.production_intelligence == "codex"
    cli_params = cli.build_video_params(
        cli.parse_args(
            [
                "--video-subject",
                "Colors",
                "--production-intelligence",
                "codex",
                "--codex-max-repair-passes",
                "1",
            ]
        )
    )
    assert cli_params.codex_max_repair_passes == 1
    with tempfile.TemporaryDirectory(prefix="mpt-production-smoke-") as directory:
        root = Path(directory)

        def task_dir(task_id=""):
            result = root / str(task_id)
            result.mkdir(parents=True, exist_ok=True)
            return str(result)

        def storage_dir(sub_dir="", create=False):
            result = root if sub_dir == "local_videos" else root / sub_dir
            if create:
                result.mkdir(parents=True, exist_ok=True)
            return str(result)

        materials = []
        for color in ("red", "blue"):
            image = root / f"{color}.png"
            Image.new("RGB", (640, 640), color).save(image)
            materials.append(MaterialInfo(provider="local", url=str(image)))
        audio = root / "narration.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\0\0" * 16000)
        params = VideoParams(
            video_subject="Colors",
            video_script=plan.narration,
            production_intelligence="codex",
            video_source="local",
            video_materials=materials,
            custom_audio_file=str(audio),
            subtitle_enabled=False,
            bgm_type="",
            n_threads=1,
            video_clip_duration=1,
            video_concat_mode="sequential",
            video_fit_mode="contain",
        )
        with (
            patch.object(utils, "task_dir", side_effect=task_dir),
            patch.object(utils, "storage_dir", side_effect=storage_dir),
            patch.object(sm, "state", sm.MemoryState()),
            patch.object(VideoAspect, "to_resolution", return_value=(160, 90)),
            patch.object(video, "fps", 10),
            patch.object(CodexRuntime, "generate", fake_generate),
            patch.object(
                CodexRuntime,
                "auth_status",
                return_value={"authenticated": True, "message": "Offline mock"},
            ),
            patch.object(
                task.upload_post.upload_post_service,
                "is_configured",
                return_value=False,
            ),
            patch.dict(
                config.app, {"api_key": "", "production_intelligence": "legacy"}
            ),
            patch.object(controller.task_manager, "add_task") as queue,
        ):
            # This enters the actual FastAPI request parser and queue boundary.
            response = TestClient(asgi.app).post(
                "/api/v1/videos", json=params.model_dump(mode="json")
            )
            assert response.status_code == 200, response.text
            assert queue.call_args.kwargs["params"].production_intelligence == "codex"
            result = task.start("codex", params, allow_server_file_input=True)
            assert result and result.get("state") != const.TASK_STATE_FAILED, result
            assert result.get("videos") and Path(result["videos"][0]).is_file(), result
            assert "ProductionPlan" in calls and "ProductionReview" in calls
            evidence = [
                "production-brief.json",
                "production-plan.json",
                "production-review.json",
                "material-review.json",
                "render-review.json",
                "repair-history.json",
                "materials-contact-sheet.jpg",
                "render-contact-sheet.jpg",
            ]
            assert all((root / "codex" / name).is_file() for name in evidence)
            legacy = params.model_copy(update={"production_intelligence": "legacy"})
            before = len(calls)
            with patch.object(
                pipeline,
                "CodexRuntime",
                side_effect=AssertionError("Legacy must not construct Codex"),
            ):
                legacy_result = task.start(
                    "legacy", legacy, allow_server_file_input=True
                )
            assert (
                legacy_result.get("videos")
                and Path(legacy_result["videos"][0]).is_file()
            ), legacy_result
            assert len(calls) == before
    return {
        "mode": "offline",
        "webui_settings": True,
        "api_settings": True,
        "cli_settings": True,
        "codex_boundary": True,
        "reviews_and_contact_sheets": True,
        "legacy_video": True,
        "external_ai_calls": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--offline", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    result = live_smoke(args.model) if args.live else offline_smoke()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
