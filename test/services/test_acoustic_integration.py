"""Acoustic API/CLI workflow with real PCM, imported alignment and FFmpeg."""

import json
from dataclasses import replace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.controllers.v1 import cases as controller
from app.services import llm
from app.services.targeted_search.case_media import import_whisperx
from app.services.targeted_search.attachments import artifact_content
from app.models.search import SearchError
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.cli import parser, run
from app.services.targeted_search.service import SearchService
from app.services.targeted_search.worker import process_once
from app.services.targeted_search.acoustic_pipeline import AcousticPipeline
from test.services.test_sound_assets import write_tone


@pytest.fixture
def acoustic_api(tmp_path):
    service = SearchService(tmp_path / "search")
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Synthetic audio integration")
    folder = service.repo.root / "owned" / "inputs"
    write_tone(
        folder / "05_Production" / "Narration.wav",
        duration=3,
        frequency=1000,
        amplitude=0.2,
    )
    write_tone(
        folder / "SFX" / "Impact.wav", duration=0.5, frequency=200, amplitude=0.4
    )
    (folder / "05_Production" / "Script.md").write_text("At dawn the harbor closed.")
    assets = workspace.import_folder(case["id"], folder)["assets"]
    by_name = {asset["filename"]: asset for asset in assets}
    for asset in assets:
        service.set_policy(
            asset["source_id"],
            "allowed_internal",
            "analysis,internal_review",
            "Synthetic fixture permission",
            "editor",
        )
    narration, script, effect = [
        by_name[name] for name in ("Narration.wav", "Script.md", "Impact.wav")
    ]
    import_whisperx(
        workspace,
        narration["id"],
        {
            "audio_sha256": narration["sha256"],
            "script_sha256": script["sha256"],
            "words": [
                {"word": word, "start": 0.1 + index * 0.45, "end": 0.3 + index * 0.45}
                for index, word in enumerate(["At", "dawn", "the", "harbor", "closed."])
            ],
        },
        scope="narration",
        script_asset_id=script["id"],
    )
    with (
        patch.dict(config.app, {"api_key": "", "targeted_search_enabled": True}),
        patch.object(controller, "get_workspace", return_value=workspace),
        patch.object(controller, "_start_worker"),
    ):
        yield TestClient(asgi.app), workspace, case, narration, script, effect


def data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def cue_response(*args, **kwargs):
    return json.dumps(
        {
            "cues": [
                {
                    "cue_id": "reveal_1",
                    "anchor_word_index": 4,
                    "category": "impact",
                    "tension": 0.8,
                    "reason": "A synthetic narrative turn",
                    "query": "low impact",
                    "duration_ms": 400,
                    "gain_db": -18,
                }
            ],
            "notes": ["Synthetic timestamps supplied by the fixture."],
        }
    )


def test_api_analyze_match_mix_download_and_revision_guard(acoustic_api, monkeypatch):
    client, workspace, case, narration, script, effect = acoustic_api
    prefix = f"/api/v1/cases/{case['id']}"
    data(
        client.post(
            prefix + "/sounds",
            json={
                "asset_id": effect["id"],
                "category": "impact",
                "tags": ["low", "impact"],
            },
        )
    )
    options = {"narration_asset_id": narration["id"], "script_asset_id": script["id"]}
    assert (
        data(client.post(prefix + "/acoustics/readiness", json=options))["word_count"]
        == 5
    )
    monkeypatch.setattr(llm, "_generate_response", cue_response)
    analyzed = data(client.post(prefix + "/acoustics", json=options))
    process_once(workspace.search_service)
    assert workspace.search_service.get_job(analyzed["id"])["status"] == "complete"
    summary = data(client.get(prefix + "/acoustics"))[0]
    plan = data(client.get(prefix + "/acoustics/" + summary["id"]))
    assert plan["cues"][0]["start_ms"] == 1900
    assert plan["cues"][0]["asset_id"] == effect["id"]
    mixed = data(
        client.post(
            prefix + "/acoustics/" + plan["id"] + "/mix",
            json={"expected_revision": plan["revision"]},
        )
    )
    process_once(workspace.search_service)
    job = workspace.search_service.get_job(mixed["id"])
    assert job["status"] == "complete", job.get("error")
    for key in ("artifact_id", "timeline_artifact_id", "otio_artifact_id"):
        response = client.get(
            prefix + "/acoustics/" + plan["id"] + "/files/" + job["result"][key]
        )
        assert response.status_code == 200, response.text
    assert (
        client.post(
            prefix + "/acoustics/" + plan["id"] + "/mix", json={"expected_revision": 99}
        ).status_code
        == 409
    )
    workspace.search_service.set_policy(
        effect["source_id"], "blocked", "internal_review", "Fixture revoked", "editor"
    )
    assert (
        client.get(
            prefix
            + "/acoustics/"
            + plan["id"]
            + "/files/"
            + job["result"]["artifact_id"]
        ).status_code
        >= 400
    )
    for key in ("artifact_id", "timeline_artifact_id", "otio_artifact_id"):
        with pytest.raises(SearchError):
            artifact_content(job["result"][key], root_dir=workspace.repo.root)


def test_cli_register_readiness_and_queued_analysis(acoustic_api, monkeypatch):
    _, workspace, case, narration, script, effect = acoustic_api
    service = workspace.search_service
    run(
        service,
        parser().parse_args(
            [
                "case-sound-register",
                case["id"],
                effect["id"],
                "--category",
                "impact",
                "--tags",
                "low",
                "impact",
            ]
        ),
    )
    ready = run(
        service,
        parser().parse_args(
            ["case-acoustic-readiness", case["id"], narration["id"], script["id"]]
        ),
    )
    assert ready["alignment_ready"]
    monkeypatch.setattr(llm, "_generate_response", cue_response)
    queued = run(
        service,
        parser().parse_args(
            [
                "case-acoustic-analyze",
                case["id"],
                narration["id"],
                script["id"],
                "--max-cues",
                "3",
            ]
        ),
    )
    process_once(service)
    assert service.get_job(queued["id"])["status"] == "complete"
    summary = run(service, parser().parse_args(["case-acoustic-list", case["id"]]))[0]
    plan = run(
        service, parser().parse_args(["case-acoustic-get", case["id"], summary["id"]])
    )
    assert plan["cues"][0]["anchor_word"] == "closed."


def test_acoustic_routes_require_case_ownership_auth_and_revision(acoustic_api):
    client, workspace, case, _, _, effect = acoustic_api
    other = workspace.create_case("Another synthetic case")
    assert (
        client.post(
            f"/api/v1/cases/{other['id']}/sounds",
            json={"asset_id": effect["id"], "category": "impact", "tags": ["hit"]},
        ).status_code
        == 404
    )
    prefix = f"/api/v1/cases/{case['id']}/acoustics"
    with patch.dict(config.app, {"api_key": "synthetic-token"}):
        assert client.get(prefix).status_code == 401
        assert (
            client.get(prefix, headers={"x-api-key": "synthetic-token"}).status_code
            == 200
        )
    assert client.post(prefix + "/missing/mix", json={}).status_code == 400
    assert (
        client.put(prefix + "/missing", json={"record": {"cues": []}}).status_code
        == 400
    )


def test_provider_configuration_error_never_becomes_sound_cue_content(acoustic_api, monkeypatch):
    _, workspace, case, narration, script, _ = acoustic_api
    monkeypatch.setattr(llm, "_generate_response", lambda *args, **kwargs: "Error: private-provider-detail")
    pipeline = AcousticPipeline(workspace)
    with pytest.raises(SearchError) as error:
        pipeline.analyze(case["id"], {"narration_asset_id": narration["id"], "script_asset_id": script["id"]})
    assert error.value.status_code == 503
    assert "LLM settings" in str(error.value)
    assert "private-provider-detail" not in str(error.value)
    assert pipeline.list_plans(case["id"]) == []
