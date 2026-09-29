from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from app.config import config
from app.services import voice


WEBUI_MAIN = Path(__file__).resolve().parents[2] / "webui" / "Main.py"


def _widget(elements, key):
    return next(
        item
        for item in elements
        if str(item.key) == key or str(item.key).startswith(f"{key}_")
    )


@contextmanager
def _builtin_app(
    *, production_intelligence="codex", materials=None, script_backend="local"
):
    settings = dict(
        config.app,
        production_intelligence=production_intelligence,
        video_source="builtin",
        llm_provider="openai",
        openai_api_key="",
        pexels_api_keys=[],
        pixabay_api_keys=[],
        coverr_api_keys=[],
        script_generation_backend=script_backend,
    )
    with (
        patch.object(config, "app", settings),
        patch.object(
            config, "ui", dict(config.ui, language="en", voice_mode="none", bgm_type="")
        ),
        patch.object(config, "try_save_config", return_value=True),
        patch("app.services.webui_task.submit_generation") as submit,
        patch("app.services.material.download_videos") as download,
        patch("app.services.material.generate_images_openai") as images,
        patch("app.services.volcengine_seedance.generate_videos") as ai_video,
    ):
        app = AppTest.from_file(str(WEBUI_MAIN), default_timeout=60)
        app.session_state["ui_language"] = "en"
        app.session_state["codex_subscription_auth_status"] = {"authenticated": True}
        if materials is not None:
            app.session_state["local_video_materials"] = materials
        app.run()
        assert not app.exception
        yield app, submit
        download.assert_not_called()
        images.assert_not_called()
        ai_video.assert_not_called()


def test_builtin_webui_submits_without_assets_or_provider_api_keys():
    with _builtin_app() as (app, submit):
        _widget(app.text_area, "video_subject").set_value(
            "Explain a three-step process"
        ).run()
        _widget(app.button, "generate_video_button").click().run()
        assert not app.exception
        submit.assert_called_once()
        params = submit.call_args.kwargs["params"]
        assert params.video_source == "builtin"
        assert params.video_materials is None
        assert params.production_intelligence == "codex"
        assert params.voice_name == voice.NO_VOICE_NAME
        assert any("full video and audio" in caption.value for caption in app.caption)
        assert not app.error


def test_builtin_webui_prevents_legacy_dispatch():
    with _builtin_app(production_intelligence="legacy") as (app, submit):
        _widget(app.text_area, "video_subject").set_value("A process").run()
        _widget(app.button, "generate_video_button").click().run()
        submit.assert_not_called()
        assert any("Choose Codex" in error.value for error in app.error)


def test_builtin_webui_reuses_only_uploaded_screenshot_images():
    assets = [
        {"provider": "local", "url": "/tmp/screenshot.PNG", "duration": 0},
        {"provider": "local", "url": "/tmp/video.mp4", "duration": 0},
    ]
    with _builtin_app(materials=assets) as (app, submit):
        _widget(app.text_area, "video_subject").set_value(
            "Explain this screenshot"
        ).run()
        _widget(app.button, "generate_video_button").click().run()
        assert not app.exception
        submit.assert_called_once()
        assert [
            asset.url for asset in submit.call_args.kwargs["params"].video_materials
        ] == ["/tmp/screenshot.PNG"]


def test_builtin_manual_script_actions_use_codex_even_with_saved_paid_backend():
    with (
        _builtin_app(script_backend="loomloom") as (app, submit),
        patch(
            "app.services.llm.generate_script",
            return_value="A clear three-step process.",
        ) as script,
        patch("app.services.llm.generate_terms", return_value=["three steps"]) as terms,
    ):
        _widget(app.text_area, "video_subject").set_value("Explain a process").run()
        _widget(app.button, "auto_generate_script").click().run()
        assert not app.exception
        assert script.call_args.kwargs["app_config"]["llm_provider"] == "codex"
        assert terms.call_args.kwargs["app_config"]["llm_provider"] == "codex"
        _widget(app.button, "auto_generate_terms").click().run()
        assert terms.call_count == 2
        assert terms.call_args.kwargs["app_config"]["llm_provider"] == "codex"
        assert config.app["llm_provider"] == "openai"
        assert config.app["script_generation_backend"] == "loomloom"
        submit.assert_not_called()
