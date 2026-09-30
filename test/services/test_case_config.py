"""Case configuration reaches runtime adapters and shares the search API budget."""

from fastapi.testclient import TestClient

from app.services.targeted_search.settings import Settings


def test_local_alignment_configuration_reaches_media_adapter():
    from app.services.targeted_search.media import setting
    from types import SimpleNamespace

    settings = Settings.from_mapping(
        {
            "targeted_search_whisperx_alignment_checkpoint": "/local/alignment-model",
            "targeted_search_whisperx_language": "en",
        }
    )
    workspace = SimpleNamespace(settings=settings)
    assert (
        setting(workspace, "whisperx_alignment_checkpoint", "")
        == "/local/alignment-model"
    )
    assert setting(workspace, "whisperx_language", "") == "en"


def test_case_api_uses_same_authenticated_request_budget(monkeypatch):
    from app import asgi
    from app.controllers.v1 import cases
    from types import SimpleNamespace

    monkeypatch.setitem(asgi.config.app, "targeted_search_api_requests_per_minute", 2)
    monkeypatch.setitem(asgi.config.app, "api_key", "")
    monkeypatch.setitem(asgi.config.app, "api_keys", [])
    monkeypatch.setattr(
        cases, "get_workspace", lambda: SimpleNamespace(list_cases=lambda: [])
    )
    client = TestClient(asgi.get_application())
    assert client.get("/api/v1/cases").status_code == 200
    assert client.get("/api/v1/cases").status_code == 200
    response = client.get("/api/v1/cases")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
