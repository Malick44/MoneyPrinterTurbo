import math
from unittest.mock import patch

import pytest
from pydantic import ValidationError

import cli
from app.config import config
from app.models.schema import (
    ProductionIntelligenceSettings,
    TaskVideoRequest,
    VideoParams,
    get_production_intelligence_settings,
)


def test_legacy_defaults_remain_compatible_without_codex_configuration():
    with patch.object(config, "app", {}):
        params = VideoParams(video_subject="A small garden")
    assert params.production_intelligence == "legacy"
    assert params.codex_model_name == ""
    assert params.codex_quality_threshold == 8.5
    assert params.codex_max_repair_passes == 2
    assert params.codex_review_enabled is True
    assert params.codex_material_review_enabled is True
    assert params.codex_render_review_enabled is True


def test_api_request_honors_live_config_but_explicit_settings_take_priority():
    with patch.object(
        config,
        "app",
        {
            "production_intelligence": "codex",
            "codex_model_name": " local-model ",
            "codex_reasoning_effort": "high",
            "codex_max_repair_passes": 3,
        },
    ):
        configured = TaskVideoRequest(video_subject="A small garden")
        overridden = TaskVideoRequest(
            video_subject="A small garden",
            production_intelligence="legacy",
            codex_model_name="",
            codex_max_repair_passes=0,
        )
    assert configured.production_intelligence == "codex"
    assert configured.codex_model_name == "local-model"
    assert configured.codex_reasoning_effort == "high"
    assert overridden.production_intelligence == "legacy"
    assert overridden.codex_model_name == ""
    assert overridden.codex_max_repair_passes == 0


@pytest.mark.parametrize(
    "field,value",
    [
        ("production_intelligence", "openai"),
        ("codex_reasoning_effort", "unlimited"),
        ("codex_quality_threshold", -0.1),
        ("codex_quality_threshold", 10.1),
        ("codex_quality_threshold", math.nan),
        ("codex_quality_threshold", math.inf),
        ("codex_max_repair_passes", -1),
        ("codex_max_repair_passes", 11),
        ("codex_max_repair_passes", 1.5),
    ],
)
def test_api_rejects_invalid_intelligence_values(field, value):
    with pytest.raises(ValidationError):
        TaskVideoRequest(video_subject="test", **{field: value})


def test_saved_config_rejects_invalid_fields_without_discarding_valid_settings():
    settings = get_production_intelligence_settings(
        {
            "production_intelligence": "codex",
            "codex_model_name": None,
            "codex_quality_threshold": math.nan,
            "codex_max_repair_passes": 1000000,
            "codex_reasoning_effort": "high",
            "openai_api_key": "not-part-of-settings",
        }
    )
    assert settings.production_intelligence == "codex"
    assert settings.codex_model_name == ""
    assert settings.codex_quality_threshold == 8.5
    assert settings.codex_max_repair_passes == 2
    assert settings.codex_reasoning_effort == "high"
    assert "not-part-of-settings" not in settings.model_dump_json()


def test_cli_constructs_all_codex_intelligence_options_without_authentication():
    args = cli.parse_args(
        [
            "--video-subject",
            "A small garden",
            "--production-intelligence",
            "codex",
            "--codex-model-name",
            "",
            "--codex-reasoning-effort",
            "high",
            "--no-codex-review-enabled",
            "--no-codex-material-review-enabled",
            "--no-codex-render-review-enabled",
            "--codex-quality-threshold",
            "9.2",
            "--codex-max-repair-passes",
            "1",
            "--stop-at",
            "script",
        ]
    )
    with patch.object(config, "ui", {}), patch.object(config, "app", {}):
        params = cli.build_video_params(args)
    assert params.production_intelligence == "codex"
    assert params.codex_model_name == ""
    assert params.codex_reasoning_effort == "high"
    assert params.codex_review_enabled is False
    assert params.codex_material_review_enabled is False
    assert params.codex_render_review_enabled is False
    assert params.codex_quality_threshold == 9.2
    assert params.codex_max_repair_passes == 1


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--codex-quality-threshold", "nan"),
        ("--codex-quality-threshold", "11"),
        ("--codex-max-repair-passes", "-1"),
        ("--codex-max-repair-passes", "11"),
        ("--codex-max-repair-passes", "1.5"),
    ],
)
def test_cli_rejects_unbounded_or_invalid_review_settings(flag, value):
    with pytest.raises(SystemExit) as exc:
        cli.parse_args(["--video-subject", "test", flag, value])
    assert exc.value.code == 2


def test_intelligence_settings_round_trip_through_api_schema():
    expected = ProductionIntelligenceSettings(production_intelligence="codex")
    request = TaskVideoRequest(video_subject="test", **expected.model_dump())
    restored = TaskVideoRequest.model_validate_json(request.model_dump_json())
    for field in ProductionIntelligenceSettings.model_fields:
        assert getattr(restored, field) == getattr(expected, field)
    schema = TaskVideoRequest.model_json_schema()
    assert schema["properties"]["production_intelligence"]["enum"] == [
        "legacy",
        "codex",
    ]
    assert "codex_api_key" not in schema["properties"]
