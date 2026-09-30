"""Regression coverage for search artifacts crossing existing generation APIs."""

import json
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app import asgi
from app.config import config
from app.controllers.v1 import search as search_controller
from app.models.schema import MaterialInfo, VideoParams
from app.services import search_bridge
from app.utils import utils


def test_generation_bridge_preserves_actual_attachment_seconds(tmp_path):
    reference = {
        "artifact_id": "clip-1",
        "provider": "local",
        "duration": 12,
        "url": str(tmp_path / "clip.mp4"),
        "local_file": str(tmp_path / "clip.mp4"),
    }
    params = VideoParams(
        video_subject="fractions", video_source="local", search_artifact_ids=["clip-1"]
    )
    with (
        patch(
            "app.services.targeted_search.attachments.attach_clip",
            return_value=reference,
        ),
        patch(
            "app.services.targeted_search.attachments.resolve_attachments",
            return_value=[],
        ),
    ):
        search_bridge.prepare_search_materials(params, "task-1")
    assert params.video_materials[0].duration == 12
    assert params.video_materials[0].artifact_id == "clip-1"


def test_delivery_policy_uses_verified_provenance_artifact_and_local_path(tmp_path):
    task_dir = tmp_path / "task-1"
    task_dir.mkdir()
    local_path = str(tmp_path / "local_videos" / "search-clip.mp4")
    (task_dir / "search-materials.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "task_id": "task-1",
                "materials": [
                    {
                        "clip_id": "clip-1",
                        "material_artifact_id": "clip-1",
                        "local_path": local_path,
                        "parent_source_id": "source-1",
                        "source_start_ms": 1000,
                        "source_end_ms": 5000,
                    }
                ],
            }
        )
    )
    with (
        patch.object(utils, "task_dir", return_value=str(tmp_path)),
        patch(
            "app.services.targeted_search.attachments.authorize_attachments",
            return_value=[],
        ) as verify,
        patch(
            "app.services.targeted_search.attachments.resolve_attachments",
            side_effect=AssertionError("Delivery must not rewrite task provenance"),
        ),
    ):
        search_bridge.authorize_task_sources("task-1", "publication")
    materials = verify.call_args.args[0]
    assert materials[0].artifact_id == "clip-1"
    assert materials[0].url == local_path
    assert verify.call_args.kwargs["requested_use"] == "publication"


@pytest.mark.parametrize(
    "payload", ["{", "{}", '{"materials": []}', '{"materials": [null]}']
)
def test_malformed_task_provenance_fails_closed(tmp_path, payload):
    directory = tmp_path / "task-1"
    directory.mkdir()
    (directory / "search-materials.json").write_text(payload)
    with patch.object(utils, "task_dir", return_value=str(tmp_path)):
        with pytest.raises(ValueError, match="provenance"):
            search_bridge.authorize_task_sources("task-1")


def test_task_provenance_path_rejects_traversal_before_reading(tmp_path):
    with patch.object(utils, "task_dir", return_value=str(tmp_path)) as task_dir:
        with pytest.raises(ValueError, match="identifier"):
            search_bridge.authorize_task_sources("../task-1")
    task_dir.assert_not_called()


def test_local_material_basename_is_checked_even_when_client_drops_artifact_id():
    params = VideoParams(
        video_subject="fractions",
        video_source="local",
        video_materials=[MaterialInfo(provider="local", url="search-registered.mp4")],
    )
    with patch(
        "app.services.targeted_search.attachments.resolve_attachments",
        side_effect=ValueError("rights expired"),
    ) as verify:
        with pytest.raises(ValueError, match="rights expired"):
            search_bridge.prepare_search_materials(params, "task-1")
    verify.assert_called_once()
    assert verify.call_args.args[0][0].url == "search-registered.mp4"


@pytest.mark.parametrize(
    "route",
    [
        "/tasks/{task}/clip.mp4",
        "/api/v1/download/{task}/clip.mp4",
        "/api/v1/stream/{task}/clip.mp4",
    ],
)
def test_all_generated_delivery_routes_recheck_source_policy(route):
    with tempfile.TemporaryDirectory(
        prefix="search-delivery-", dir=utils.task_dir()
    ) as directory:
        path = Path(directory)
        (path / "clip.mp4").write_bytes(b"registered output")
        with (
            patch.dict(config.app, {"api_key": ""}),
            patch.object(
                search_bridge,
                "authorize_task_sources",
                side_effect=ValueError("rights expired"),
            ) as verify,
        ):
            response = TestClient(asgi.app).get(route.format(task=path.name))
    assert response.status_code == 403
    assert b"registered output" not in response.content
    assert verify.called


@pytest.fixture
def api_library(tmp_path):
    from app.services.targeted_search.service import SearchService

    service = SearchService(tmp_path / "targeted_search")
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
    )
    service.repo.settings = service.settings
    with (
        patch.dict(config.app, {"api_key": "", "targeted_search_enabled": True}),
        patch.object(search_controller, "get_service", return_value=service),
        patch.object(search_controller, "_start_worker"),
    ):
        yield TestClient(asgi.app), service


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_api_metadata_captions_search_policy_and_clip_contracts(api_library):
    client, service = api_library
    collection = _data(
        client.post(
            "/api/v1/search/collections",
            json={
                "name": "Arithmetic",
                "topic": "Fractions",
                "queries": ["subtract fractions"],
            },
        )
    )
    source = _data(
        client.post(
            "/api/v1/search/sources/metadata",
            json={
                "url": "https://www.youtube.com/watch?v=ownedclip01",
                "title": "Arithmetic lesson",
                "description": "An owned classroom recording",
                "creator_name": "Fixture teacher",
                "collection_id": collection["id"],
                "metadata": {"duration_ms": 60000},
            },
        )
    )
    source_id = source["id"]
    captions = "WEBVTT\n\n00:00:01.000 --> 00:00:06.000\nFind the least common denominator.\n\n00:00:06.000 --> 00:00:12.000\nThen rewrite both fractions and subtract the numerators.\n"
    _data(
        client.post(
            f"/api/v1/search/sources/{source_id}/captions",
            json={
                "text": captions,
                "language": "en",
                "kind": "vtt",
            },
        )
    )
    result = _data(
        client.post(
            "/api/v1/search/query",
            json={
                "query": "least common denominator",
                "filters": {"collection_id": collection["id"], "language": "en"},
                "top_k": 10,
            },
        )
    )
    transcript = next(
        item for item in result["results"] if item["evidence_type"] == "transcript"
    )
    assert (
        transcript["start_ms"] is not None
        and transcript["end_ms"] > transcript["start_ms"]
    )
    assert not transcript["actions"]["can_download"]
    assert service.list_artifacts(source_id) == []
    candidate_id = transcript["candidate_id"]
    forbidden = client.post(
        "/api/v1/search/clips",
        json={
            "candidate_id": candidate_id,
            "start_ms": 1000,
            "end_ms": 6000,
        },
    )
    assert forbidden.status_code == 403
    _data(
        client.post(
            f"/api/v1/search/sources/{source_id}/policy",
            json={
                "rights_status": "allowed_internal",
                "permitted_use": ["internal_review"],
                "reason": "Owned fixture approved for testing",
                "reviewed_by": "fixture-owner",
            },
        )
    )
    _data(client.post(f"/api/v1/search/candidates/{candidate_id}/validate"))
    _data(
        client.post(
            f"/api/v1/search/candidates/{candidate_id}/approve-download",
            json={
                "reviewed_by": "fixture-owner",
                "start_ms": 1000,
                "end_ms": 6000,
            },
        )
    )
    job = _data(
        client.post(
            "/api/v1/search/clips",
            json={
                "candidate_id": candidate_id,
                "start_ms": 1000,
                "end_ms": 6000,
            },
        )
    )
    assert job["job_type"] == "extract_clip"
    assert _data(client.get(f"/api/v1/search/jobs/{job['id']}"))["id"] == job["id"]
    assert _data(client.get(f"/api/v1/search/sources/{source_id}"))["id"] == source_id
    assert _data(client.get("/api/v1/search/collections"))[0]["id"] == collection["id"]
    assert service.list_artifacts(source_id) == []


def test_api_discovery_only_enqueues_and_rejects_invalid_range(api_library):
    client, service = api_library
    source = _data(
        client.post(
            "/api/v1/search/sources/discover",
            json={
                "url": "https://www.youtube.com/watch?v=ownedclip02",
            },
        )
    )
    assert source.get("job_id")
    assert service.list_artifacts(source["id"]) == []
    invalid = client.post(
        "/api/v1/search/clips",
        json={
            "candidate_id": "candidate-1",
            "start_ms": 6000,
            "end_ms": 1000,
        },
    )
    assert invalid.status_code in {400, 422}


@pytest.mark.parametrize(
    "kind,text",
    [
        (
            "vtt",
            "WEBVTT\n\n00:00:01.000 --> 00:00:06.000\nCommon denominator demonstration.\n",
        ),
        (
            "srt",
            "1\n00:00:01,000 --> 00:00:06,000\nCommon denominator demonstration.\n",
        ),
        (
            "json",
            json.dumps(
                {
                    "events": [
                        {
                            "tStartMs": 1000,
                            "dDurationMs": 5000,
                            "segs": [{"utf8": "Common denominator demonstration."}],
                        }
                    ]
                }
            ),
        ),
    ],
)
def test_api_caption_formats_keep_source_timestamps(api_library, kind, text):
    client, _ = api_library
    source = _data(
        client.post(
            "/api/v1/search/sources/metadata",
            json={
                "url": "https://www.youtube.com/watch?v=ownedclip03",
                "title": "Owned lesson",
                "metadata": {"duration_ms": 60000},
            },
        )
    )
    _data(
        client.post(
            f"/api/v1/search/sources/{source['id']}/captions",
            json={
                "text": text,
                "kind": kind,
                "language": "en",
            },
        )
    )
    result = _data(client.post("/api/v1/search/query", json={"query": "denominator"}))
    transcript = next(
        row for row in result["results"] if row["evidence_type"] == "transcript"
    )
    assert transcript["start_ms"] == 1000
    assert transcript["end_ms"] == 6000
