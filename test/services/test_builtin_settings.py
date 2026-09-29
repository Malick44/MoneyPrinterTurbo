import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import cli
from app.config import config
from app.models.schema import TaskVideoRequest, VideoParams


def test_builtin_api_accepts_local_generation_without_assets_or_keys():
    with patch.object(config, "app", {}):
        params = TaskVideoRequest(
            video_subject="Explain a three-step process",
            production_intelligence="codex",
            video_source="builtin",
        )
    assert params.video_materials is None
    assert params.production_intelligence == "codex"
    assert (
        "builtin"
        in TaskVideoRequest.model_json_schema()["properties"]["video_source"][
            "description"
        ]
    )


def test_builtin_api_requires_codex_intelligence():
    with (
        patch.object(config, "app", {}),
        pytest.raises(ValidationError, match="requires production_intelligence=codex"),
    ):
        VideoParams(video_subject="A process", video_source="builtin")


@pytest.mark.parametrize(
    "asset",
    [
        {"provider": "pexels", "url": "screenshot.png"},
        {"provider": "local", "url": "https://example.org/screenshot.png"},
        {"provider": "local", "url": "recording.mp4"},
    ],
)
def test_builtin_api_rejects_non_local_screenshot_assets(asset):
    with pytest.raises(ValidationError, match="local PNG"):
        VideoParams(
            video_subject="A process",
            production_intelligence="codex",
            video_source="builtin",
            video_materials=[asset],
        )


def test_builtin_cli_stages_optional_screenshots_in_managed_storage(tmp_path):
    screenshot = tmp_path / "screen.PNG"
    screenshot.write_bytes(b"local screenshot fixture")
    storage = tmp_path / "storage" / "local_videos"
    args = cli.parse_args(
        [
            "--video-subject",
            "A process",
            "--production-intelligence",
            "codex",
            "--video-source",
            "builtin",
            "--video-materials",
            str(screenshot),
            "--no-subtitle-enabled",
            "--bgm-type",
            "none",
        ]
    )
    with patch.object(config, "ui", {}), patch.object(config, "app", {}):
        params = cli.build_video_params(args)
    with patch("app.utils.utils.storage_dir", return_value=str(storage)):
        cli.prepare_cli_files(params, "video")
    asset = params.video_materials[0]
    assert asset.provider == "local"
    assert Path(asset.url).parent == storage
    assert Path(asset.url).read_bytes() == screenshot.read_bytes()


def test_builtin_cli_accepts_no_screenshot_and_uses_saved_codex_default():
    args = cli.parse_args(["--video-subject", "A process", "--video-source", "builtin"])
    with (
        patch.object(config, "ui", {}),
        patch.object(config, "app", {"production_intelligence": "codex"}),
    ):
        params = cli.build_video_params(args)
    assert params.video_source == "builtin"
    assert params.video_materials is None


def test_builtin_cli_rejects_explicit_legacy_mode():
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--video-subject",
                "A process",
                "--production-intelligence",
                "legacy",
                "--video-source",
                "builtin",
            ]
        )


def test_builtin_batch_resolves_screenshots_relative_to_manifest(tmp_path):
    screenshot = tmp_path / "screen.jpg"
    screenshot.write_bytes(b"local screenshot fixture")
    manifest = tmp_path / "tasks.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "video_subject": "Explain this screen",
                    "video_source": "builtin",
                    "production_intelligence": "codex",
                    "subtitle_enabled": False,
                    "bgm_type": "",
                    "video_materials": [{"provider": "local", "url": "screen.jpg"}],
                }
            ]
        )
    )
    storage = tmp_path / "storage" / "local_videos"
    args = cli.parse_args(["--batch-file", str(manifest)])
    with (
        patch.object(config, "ui", {}),
        patch.object(config, "app", {}),
        patch("app.utils.utils.storage_dir", return_value=str(storage)),
    ):
        params = cli._build_batch_tasks(args)[0]
    assert Path(params.video_materials[0].url).parent == storage
    assert Path(params.video_materials[0].url).read_bytes() == screenshot.read_bytes()
