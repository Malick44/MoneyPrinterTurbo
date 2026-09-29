import json
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.models.schema import TaskVideoRequest


WEBUI_MAIN = Path(__file__).resolve().parents[2] / "webui" / "Main.py"


def _widget(elements, key):
    return next(
        item
        for item in elements
        if str(item.key) == key or str(item.key).startswith(f"{key}_")
    )


def _new_app(*, authenticated=True, settings=False):
    app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
    app.session_state["ui_language"] = "en"
    if authenticated is not None:
        app.session_state["codex_subscription_auth_status"] = {
            "authenticated": authenticated,
            "message": "Sign in with ChatGPT to use Codex.",
        }
    if settings:
        app.session_state["settings_dialog_open"] = True
        app.session_state["settings_dialog_target_tab"] = "llm"
    app.run()
    assert not app.exception
    return app


def test_webui_constructs_and_exports_codex_settings_and_preserves_them():
    app_settings = dict(
        config.app, production_intelligence="legacy", video_source="pexels"
    )
    with (
        patch.object(config, "app", app_settings),
        patch.object(config, "ui", dict(config.ui, language="en")),
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = _new_app()
        _widget(app.selectbox, "production_intelligence_select").set_value(
            "codex"
        ).run()
        _widget(app.text_input, "production_codex_model_name_input").set_value("")
        _widget(app.selectbox, "codex_reasoning_effort_select").set_value("high")
        _widget(app.checkbox, "production_codex_review_enabled_input").set_value(False)
        _widget(
            app.checkbox, "production_codex_material_review_enabled_input"
        ).set_value(False)
        _widget(app.checkbox, "production_codex_render_review_enabled_input").set_value(
            False
        )
        _widget(app.slider, "production_codex_quality_threshold_input").set_value(9.0)
        _widget(app.slider, "production_codex_max_repair_passes_input").set_value(1)
        app.run()
        assert not app.exception
        params = TaskVideoRequest(video_subject="A small garden")
        assert params.production_intelligence == "codex"
        assert params.codex_model_name == ""
        assert params.codex_reasoning_effort == "high"
        assert params.codex_review_enabled is False
        assert params.codex_material_review_enabled is False
        assert params.codex_render_review_enabled is False
        assert params.codex_quality_threshold == 9.0
        assert params.codex_max_repair_passes == 1
        second = _new_app()
        assert (
            _widget(second.selectbox, "production_intelligence_select").value == "codex"
        )
        assert (
            _widget(second.slider, "production_codex_max_repair_passes_input").value
            == 1
        )
        _widget(second.selectbox, "production_intelligence_select").set_value(
            "legacy"
        ).run()
        assert app_settings["production_intelligence"] == "legacy"
        assert not any(
            "production_codex_model_name_input" == item.key
            for item in second.text_input
        )


def test_codex_provider_has_subscription_status_optional_model_and_local_login():
    app_settings = dict(
        config.app,
        llm_provider="codex",
        production_intelligence="legacy",
        codex_model_name="",
    )
    with (
        patch.object(config, "app", app_settings),
        patch.object(config, "ui", dict(config.ui, language="en")),
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = _new_app(authenticated=False, settings=True)
        assert _widget(app.text_input, "codex_model_name_input").value == ""
        assert not any(
            item.key in {"codex_api_key_input", "codex_base_url_custom_input"}
            for item in app.text_input
        )
        assert any(
            "uv run python -m app.intelligence.runtime login" in item.value
            for item in app.code
        )
        assert any("--device-auth" in item.value for item in app.caption)
        assert "codex_api_key" not in app_settings
        assert "codex_base_url" not in app_settings


def test_codex_preset_restore_updates_controls_and_exported_settings():
    app_settings = dict(config.app, production_intelligence="legacy")
    with (
        patch.object(config, "app", app_settings),
        patch.object(config, "ui", dict(config.ui, language="en")),
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = _new_app()
        app.session_state["settings_preset_payload"] = json.loads(
            TaskVideoRequest(
                video_subject="A small garden",
                production_intelligence="codex",
                codex_model_name="test-model",
                codex_reasoning_effort="xhigh",
                codex_quality_threshold=9.4,
                codex_max_repair_passes=0,
            ).model_dump_json()
        )
        app.run()
        assert not app.exception
        assert _widget(app.selectbox, "production_intelligence_select").value == "codex"
        assert (
            _widget(app.text_input, "production_codex_model_name_input").value
            == "test-model"
        )
        assert (
            _widget(app.slider, "production_codex_quality_threshold_input").value == 9.4
        )
        assert (
            _widget(app.slider, "production_codex_max_repair_passes_input").value == 0
        )


def test_authentication_is_lazy_and_refreshes_after_local_login():
    with (
        patch.object(config, "app", dict(config.app, production_intelligence="legacy")),
        patch.object(config, "ui", dict(config.ui, language="en")),
        patch.object(config, "try_save_config", return_value=True),
        patch(
            "app.intelligence.runtime.CodexRuntime.auth_status",
            side_effect=[
                {"authenticated": False, "message": "Sign in with ChatGPT."},
                {"authenticated": True, "message": "Connected."},
            ],
        ) as auth_status,
    ):
        app = _new_app(authenticated=None)
        auth_status.assert_not_called()
        _widget(app.selectbox, "production_intelligence_select").set_value(
            "codex"
        ).run()
        auth_status.assert_called_once()
        _widget(app.button, "codex_auth_refresh_production").click().run()
        assert auth_status.call_count == 2
        assert any("connected with ChatGPT" in item.value for item in app.success)
        assert not app.exception


def test_provider_and_production_model_widgets_stay_synchronized():
    app_settings = dict(
        config.app,
        llm_provider="codex",
        production_intelligence="codex",
        codex_model_name="",
    )
    with (
        patch.object(config, "app", app_settings),
        patch.object(config, "ui", dict(config.ui, language="en")),
        patch.object(config, "try_save_config", return_value=True),
    ):
        app = _new_app(settings=True)
        _widget(app.text_input, "codex_model_name_input").set_value(
            "provider-model"
        ).run()
        assert not app.exception
        assert (
            _widget(app.text_input, "production_codex_model_name_input").value
            == "provider-model"
        )
        assert app_settings["codex_model_name"] == "provider-model"
        _widget(app.text_input, "production_codex_model_name_input").set_value(
            "production-model"
        ).run()
        assert not app.exception
        assert (
            _widget(app.text_input, "codex_model_name_input").value
            == "production-model"
        )
        assert app_settings["codex_model_name"] == "production-model"


def test_codex_generation_submits_without_a_legacy_llm_api_key():
    app_settings = dict(
        config.app,
        production_intelligence="codex",
        llm_provider="openai",
        openai_api_key="",
        video_source="pexels",
        pexels_api_keys=["mock-stock-key"],
        codex_model_name="",
        codex_reasoning_effort="high",
        codex_max_repair_passes=1,
    )
    with (
        patch.object(config, "app", app_settings),
        patch.object(
            config, "ui", dict(config.ui, language="en", voice_mode="none", bgm_type="")
        ),
        patch.object(config, "try_save_config", return_value=True),
        patch("app.services.webui_task.submit_generation") as submit,
        patch("app.services.llm.generate_script") as legacy_script,
    ):
        app = _new_app()
        _widget(app.text_area, "video_subject").set_value("A small garden").run()
        _widget(app.button, "generate_video_button").click().run()
        assert not app.exception
        submit.assert_called_once()
        legacy_script.assert_not_called()
        params = submit.call_args.kwargs["params"]
        assert params.production_intelligence == "codex"
        assert params.video_subject == "A small garden"
        assert params.codex_model_name == ""
        assert params.codex_reasoning_effort == "high"
        assert params.codex_max_repair_passes == 1
