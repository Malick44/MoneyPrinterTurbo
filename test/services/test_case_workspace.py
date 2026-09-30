"""Offline case inventory, immutable versions, typed citations and rights gates."""

import json
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.search import SearchError
from app.services.targeted_search import SearchService
from app.services.targeted_search.case_workspace import CaseWorkspace


@pytest.fixture
def workspace(tmp_path):
    service = SearchService(tmp_path)
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
    )
    service.repo.settings = service.settings
    return CaseWorkspace(service)


def folder(workspace, files):
    root = workspace.repo.root / "owned" / "fixture-input"
    root.mkdir(parents=True, exist_ok=True)
    for name, value in files.items():
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(value.encode() if isinstance(value, str) else value)
    return root


def imported(workspace, files=None):
    case = workspace.create_case("Oil patch case", "Source assertions and originals")
    root = folder(
        workspace, files or {"LegalDocs/filing.pdf": b"%PDF-1.4\nfixture source"}
    )
    return case, root, workspace.import_folder(case["id"], root)["assets"]


def permit(workspace, asset, use="analysis,internal_review", status="allowed_internal"):
    return workspace.search_service.set_policy(
        asset["source_id"],
        status,
        use,
        "Fixture use explicitly reviewed",
        "fixture-reviewer",
    )


def passage(workspace, asset, text="The filing alleges a Bakken oil patch event."):
    return workspace.add_evidence_unit(
        asset["id"],
        text,
        "page",
        {"page_index": 0, "page_label": "1"},
        "document_passage",
        "native_pdf",
    )


def test_import_original_is_immutable_idempotent_and_unapproved(workspace):
    case, root, assets = imported(workspace)
    asset = assets[0]
    original = workspace.asset_path(asset["id"])
    assert original.is_relative_to(workspace.repo.root / "artifacts")
    assert original.read_bytes() == (root / "LegalDocs/filing.pdf").read_bytes()
    assert workspace.import_folder(case["id"], root)["unchanged"] == 1
    assert asset["rights_status"] == "unknown"
    assert (
        workspace.search_service.get_source(asset["source_id"])["metadata"][
            "asset_kind"
        ]
        == "document"
    )
    assert workspace.search_service.list_jobs() == []
    with pytest.raises(SearchError, match="reviewed rights"):
        workspace.enqueue_index(asset["id"])
    with workspace.repo.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM case_asset_versions").fetchone()[0]
            == 1
        )
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_replacement_keeps_original_versions_and_invalidates_evidence_rights(workspace):
    case, root, assets = imported(workspace)
    asset = assets[0]
    old_original = workspace.asset_path(asset["id"])
    unit = passage(workspace, asset)
    permit(workspace, asset)
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "A filing alleges an event",
            "status": "reviewed",
            "reviewed_by": "editor",
            "assertion_class": "allegation",
            "citations": [{"unit_id": unit["id"], "quote": "filing alleges"}],
        },
    )
    (root / "LegalDocs/filing.pdf").write_bytes(b"%PDF-1.4\nchanged source")
    result = workspace.import_folder(case["id"], root)
    fresh = result["assets"][0]
    assert result["updated"] == 1
    assert fresh["id"] == asset["id"] and fresh["version"] == 2
    assert old_original.read_bytes() == b"%PDF-1.4\nfixture source"
    assert workspace.asset_path(fresh["id"]).read_bytes() == b"%PDF-1.4\nchanged source"
    assert fresh["rights_status"] == "review_required"
    assert workspace.repo.get("evidence_units", unit["id"])["is_active"] == 0
    assert workspace.list_claims(case["id"])[0]["has_stale_citations"]
    with pytest.raises(SearchError, match="current evidence|obsolete"):
        workspace.save_claim(case["id"], claim)


@pytest.mark.parametrize("kind", ["file", "directory"])
def test_symlinks_are_rejected_before_any_assets_are_registered(
    workspace, tmp_path, kind
):
    case = workspace.create_case("Symlink fixture")
    root = folder(workspace, {"LegalDocs/good.pdf": "okay"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_text("never import")
    if kind == "file":
        (root / "bad.pdf").symlink_to(outside / "secret.pdf")
    else:
        (root / "bad").symlink_to(outside, target_is_directory=True)
    with pytest.raises(SearchError, match="symlinks"):
        workspace.import_folder(case["id"], root)
    assert workspace.list_assets(case["id"]) == []


def test_import_restricts_roots_file_count_and_bytes_before_registration(
    workspace, tmp_path
):
    case = workspace.create_case("Budgets")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.pdf").write_text("outside")
    with pytest.raises(SearchError, match="owned storage"):
        workspace.import_folder(case["id"], outside)
    root = folder(workspace, {"a.pdf": "aaaa", "b.wav": "bbbb"})
    with pytest.raises(SearchError, match="file count"):
        workspace.import_folder(case["id"], root, max_files=1)
    with pytest.raises(SearchError, match="storage limit"):
        workspace.import_folder(case["id"], root, max_total_bytes=4)
    assert workspace.list_assets(case["id"]) == []


def test_original_tampering_is_detected_on_access(workspace):
    _, _, assets = imported(workspace)
    original = workspace.asset_path(assets[0]["id"])
    original.write_bytes(b"tampered")
    with pytest.raises(SearchError, match="digest|hash|verification"):
        workspace.asset_path(assets[0]["id"])


def test_pages_and_stills_use_typed_locators_without_invented_times(workspace):
    case, _, assets = imported(
        workspace, {"LegalDocs/a.pdf": "pdf", "Images/photo.png": "image"}
    )
    document = next(asset for asset in assets if asset["asset_kind"] == "document")
    image = next(asset for asset in assets if asset["asset_kind"] == "image")
    workspace.record_document_page(
        document["id"], 0, "i", "Bakken court filing page text", "native_pdf"
    )
    still = workspace.add_evidence_unit(
        image["id"],
        "An oil rig shown in this image",
        "image",
        {"bbox": [0.1, 0.2, 0.8, 0.9]},
        "image_region",
        "reviewed_annotation",
    )
    assert still["locator"]["kind"] == "image" and "start_ms" not in still["locator"]
    for asset in assets:
        permit(workspace, asset)
    found = workspace.search_supporting(case["id"], "Bakken oil")
    assert {result["asset_kind"] for result in found["results"]} == {
        "document",
        "image",
    }
    assert (
        next(
            result for result in found["results"] if result["asset_kind"] == "document"
        )["locator"]["page_index"]
        == 0
    )
    with pytest.raises(SearchError, match="locator"):
        workspace.add_evidence_unit(
            document["id"],
            "bad",
            "time",
            {"start_ms": 0, "end_ms": 1},
            "passage",
            "provided",
        )
    with pytest.raises(SearchError, match="locator"):
        workspace.add_evidence_unit(
            image["id"], "bad", "image", {"bbox": [0, 0, 200, 300]}, "image", "provided"
        )


def test_word_import_preserves_unaligned_words_and_source_version(workspace):
    _, _, assets = imported(workspace, {"RawAudio/911.wav": b"audio"})
    asset = assets[0]
    artifact = workspace.repo.insert_artifact(
        source_id=asset["source_id"],
        kind="source_transcript",
        path=workspace.asset_path(asset["id"]),
        sha256=asset["sha256"],
        bytes=5,
    )
    result = workspace.record_transcript_words(
        asset["id"],
        artifact["id"],
        [
            {
                "text": "Dispatch",
                "start_ms": 0,
                "end_ms": 300,
                "speaker": "SPEAKER_00",
                "channel": 1,
                "confidence": 0.7,
            },
            {"text": "inaudible", "start_ms": None, "end_ms": None},
        ],
    )
    assert result["unaligned_words"] == 1
    with workspace.repo.connect() as connection:
        words = connection.execute(
            "SELECT * FROM transcript_words ORDER BY word_index"
        ).fetchall()
    assert words[0]["speaker"] == "SPEAKER_00" and words[0]["channel"] == "1"
    assert words[1]["start_ms"] is None and words[1]["end_ms"] is None
    assert words[1]["asset_version_id"] == asset["asset_version_id"]
    with pytest.raises(SearchError, match="Word times"):
        workspace.record_transcript_words(
            asset["id"],
            artifact["id"],
            [{"text": "unknown", "start_ms": None, "end_ms": 100}],
        )


def test_claim_citations_are_case_scoped_exact_and_not_truth_validation(workspace):
    case, _, assets = imported(workspace)
    asset = assets[0]
    unit = passage(workspace, asset)
    other = workspace.create_case("Other case")
    with pytest.raises(SearchError, match="current evidence"):
        workspace.save_claim(other["id"], {"text": "Other", "citations": [unit["id"]]})
    with pytest.raises(SearchError, match="match an indexed"):
        workspace.save_claim(
            case["id"],
            {
                "text": "Other",
                "citations": [
                    {"unit_id": unit["id"], "quote": "Invented witness quote"}
                ],
            },
        )
    with pytest.raises(SearchError, match="retrieval cannot verify facts"):
        workspace.save_claim(case["id"], {"text": "Assumed true", "status": "verified"})
    with pytest.raises(SearchError, match="reviewer"):
        workspace.save_claim(
            case["id"],
            {"text": "Allegation", "status": "reviewed", "citations": [unit["id"]]},
        )
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "This filing alleges an oil patch event",
            "assertion_class": "allegation",
            "citations": [{"unit_id": unit["id"], "relation": "mentions"}],
        },
    )
    assert claim["assertion_class"] == "allegation" and claim["status"] == "proposed"
    updated = workspace.save_claim(case["id"], {**claim, "text": "Edited allegation"})
    assert updated["id"] == claim["id"]
    with workspace.repo.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM case_record_versions WHERE record_id=?",
                (claim["id"],),
            ).fetchone()[0]
            == 2
        )


def test_production_content_cannot_self_corroborate_claim_and_is_excluded(workspace):
    case, _, assets = imported(
        workspace,
        {
            "05_Production/script.md": "narration",
            "LegalDocs/a.pdf": "filing",
            "05_Production/narration.wav": "audio",
        },
    )
    script = next(asset for asset in assets if asset["asset_kind"] == "script")
    audio = next(asset for asset in assets if asset["asset_kind"] == "audio")
    assert script["metadata"]["role"] == audio["metadata"]["role"] == "production"
    unit = workspace.add_evidence_unit(
        script["id"],
        "Bakken narration assertion",
        "script",
        {"char_start": 0, "char_end": 8},
        "script_passage",
        "production_script",
    )
    for asset in assets:
        permit(workspace, asset)
    assert workspace.search_supporting(case["id"], "Bakken")["results"] == []
    assert (
        workspace.search_supporting(case["id"], "Bakken", {"include_production": True})[
            "results"
        ][0]["id"]
        == unit["id"]
    )
    with pytest.raises(SearchError, match="cannot substantiate"):
        workspace.save_claim(
            case["id"],
            {
                "text": "Bakken assertion",
                "status": "reviewed",
                "reviewed_by": "editor",
                "citations": [unit["id"]],
            },
        )


def test_rights_filtering_and_manifest_withhold_content_after_revocation(workspace):
    case, _, assets = imported(workspace)
    unit = passage(workspace, assets[0])
    assert workspace.search_supporting(case["id"], "Bakken")["results"] == []
    permit(workspace, assets[0])
    assert workspace.search_supporting(case["id"], "Bakken")["results"]
    workspace.save_claim(
        case["id"],
        {
            "text": "An allegation",
            "citations": [{"unit_id": unit["id"], "quote": "Bakken oil patch"}],
        },
    )
    workspace.search_service.set_policy(
        assets[0]["source_id"],
        "blocked",
        "internal_review",
        "Fixture revoked",
        "reviewer",
    )
    assert workspace.search_supporting(case["id"], "Bakken")["results"] == []
    export = workspace.export_case(case["id"])
    assert (
        export["evidence_units"][0]["content_withheld"]
        and "text" not in export["evidence_units"][0]
    )
    assert export["claims"][0]["citations"][0]["quote"] is None
    assert str(workspace.repo.root) not in json.dumps(export)


def test_geojson_categories_and_existing_source_links(workspace):
    case, _, assets = imported(
        workspace,
        {
            "Maps/location.geojson": '{"type":"FeatureCollection","features":[]}',
            "Maps/points.json": '{"type":"Point","coordinates":[0,0]}',
        },
    )
    assert all(asset["asset_kind"] == "map" for asset in assets)
    source = workspace.search_service.register_metadata(
        "https://www.krem.com/article/example",
        "Existing video",
        "Original selected source",
    )
    linked = workspace.link_source(case["id"], source["id"])
    assert workspace.link_source(case["id"], source["id"])["id"] == linked["id"]
    assert workspace.get_case(case["id"])["source_ids"] == [source["id"]]
    assert (
        workspace.search_service.get_source(source["id"])["metadata"].get("asset_kind")
        is None
    )


def test_requests_events_entities_mentions_and_citation_links(workspace):
    case, _, assets = imported(workspace)
    unit = passage(workspace, assets[0])
    request = workspace.save_request(
        case["id"],
        {
            "title": "911 audio",
            "status": "missing",
            "asset_kind": "audio",
            "notes": "Not received",
        },
    )
    assert (
        workspace.save_request(case["id"], {**request, "status": "requested"})["status"]
        == "requested"
    )
    event = workspace.save_event(
        case["id"],
        {
            "title": "Alleged event",
            "event_at": "2013-12",
            "time_precision": "month",
            "citations": [unit["id"]],
        },
    )
    assert event["event_at"] == "2013-12" and event["time_precision"] == "month"
    entity = workspace.save_entity(
        case["id"],
        {"entity_type": "location", "name": "Bakken", "aliases": ["Bakken formation"]},
    )
    mention = workspace.save_mention(
        case["id"],
        {
            "entity_id": entity["id"],
            "unit_id": unit["id"],
            "char_start": 20,
            "char_end": 26,
            "review_status": "proposed",
        },
    )
    assert mention["entity_id"] == entity["id"]
    assert workspace.list_entities(case["id"])[0]["review_status"] == "proposed"


def test_storyboard_pins_assets_backgrounds_narration_and_queue_identity(workspace):
    case, root, assets = imported(
        workspace,
        {
            "Audio/911.wav": "audio",
            "Images/photo.png": "picture",
            "Production/narration.wav": "narration",
        },
    )
    audio = next(asset for asset in assets if asset["relative_path"] == "Audio/911.wav")
    image = next(asset for asset in assets if asset["asset_kind"] == "image")
    narration = next(asset for asset in assets if asset["category"] == "Production")
    for asset in assets:
        permit(
            workspace,
            asset,
            "analysis,internal_review,generated_export",
            "allowed_export",
        )
    storyboard = workspace.save_storyboard(
        case["id"],
        {
            "title": "Reviewed assembly",
            "script": "Narration",
            "narration_asset_id": narration["id"],
            "scenes": [
                {
                    "scene_id": "911",
                    "asset_id": audio["id"],
                    "visual_asset_id": image["id"],
                    "visual_locator": {"kind": "image"},
                    "role": "original_sound",
                    "source_start_ms": 0,
                    "source_end_ms": 1000,
                    "duration_ms": 1000,
                }
            ],
        },
    )
    scene = storyboard["scenes"][0]
    assert scene["asset_version_id"] == audio["asset_version_id"]
    assert scene["visual_asset_version_id"] == image["asset_version_id"]
    job = workspace.enqueue_render(storyboard["id"])
    assert (
        job["job_type"] == "case_render"
        and job["payload"]["storyboard_hash"] == storyboard["content_hash"]
    )
    assert workspace.enqueue_render(storyboard["id"])["id"] == job["id"]
    (root / "Images/photo.png").write_text("changed picture")
    workspace.import_folder(case["id"], root)
    with pytest.raises(SearchError, match="background changed"):
        workspace.enqueue_render(storyboard["id"])
    fresh = workspace.save_storyboard(case["id"], storyboard)
    assert fresh["content_hash"] != storyboard["content_hash"]


def test_api_routes_and_case_membership_guard(workspace, monkeypatch):
    from app.controllers import base
    from app.controllers.v1 import cases

    monkeypatch.setattr(cases, "get_workspace", lambda: workspace)
    monkeypatch.setattr(cases, "_start_worker", lambda: None)
    app = FastAPI()
    app.include_router(cases.router)
    app.dependency_overrides[base.verify_token] = lambda: None
    client = TestClient(app, raise_server_exceptions=False)
    created = client.post("/api/v1/cases", json={"name": "API case"})
    assert created.status_code == 200
    case_id = created.json()["data"]["id"]
    assert client.get(f"/api/v1/cases/{case_id}/assets").json()["data"] == []
    assert (
        client.post(
            f"/api/v1/cases/{case_id}/requests",
            json={"record": {"title": "Radio audio"}},
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/v1/cases/{case_id}/search", json={"query": "oil patch", "top_k": 0}
        ).status_code
        == 422
    )
    assert (
        client.get(f"/api/v1/cases/{case_id}/export").json()["data"]["schema_version"]
        == "case-workspace-1"
    )


def test_stale_native_extraction_cannot_bind_to_replacement_version(workspace):
    case, root, assets = imported(workspace)
    old = assets[0]
    (root / "LegalDocs/filing.pdf").write_text("new PDF bytes")
    workspace.import_folder(case["id"], root)
    pin = {"asset_version_id": old["asset_version_id"], "input_sha256": old["sha256"]}
    with pytest.raises(SearchError, match="obsolete asset version"):
        workspace.record_document_page(
            old["id"], 0, "1", "Old extraction", "native_pdf", metadata=pin
        )
    with pytest.raises(SearchError, match="obsolete asset version"):
        workspace.add_evidence_unit(
            old["id"],
            "Old extraction",
            "page",
            {"page_index": 0},
            "passage",
            "native_pdf",
            metadata=pin,
        )
    with workspace.repo.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM document_pages").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT count(*) FROM evidence_units").fetchone()[0] == 0
        )


def test_linked_asset_sync_retains_verified_acquisition_and_is_idempotent(workspace):
    case = workspace.create_case("Linked metadata lead")
    source = workspace.search_service.register_metadata(
        "https://www.krem.com/article/example", "Original selected video", "Context"
    )
    lead = workspace.link_source(case["id"], source["id"])
    assert workspace.refresh_linked_asset(lead["id"])["artifact_id"] is None
    path = workspace.repo.root / "artifacts" / "source.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"video fixture bytes")
    artifact = workspace.repo.insert_artifact(
        source_id=source["id"],
        kind="source",
        path=path,
        sha256=workspace._file_hash(path),
        bytes=path.stat().st_size,
        metadata={"duration_ms": 1000},
    )
    synced = workspace.refresh_linked_asset(lead["id"])
    assert synced["version"] == 2 and synced["artifact_id"] == artifact["id"]
    assert workspace.asset_path(lead["id"]).read_bytes() == path.read_bytes()
    assert (
        workspace.refresh_linked_asset(lead["id"])["asset_version_id"]
        == synced["asset_version_id"]
    )


def test_alignment_job_pins_source_and_script_versions(workspace):
    _, _, assets = imported(
        workspace, {"Production/audio.wav": "audio", "Production/script.md": "script"}
    )
    audio = next(asset for asset in assets if asset["asset_kind"] == "audio")
    script = next(asset for asset in assets if asset["asset_kind"] == "script")
    for asset in assets:
        permit(workspace, asset)
    job = workspace.enqueue_alignment(audio["id"], script["id"], scope="narration")
    assert job["payload"]["asset_version_id"] == audio["asset_version_id"]
    assert job["payload"]["script_asset_version_id"] == script["asset_version_id"]
    assert job["payload"]["input_script_sha256"] == script["sha256"]


def test_storyboard_archive_preserves_history_and_prevents_render(workspace):
    case, _, assets = imported(workspace, {"Images/photo.png": "image"})
    storyboard = workspace.save_storyboard(
        case["id"],
        {
            "title": "Archived draft",
            "scenes": [
                {
                    "scene_id": "image",
                    "asset_id": assets[0]["id"],
                    "duration_ms": 1000,
                    "role": "still",
                }
            ],
        },
    )
    archived = workspace.delete_storyboard(storyboard["id"])
    assert archived["metadata"]["archived"]
    assert workspace.list_storyboards(case["id"]) == []
    assert (
        workspace.list_storyboards(case["id"], include_archived=True)[0]["id"]
        == storyboard["id"]
    )
    with pytest.raises(SearchError, match="Archived"):
        workspace.enqueue_render(storyboard["id"])
    with workspace.repo.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM case_record_versions WHERE record_id=?",
                (storyboard["id"],),
            ).fetchone()[0]
            == 2
        )


def test_generated_manifests_and_workspace_outputs_are_not_reimported(workspace):
    case, root, _ = imported(
        workspace,
        {
            "LegalDocs/original.pdf": "source",
            "00_Research/requests.json": "{}",
            "06_Derived/transcript.json": "{}",
            "07_Exports/render.mp4": "render",
            "research/summary.md": "summary",
            "case.json": "{}",
            "Case_Manifest.json": "{}",
        },
    )
    assets = workspace.list_assets(case["id"])
    assert [asset["filename"] for asset in assets] == ["original.pdf"]
    assert workspace.import_folder(case["id"], root)["unchanged"] == 1


def test_supporting_assets_do_not_consume_footage_collection_capacity(workspace):
    workspace.settings = replace(workspace.settings, max_collection_sources=1)
    workspace.search_service.settings = workspace.settings
    workspace.repo.settings = workspace.settings
    case, _, assets = imported(
        workspace,
        {
            "LegalDocs/a.pdf": "a",
            "LegalDocs/b.pdf": "b",
            "Audio/call.wav": "audio",
            "Images/map.png": "image",
            "Video/report.mp4": "video",
        },
    )
    assert len(assets) == 5
    collection_sources = workspace.search_service.list_sources(case["collection_id"])
    assert len(collection_sources) == 1
    assert collection_sources[0]["metadata"]["asset_kind"] == "video"
    source = workspace.search_service.register_metadata(
        "https://www.krem.com/article/other", "Another selected video", "Context"
    )
    with pytest.raises(SearchError, match="footage collection"):
        workspace.link_source(case["id"], source["id"])
    assert (
        workspace.link_source(case["id"], source["id"], asset_kind="reference")[
            "asset_kind"
        ]
        == "reference"
    )


def test_production_video_stays_in_inventory_without_entering_footage_scope(workspace):
    workspace.settings = replace(workspace.settings, max_collection_sources=1)
    workspace.search_service.settings = workspace.settings
    workspace.repo.settings = workspace.settings
    case, _, assets = imported(
        workspace,
        {
            "05_Production/draft.mp4": "owned draft video",
            "Video/source.mp4": "source video",
        },
    )
    production = next(
        asset for asset in assets if asset["metadata"]["role"] == "production"
    )
    primary = next(
        asset for asset in assets if asset["metadata"]["role"] == "source_evidence"
    )
    assert len(workspace.list_assets(case["id"])) == 2
    assert workspace.get_case(case["id"])["counts"]["video"] == 2
    assert workspace.get_case(case["id"])["source_ids"] == [primary["source_id"]]
    assert [
        source["id"]
        for source in workspace.search_service.list_sources(case["collection_id"])
    ] == [primary["source_id"]]
    second = workspace.create_case("Linked production")
    linked = workspace.link_source(second["id"], production["source_id"])
    assert linked["metadata"]["role"] == "production"
    assert workspace.get_case(second["id"])["source_ids"] == []
    assert workspace.search_service.list_sources(second["collection_id"]) == []


def test_storyboard_script_pin_rejects_byte_changes_even_when_visible_text_matches(
    workspace, monkeypatch
):
    from app.services.targeted_search import case_production

    case, root, assets = imported(
        workspace,
        {"05_Production/script.md": "Narration text", "Images/photo.png": "image"},
    )
    script = next(asset for asset in assets if asset["asset_kind"] == "script")
    image = next(asset for asset in assets if asset["asset_kind"] == "image")
    for asset in assets:
        permit(
            workspace,
            asset,
            "analysis,internal_review,generated_export",
            "allowed_export",
        )
    storyboard = workspace.save_storyboard(
        case["id"],
        {
            "title": "Pinned script",
            "script": "Narration text",
            "scenes": [
                {
                    "scene_id": "still",
                    "asset_id": image["id"],
                    "role": "still",
                    "duration_ms": 1000,
                }
            ],
            "metadata": {
                "script_asset_id": script["id"],
                "script_asset_version_id": "client-forged-version",
                "script_asset_sha256": "client-forged-digest",
            },
        },
    )
    assert (
        storyboard["metadata"]["script_asset_version_id"] == script["asset_version_id"]
    )
    assert storyboard["metadata"]["script_asset_sha256"] == script["sha256"]
    job = workspace.enqueue_render(storyboard["id"])
    assert job["payload"]["storyboard_hash"] == storyboard["content_hash"]
    (root / "05_Production/script.md").write_text("  Narration text\n")
    fresh = workspace.import_folder(case["id"], root)["assets"]
    changed_script = next(asset for asset in fresh if asset["asset_kind"] == "script")
    assert (
        workspace.asset_path(script["id"]).read_text().strip() == storyboard["script"]
    )
    assert changed_script["sha256"] != script["sha256"]
    assert (
        workspace.get_storyboard(storyboard["id"])["metadata"]["script_asset_sha256"]
        == script["sha256"]
    )
    permit(
        workspace,
        changed_script,
        "analysis,internal_review,generated_export",
        "allowed_export",
    )
    with pytest.raises(SearchError, match="Storyboard script changed") as enqueue_error:
        workspace.enqueue_render(storyboard["id"])
    assert enqueue_error.value.status_code == 409

    def no_ffmpeg(*args, **kwargs):
        pytest.fail("A stale script must be rejected before rendering starts")

    monkeypatch.setattr(case_production, "run_command", no_ffmpeg)
    with pytest.raises(
        SearchError,
        match="script.*(changed|superseded)|[Ss]cript.*(changed|superseded)",
    ) as render_error:
        case_production.render_storyboard(
            workspace, storyboard["id"], expected_hash=job["payload"]["storyboard_hash"]
        )
    assert render_error.value.status_code == 409
    refreshed = workspace.save_storyboard(case["id"], storyboard)
    assert (
        refreshed["metadata"]["script_asset_version_id"]
        == changed_script["asset_version_id"]
    )
    assert refreshed["content_hash"] != storyboard["content_hash"]


def test_storyboard_script_reference_rejects_wrong_kind_and_cross_case(workspace):
    case, _, assets = imported(
        workspace, {"Production/script.md": "script", "Images/photo.png": "image"}
    )
    image = next(asset for asset in assets if asset["asset_kind"] == "image")
    script = next(asset for asset in assets if asset["asset_kind"] == "script")
    other = workspace.create_case("Other script case")
    with pytest.raises(SearchError, match="script asset in this case"):
        workspace.save_storyboard(
            case["id"],
            {"title": "Wrong kind", "metadata": {"script_asset_id": image["id"]}},
        )
    with pytest.raises(SearchError, match="script asset in this case"):
        workspace.save_storyboard(
            other["id"],
            {"title": "Other case", "metadata": {"script_asset_id": script["id"]}},
        )
    clean = workspace.save_storyboard(
        case["id"],
        {
            "title": "No bound script",
            "metadata": {
                "script_asset_version_id": "forged",
                "script_asset_sha256": "forged",
            },
        },
    )
    assert "script_asset_sha256" not in clean["metadata"]
    assert "script_asset_version_id" not in clean["metadata"]
