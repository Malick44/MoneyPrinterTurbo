"""Keep baseline tests independent of the workflow selected in the local WebUI."""

import pytest

from app.config import config


@pytest.fixture(scope="session", autouse=True)
def isolate_configuration_writes(tmp_path_factory):
    # Some UI controls queue a deferred save. Keep that real save mechanism
    # exercised without ever replacing the developer's live config.toml.
    target = tmp_path_factory.mktemp("mpt-test-config") / "config.toml"
    target.write_text("", encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(config, "config_file", str(target))
        yield


@pytest.fixture(autouse=True)
def isolate_default_production_workflow(monkeypatch):
    # Existing media tests exercise the legacy pipeline and mock its providers.
    # A developer's saved Codex selection must not turn those tests into live
    # subscription requests. Codex/configuration tests opt in explicitly.
    monkeypatch.setitem(config.app, "production_intelligence", "legacy")
