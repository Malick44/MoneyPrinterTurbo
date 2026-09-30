"""Documentary writing across authenticated API, CLI and durable workers."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.controllers.v1 import cases as controller
from app.models.search import SearchError
from app.services import llm
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.cli import parser, run
from app.services.targeted_search.documentary import DocumentaryWriter
from app.services.targeted_search.service import SearchService
from app.services.targeted_search.worker import process_once


@pytest.fixture
def writing_api(tmp_path):
    service = SearchService(tmp_path / "search")
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Harbor fixture", "A fictional documentary fixture")
    folder = service.repo.root / "owned" / "inputs"
    folder.mkdir(parents=True)
    (folder / "filing.pdf").write_bytes(b"%PDF synthetic retained original")
    asset = workspace.import_folder(case["id"], folder)["assets"][0]
    service.set_policy(
        asset["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Synthetic internal writing fixture",
        "fixture-editor",
    )
    unit = workspace.add_evidence_unit(
        asset["id"],
        "The court found that the harbor closed in June.",
        "page",
        {"page_index": 0, "page_label": "1"},
        "document_passage",
        "native_pdf",
    )
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "The court found that the harbor closed in June.",
            "status": "reviewed",
            "assertion_class": "court_finding",
            "reviewed_by": "fixture-editor",
            "citations": [{"unit_id": unit["id"]}],
        },
    )
    with (
        patch.dict(config.app, {"api_key": "", "targeted_search_enabled": True}),
        patch.object(controller, "get_workspace", return_value=workspace),
        patch.object(controller, "_start_worker"),
    ):
        yield TestClient(asgi.app), workspace, case, claim, asset


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def structures(packet):
    claim_id = packet["claims"][0]["id"]
    citation_id = packet["citations"][0]["id"]
    outline = {
        "chapters": [
            {
                "chapter_id": "chapter_1",
                "title": "The closure",
                "scenes": [
                    {
                        "scene_id": "scene_1",
                        "title": "Court finding",
                        "purpose": "Establish the timeline",
                        "claim_ids": [claim_id],
                        "citation_ids": [citation_id],
                        "footage_queries": ["harbor exterior"],
                        "evidence_gaps": [],
                    }
                ],
            }
        ]
    }
    draft = {
        "chapters": [
            {
                "chapter_id": "chapter_1",
                "title": "The closure",
                "scenes": [
                    {
                        "scene_id": "scene_1",
                        "title": "Court finding",
                        "passages": [
                            {
                                "passage_id": "passage_1",
                                "text": "The court found that the harbor closed in June.",
                                "claim_ids": [claim_id],
                                "citation_ids": [citation_id],
                                "quotes": [],
                            }
                        ],
                        "footage_queries": ["harbor exterior"],
                        "evidence_gaps": [],
                    }
                ],
            }
        ]
    }
    review = {
        "passages": [
            {
                "passage_id": "passage_1",
                "status": "supported",
                "reason": "The passage preserves the cited court finding.",
                "citation_ids": [citation_id],
            }
        ],
        "notes": [],
    }
    return outline, draft, review


def test_api_worker_writing_review_export_and_policy_delivery(writing_api, monkeypatch):
    client, workspace, case, claim, asset = writing_api
    prefix = f"/api/v1/cases/{case['id']}/documentaries"
    packet = data(client.post(prefix + "/evidence", json={"claim_ids": [claim["id"]]}))
    outputs = iter(structures(packet))
    monkeypatch.setattr(
        llm, "_generate_response", lambda *args, **kwargs: json.dumps(next(outputs))
    )
    options = {
        "title": "Harbor documentary",
        "claim_ids": [claim["id"]],
        "stage": "outline",
    }
    queued = data(client.post(prefix, json=options))
    assert queued["job_type"] == "case_documentary"
    process_once(workspace.search_service)
    document = data(client.get(prefix))[0]
    for stage in ("draft", "factual_review"):
        options.update({"document_id": document["id"], "stage": stage})
        data(client.post(prefix, json=options))
        process_once(workspace.search_service)
        document = data(client.get(prefix + "/" + document["id"]))
    review = data(
        client.post(
            prefix + "/" + document["id"] + "/review",
            json={
                "reviewed_by": "Human editor",
                "notes": "Checked against the retained passage",
                "approved": True,
                "expected_revision": document["revision"],
            },
        )
    )
    exported = data(
        client.post(
            prefix + "/" + document["id"] + "/export",
            json={
                "final": True,
                "expected_revision": review["revision"],
            },
        )
    )
    assert exported["script_asset_id"]
    script = workspace.get_asset(exported["script_asset_id"])
    assert workspace._is_production(script)
    download = prefix + "/" + document["id"] + "/files/Final_Script.md"
    query = {"final": True, "expected_revision": review["revision"]}
    response = client.get(download, params=query)
    assert response.status_code == 200, response.text
    assert "The court found" in response.text
    workspace.search_service.set_policy(
        asset["source_id"], "blocked", "internal_review", "Fixture revoked", "editor"
    )
    assert client.get(download, params=query).status_code == 403


def test_api_requires_auth_and_revisions_and_rejects_external_origin(writing_api):
    client, _, case, _, _ = writing_api
    prefix = f"/api/v1/cases/{case['id']}/documentaries"
    with patch.dict(config.app, {"api_key": "fixture-api-token"}):
        assert client.get(prefix).status_code == 401
        assert (
            client.get(prefix, headers={"x-api-key": "fixture-api-token"}).status_code
            == 200
        )
    assert (
        client.post(
            prefix,
            json={"title": "Fixture"},
            headers={"Origin": "https://untrusted.example"},
        ).status_code
        == 403
    )
    assert client.put(prefix + "/missing", json={"draft": {}}).status_code == 400
    assert (
        client.post(
            prefix + "/missing/review", json={"reviewed_by": "editor", "approved": True}
        ).status_code
        == 400
    )
    assert (
        client.post(prefix + "/missing/export", json={"final": True}).status_code == 400
    )
    assert client.get(prefix + "/missing/files/Final_Script.md").status_code == 400


def test_empty_case_packet_never_invokes_writer_model(writing_api, monkeypatch):
    client, workspace, _, _, _ = writing_api
    empty = workspace.create_case("Empty fictional case")
    calls = []
    monkeypatch.setattr(
        llm, "_generate_response", lambda *args, **kwargs: calls.append(args)
    )
    response = client.post(
        f"/api/v1/cases/{empty['id']}/documentaries", json={"title": "Empty"}
    )
    assert response.status_code >= 400
    assert not calls


def test_cli_packet_and_generation_use_same_durable_worker(writing_api, monkeypatch):
    _, workspace, case, claim, _ = writing_api
    packet = run(
        workspace.search_service,
        parser().parse_args(
            [
                "case-documentary-packet",
                case["id"],
                "--claims",
                claim["id"],
            ]
        ),
    )
    outline = structures(packet)[0]
    monkeypatch.setattr(
        llm, "_generate_response", lambda *args, **kwargs: json.dumps(outline)
    )
    queued = run(
        workspace.search_service,
        parser().parse_args(
            [
                "case-documentary-write",
                case["id"],
                "Harbor documentary",
                "--minutes",
                "28",
                "--claims",
                claim["id"],
            ]
        ),
    )
    assert queued["job_type"] == "case_documentary"
    results = run(workspace.search_service, parser().parse_args(["worker", "--drain"]))
    assert results[0]["status"] == "complete"
    documents = run(
        workspace.search_service,
        parser().parse_args(["case-documentary-list", case["id"]]),
    )
    document = run(
        workspace.search_service,
        parser().parse_args(
            [
                "case-documentary-get",
                case["id"],
                documents[0]["id"],
            ]
        ),
    )
    assert document["options"]["target_minutes"] == 28


def test_queued_packet_change_fails_without_calling_model(writing_api, monkeypatch):
    _, workspace, case, claim, _ = writing_api
    writer = DocumentaryWriter(workspace)
    job = writer.enqueue(case["id"], {"title": "Fixture", "claim_ids": [claim["id"]]})
    workspace.save_claim(case["id"], {**claim, "text": "A changed reviewed claim."})
    monkeypatch.setattr(
        llm,
        "_generate_response",
        lambda *args, **kwargs: pytest.fail("Stale packet reached the model"),
    )
    process_once(workspace.search_service)
    result = workspace.search_service.get_job(job["id"])
    assert result["status"] == "failed"


def test_cli_review_requires_explicit_approval_or_rejection():
    with pytest.raises(SystemExit):
        parser().parse_args(
            [
                "case-documentary-review",
                "case-fixture",
                "doc-fixture",
                "--revision",
                "1",
                "--reviewer",
                "editor",
            ]
        )


def test_worker_rejects_superseded_document_revision_before_generation(
    writing_api, monkeypatch
):
    from app.services.targeted_search.worker import _dispatch

    _, workspace, case, _, _ = writing_api
    writer = DocumentaryWriter(workspace)
    packet = writer.build_packet(case["id"])
    monkeypatch.setattr(
        llm,
        "_generate_response",
        lambda *args, **kwargs: json.dumps(structures(packet)[0]),
    )
    document = writer.generate(case["id"], {"title": "Fixture"})
    with pytest.raises(SearchError, match="draft changed"):
        _dispatch(
            workspace.search_service,
            {
                "job_type": "case_documentary",
                "payload": {
                    "case_id": case["id"],
                    "options": {"document_id": document["id"], "stage": "draft"},
                    "packet_hash": packet["packet_hash"],
                    "document_revision": document["revision"] - 1,
                },
            },
        )


def test_worker_creates_new_outline_when_options_include_existing_document(
    writing_api, monkeypatch
):
    _, workspace, case, claim, _ = writing_api
    writer = DocumentaryWriter(workspace)
    packet = writer.build_packet(case["id"])
    monkeypatch.setattr(
        llm,
        "_generate_response",
        lambda *args, **kwargs: json.dumps(structures(packet)[0]),
    )
    original = writer.generate(case["id"], {"title": "Original outline"})
    job = writer.enqueue(
        case["id"],
        {
            "title": "New outline",
            "stage": "outline",
            "document_id": original["id"],
            "claim_ids": [claim["id"]],
        },
    )
    assert job["payload"]["document_revision"] is None

    result = process_once(workspace.search_service)

    assert result["status"] == "complete"
    documents = writer.list_documents(case["id"])
    assert len(documents) == 2
    created = next(item for item in documents if item["id"] != original["id"])
    assert created["title"] == "New outline"
    assert created["status"] == "outline_ready"
    assert writer.get_document(case["id"], original["id"]) == original


def test_provider_configuration_error_is_actionable_and_never_persisted(writing_api, monkeypatch):
    _, workspace, case, _, _ = writing_api
    monkeypatch.setattr(llm, "_generate_response", lambda *args, **kwargs: "Error: private-provider-detail")
    with pytest.raises(SearchError) as error:
        DocumentaryWriter(workspace).generate(case["id"], {"title": "Fixture"})
    assert error.value.status_code == 503
    assert "LLM settings" in str(error.value)
    assert "private-provider-detail" not in str(error.value)
    assert DocumentaryWriter(workspace).list_documents(case["id"]) == []
