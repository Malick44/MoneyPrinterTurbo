"""Offline tests: never issue real SDK model turns or require account credentials."""

import json
import os
import signal
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from openai_codex import ApprovalMode, LocalImageInput, Sandbox, TextInput
from openai_codex.generated.v2_all import GetAccountResponse, ReasoningEffort
from pydantic import BaseModel, ConfigDict, Field

from app.intelligence._codex_launcher import main as launch_codex
from app.intelligence._codex_launcher import subscription_environment
from app.intelligence.errors import redact_secrets
from app.intelligence.runtime import (
    CodexAuthError,
    CodexRuntime,
    IntelligenceError,
    _strict_schema,
)


class Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1)
    score: float = Field(ge=0, le=10)


def sdk_client(account_type="chatgpt", response='{"title":"A plan","score":9}'):
    client = MagicMock()
    account = None if account_type is None else {"type": account_type}
    if account_type == "chatgpt":
        account.update(email="private@example.com", planType="plus")
    client.account.return_value = GetAccountResponse.model_validate(
        {"account": account, "requiresOpenaiAuth": True}
    )
    client._client.request.return_value.config.model_dump.return_value = {
        "mcp_servers": {"paid_media": {"enabled": True}, "local": {"enabled": True}}
    }
    client.thread_start.return_value.run.return_value = SimpleNamespace(
        status="completed", final_response=response
    )
    return client


@contextmanager
def connected(runtime, client):
    @contextmanager
    def fake_client():
        yield client, "/neutral-directory"

    with patch.object(runtime, "_client", fake_client):
        yield client


def test_environment_isolation_preserves_parent_and_subscription_location(monkeypatch):
    values = {
        "OPENAI_API_KEY": "openai-secret",
        "CODEX_API_KEY": "codex-secret",
        "OPENAI_BASE_URL": "https://custom.invalid",
        "CODEX_MODEL_PROVIDER": "custom",
        "AZURE_OPENAI_API_KEY": "azure-secret",
        "ANTHROPIC_API_KEY": "anthropic-secret",
        "OPENROUTER_API_KEY": "router-secret",
        "CODEX_HOME": "/existing/subscription",
        "HOME": "/original",
        "CODEX_CA_CERTIFICATE": "/ca.pem",
        "PATH": "/bin",
        "MPT_ENV": "legacy",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    before = dict(os.environ)
    child = subscription_environment()
    assert dict(os.environ) == before
    for key in values:
        if key.endswith("API_KEY") or key in (
            "OPENAI_BASE_URL",
            "CODEX_MODEL_PROVIDER",
        ):
            assert key not in child
        else:
            assert child[key] == values[key]


def test_launcher_removes_credentials_even_when_sdk_would_merge_them(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "legacy-api-key")
    monkeypatch.setenv("CODEX_API_KEY", "legacy-codex-key")
    with (
        patch("codex_cli_bin.bundled_codex_path", return_value=Path("/bundled/codex")),
        patch("sys.argv", ["launcher", "app-server", "--listen", "stdio://"]),
        patch("os.execve") as execute,
    ):
        launch_codex()
    binary, args, env = execute.call_args.args
    assert binary == "/bundled/codex"
    assert args == [binary, "app-server", "--listen", "stdio://"]
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env
    assert os.environ["OPENAI_API_KEY"] == "legacy-api-key"


@pytest.mark.parametrize("account_type", [None, "apiKey", "amazonBedrock"])
def test_missing_or_non_chatgpt_account_cannot_generate(account_type):
    runtime = CodexRuntime()
    client = sdk_client(account_type)
    with connected(runtime, client), pytest.raises(CodexAuthError) as error:
        runtime._run_sdk("write a script")
    assert error.value.stage == "codex_auth"
    assert "runtime login" in str(error.value)
    assert "API-key billing" in str(error.value)
    client.thread_start.assert_not_called()


def test_auth_metadata_never_exposes_email_or_starts_a_turn():
    runtime = CodexRuntime()
    client = sdk_client()
    with connected(runtime, client):
        status = runtime._auth_status_sdk()
    assert status["authenticated"] is True
    assert "private@example.com" not in json.dumps(status)
    client.account.assert_called_once_with(refresh_token=False)
    client.thread_start.assert_not_called()
    client.logout.assert_not_called()


def test_sdk_account_errors_are_not_logged_or_exposed(caplog):
    runtime = CodexRuntime()
    client = sdk_client()
    client.account.side_effect = RuntimeError(
        "access_token=private-token auth.json contents sk-private-key"
    )
    with connected(runtime, client):
        status = runtime._auth_status_sdk()
    assert status["authenticated"] is False
    assert "private-token" not in json.dumps(status) + caplog.text
    assert "sk-private-key" not in json.dumps(status) + caplog.text


def test_structured_generation_passes_schema_and_local_images(tmp_path):
    image = tmp_path / "contact-sheet.jpg"
    image.write_bytes(b"fixture image (SDK mocked)")
    runtime = CodexRuntime(
        model=" custom-model ", reasoning_effort="high", working_directory=tmp_path
    )
    client = sdk_client()
    with connected(runtime, client):
        result = runtime._run_sdk(
            "Review the selected scenes", _strict_schema(Result), [image.name]
        )
    assert Result.model_validate_json(result).score == 9
    start = client.thread_start.call_args.kwargs
    assert start["model"] == "custom-model" and start["model_provider"] == "openai"
    assert start["sandbox"] is Sandbox.read_only
    assert start["approval_mode"] is ApprovalMode.deny_all
    assert start["ephemeral"] is True
    assert start["cwd"] == "/neutral-directory"
    assert start["developer_instructions"] == start["base_instructions"]
    assert "Do not execute commands" in start["developer_instructions"]
    assert start["config"]["mcp_servers"] == {
        "paid_media": {"enabled": False},
        "local": {"enabled": False},
    }
    call = client.thread_start.return_value.run.call_args
    assert isinstance(call.args[0][0], TextInput)
    assert isinstance(call.args[0][1], LocalImageInput)
    assert call.args[0][1].path == str(image)
    assert call.kwargs["output_schema"]["additionalProperties"] is False
    assert call.kwargs["effort"] is ReasoningEffort.high


def test_blank_model_preserves_codex_default():
    runtime = CodexRuntime(model=" ")
    client = sdk_client(response="A useful script")
    with connected(runtime, client):
        assert runtime._run_sdk("write") == "A useful script"
    assert client.thread_start.call_args.kwargs["model"] is None


@pytest.mark.parametrize(
    "response", ['{"title":"valid","score":9}', '{"title":"valid","score":9.5}']
)
def test_structured_output_is_validated(response):
    runtime = CodexRuntime()
    with patch.object(runtime, "_request", return_value=response):
        assert isinstance(runtime.generate("Make a plan", Result), Result)


@pytest.mark.parametrize(
    "response",
    [
        '```json\n{"title":"valid","score":9}\n```',
        '{"title":"","score":9}',
        '{"title":"valid","score":11}',
        '{"title":"valid"}',
        '{"title":"valid","score":9,"surprise":true}',
        'prose {"title":"valid","score":9}',
    ],
)
def test_invalid_structured_output_has_safe_error(response):
    runtime = CodexRuntime()
    with (
        patch.object(runtime, "_request", return_value=response),
        pytest.raises(IntelligenceError, match="production contract"),
    ):
        runtime.generate("Make a plan", Result)


def test_nested_schema_defaults_are_strict_output_safe():
    class Nested(BaseModel):
        label: str = "default"

    class Outer(BaseModel):
        nested: Nested
        optional: str | None = None

    schema = _strict_schema(Outer)
    assert schema["required"] == ["nested", "optional"]
    assert schema["$defs"]["Nested"]["required"] == ["label"]
    assert schema["$defs"]["Nested"]["additionalProperties"] is False
    assert "default" not in schema["$defs"]["Nested"]["properties"]["label"]
    assert Outer.model_json_schema()["properties"]["optional"]["default"] is None


def test_failed_turn_does_not_return_partial_response():
    runtime = CodexRuntime()
    client = sdk_client()
    client.thread_start.return_value.run.return_value.status = "interrupted"
    with (
        connected(runtime, client),
        pytest.raises(IntelligenceError, match="did not complete"),
    ):
        runtime._run_sdk("write")


def test_sdk_error_does_not_leak_unrecognized_secret():
    runtime = CodexRuntime()
    client = sdk_client()
    client.thread_start.side_effect = RuntimeError("unrecognized-credential-value")
    with connected(runtime, client), pytest.raises(IntelligenceError) as error:
        runtime._run_sdk("write")
    assert "unrecognized-credential-value" not in str(error.value)
    assert error.value.__suppress_context__ is True


def test_missing_contact_sheet_does_not_reach_model():
    runtime = CodexRuntime()
    with (
        patch.object(runtime, "_client") as connection,
        pytest.raises(IntelligenceError, match="contact-sheet image is missing"),
    ):
        runtime._run_sdk("review", images=["/missing/contact-sheet.jpg"])
    connection.assert_not_called()


def test_worker_transport_cleans_env_and_only_returns_text(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "preserved-legacy-key")
    process = MagicMock()
    process.returncode = 0
    process.communicate.return_value = (
        json.dumps({"result": "A script"}),
        "private stderr",
    )
    with patch("app.intelligence.runtime.subprocess.Popen") as popen:
        popen.return_value.__enter__.return_value = process
        assert CodexRuntime().generate_text("write") == "A script"
    assert "OPENAI_API_KEY" not in popen.call_args.kwargs["env"]
    assert os.environ["OPENAI_API_KEY"] == "preserved-legacy-key"
    assert process.communicate.call_args.kwargs["timeout"] == 600


def test_worker_timeout_terminates_app_server_process_group():
    process = MagicMock(pid=123456)
    process.communicate.side_effect = [subprocess.TimeoutExpired("sdk", 45), ("", "")]
    with (
        patch("app.intelligence.runtime.subprocess.Popen") as popen,
        patch("app.intelligence.runtime.os.name", "posix"),
        patch("app.intelligence.runtime.os.killpg") as kill,
    ):
        popen.return_value.__enter__.return_value = process
        status = CodexRuntime().auth_status()
    assert status["authenticated"] is False
    assert "timed out" in status["message"]
    kill.assert_called_once_with(123456, signal.SIGKILL)
    assert process.communicate.call_args_list[0].kwargs["timeout"] == 45


def test_error_redaction_covers_auth_tokens_and_environment_credentials(monkeypatch):
    monkeypatch.setenv("CUSTOM_API_KEY", "an-unusual-secret")
    value = 'sk-example abc access_token="access-secret" refresh_token=refresh-secret id_token=id-secret Bearer bearer-secret https://u:password@host/path?api_key=query-secret an-unusual-secret'
    cleaned = redact_secrets(value)
    for secret in (
        "sk-example",
        "access-secret",
        "refresh-secret",
        "id-secret",
        "bearer-secret",
        "u:password",
        "query-secret",
        "an-unusual-secret",
    ):
        assert secret not in cleaned
    assert "abc" in cleaned


def test_runtime_launch_always_forces_subscription_and_official_provider():
    runtime = CodexRuntime()
    with patch("openai_codex.Codex") as sdk:
        with runtime._client():
            pass
    args = sdk.call_args.args[0].launch_args_override
    assert 'forced_login_method="chatgpt"' in args
    assert 'model_provider="openai"' in args
    assert 'openai_base_url="https://chatgpt.com/backend-api/codex"' in args
    assert 'chatgpt_base_url="https://chatgpt.com/backend-api"' in args
    assert "features.image_generation=false" in args
    assert "features.shell_tool=false" in args
    assert "features.plugins=false" in args
    assert "features.hooks=false" in args
    assert 'developer_instructions=""' in args
    assert 'instructions=""' in args
    assert any(arg.startswith("model_instructions_file=") for arg in args)


def test_auth_url_and_trace_environment_overrides_are_removed():
    keys = (
        "CODEX_REFRESH_TOKEN_URL_OVERRIDE",
        "CODEX_REVOKE_TOKEN_URL_OVERRIDE",
        "CODEX_ACCESS_TOKEN",
        "CODEX_ROLLOUT_TRACE_ROOT",
        "CODEX_TUI_SESSION_LOG_PATH",
        "CODEX_FUTURE_AUTH_OVERRIDE",
    )
    source = {key: "must-not-reach-child" for key in keys}
    source.update(CODEX_HOME="/saved/auth", CODEX_CA_CERTIFICATE="/trusted/ca.pem")
    assert subscription_environment(source) == {
        "CODEX_HOME": "/saved/auth",
        "CODEX_CA_CERTIFICATE": "/trusted/ca.pem",
    }


def test_explicit_newer_codex_binary_uses_sdk_supported_override(tmp_path, monkeypatch):
    binary = tmp_path / "codex"
    binary.write_text("placeholder")
    binary.chmod(0o700)
    monkeypatch.setenv("MPT_CODEX_BIN", str(binary))
    with patch("openai_codex.Codex") as sdk:
        with CodexRuntime()._client():
            pass
    assert sdk.call_args.args[0].codex_bin == str(binary)


def test_invalid_explicit_binary_does_not_silently_fall_back(monkeypatch):
    monkeypatch.setenv("MPT_CODEX_BIN", "/missing/codex")
    with (
        patch("openai_codex.Codex") as sdk,
        pytest.raises(CodexAuthError, match="MPT_CODEX_BIN"),
    ):
        with CodexRuntime()._client():
            pass
    sdk.assert_not_called()


def test_model_runtime_compatibility_failure_is_actionable_without_fallback():
    runtime = CodexRuntime()
    client = sdk_client()
    client.thread_start.return_value.run.side_effect = RuntimeError(
        "The model requires a newer version of Codex"
    )
    with (
        connected(runtime, client),
        pytest.raises(IntelligenceError, match="MPT_CODEX_BIN") as error,
    ):
        runtime._run_sdk("write")
    assert "no model fallback" in str(error.value)
    client.thread_start.assert_called_once()


def test_expired_subscription_reports_codex_auth_without_credentials():
    runtime = CodexRuntime()
    client = sdk_client()
    client.thread_start.return_value.run.side_effect = RuntimeError(
        "401 Unauthorized refresh_token=private-credential"
    )
    with connected(runtime, client), pytest.raises(CodexAuthError) as error:
        runtime._run_sdk("write")
    assert error.value.stage == "codex_auth"
    assert "private-credential" not in str(error.value)
