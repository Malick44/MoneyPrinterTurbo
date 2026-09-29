from unittest.mock import patch

from app.intelligence.runtime import CodexAuthError
from app.models.llm_provider import get_llm_provider
from app.services import llm


def test_codex_provider_metadata_is_subscription_only():
    spec = get_llm_provider("codex")
    assert spec.default_label == "Codex (ChatGPT subscription)"
    assert spec.adapter == "codex"
    assert not spec.requires_api_key and not spec.show_api_key
    assert not spec.requires_base_url and not spec.show_base_url
    assert not spec.requires_model_name
    assert spec.default_model == spec.default_base_url == spec.api_key_url == ""
    assert spec.resolve_model_name("") == ""


def test_generic_llm_adapter_calls_codex_without_api_client():
    with (
        patch("app.intelligence.runtime.CodexRuntime") as runtime,
        patch.object(llm, "OpenAI") as api,
    ):
        runtime.return_value.generate_text.return_value = "A useful script"
        output = llm._generate_response(
            "Rewrite this",
            app_config={
                "llm_provider": "codex",
                "codex_model_name": "",
                "codex_reasoning_effort": "high",
                "codex_api_key": "ignored-key",
                "codex_base_url": "https://ignored.invalid",
            },
        )
    assert output == "A useful script"
    runtime.assert_called_once_with(model=None, reasoning_effort="high")
    runtime.return_value.generate_text.assert_called_once_with("Rewrite this")
    api.assert_not_called()


def test_auth_failure_never_falls_back_to_paid_api():
    with (
        patch("app.intelligence.runtime.CodexRuntime") as runtime,
        patch.object(llm, "OpenAI") as api,
    ):
        runtime.return_value.generate_text.side_effect = CodexAuthError()
        output = llm._generate_response(
            "Rewrite",
            app_config={"llm_provider": "codex", "openai_api_key": "paid-key"},
        )
    assert output.startswith("Error:")
    assert "ChatGPT" in output and "runtime login" in output
    api.assert_not_called()


def test_social_metadata_can_use_codex_provider():
    with (
        patch.dict(llm.config.app, {"llm_provider": "codex"}),
        patch("app.intelligence.runtime.CodexRuntime") as runtime,
    ):
        runtime.return_value.generate_text.return_value = (
            '{"title":"Coffee","caption":"A fresh cup","hashtags":["#coffee"]}'
        )
        result = llm.generate_social_metadata("Coffee", "A fresh cup.")
    assert result["title"] == "Coffee" and result["hashtags"] == ["#coffee"]
    runtime.return_value.generate_text.assert_called_once()
