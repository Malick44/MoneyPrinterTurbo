"""Persisted case retrieval budgets and inference-time eligibility regressions.

These fixtures seed extractor output directly; decoding and OCR are exercised
separately in test_case_media.py. No model download is needed here.
"""

import base64
import hashlib
import sys
import wave
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services.targeted_search import case_retrieval, visual
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.repository import json_text, new_id, now
from app.services.targeted_search.service import SearchService


@pytest.fixture
def library(tmp_path):
    service = SearchService(tmp_path / "case-library")
    service.settings = replace(
        service.settings,
        enabled=True,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
        embedding_model="fixture-text",
        embedding_revision="fixture-revision",
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Retrieval eligibility fixture")
    root = workspace.repo.root / "owned" / "retrieval-input"
    for directory in ("LegalDocs", "Audio", "Images", "05_Production"):
        (root / directory).mkdir(parents=True)
    for name in ("LegalDocs/source.pdf", "05_Production/draft.pdf"):
        (root / name).write_bytes(b"%PDF-1.4\n% immutable inventory fixture\n")
    with wave.open(str(root / "Audio" / "source.wav"), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(b"\0\0" * 32000)
    (root / "Images" / "source.png").write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/l9sAAAAASUVORK5CYII="
        )
    )
    imported = workspace.import_folder(case["id"], root)
    assets = {asset["relative_path"]: asset for asset in imported["assets"]}
    for asset in assets.values():
        permit(workspace, asset)
    return workspace, case, root, assets


def permit(workspace, asset, status="allowed_internal"):
    workspace.search_service.set_policy(
        asset["source_id"],
        status,
        "analysis,internal_review",
        "Owned retrieval fixture reviewed explicitly",
        "fixture-reviewer",
    )


def configure(workspace, **changes):
    settings = replace(workspace.settings, **changes)
    workspace.settings = settings
    workspace.search_service.settings = settings
    workspace.repo.settings = settings


def passage(
    workspace, asset, text="needle source passage", origin="native_pdf", metadata=None
):
    return workspace.add_evidence_unit(
        asset["id"],
        text,
        "page",
        {"kind": "page", "page_index": 0, "page_label": "1"},
        "document_passage",
        origin,
        metadata=metadata,
    )


def many_passages(
    workspace, asset, count, origin="native_pdf", scope=None, text="needle " * 30
):
    """Bulk extractor fixtures leave real SQLite FTS triggers and joins active."""
    records = []
    for index in range(count):
        identifier = new_id("retrieval_unit_")
        evidence = text + str(index)
        metadata = {
            "asset_version_id": asset["asset_version_id"],
            "input_sha256": asset["sha256"],
        }
        if scope:
            metadata["scope"] = scope
        content_hash = hashlib.sha256(
            json_text([asset["asset_version_id"], evidence, identifier]).encode()
        ).hexdigest()
        records.append(
            (
                identifier,
                asset["case_id"],
                asset["id"],
                asset["asset_version_id"],
                asset["source_id"],
                asset["artifact_id"],
                "document_passage",
                evidence,
                "page",
                json_text({"kind": "page", "page_index": 0, "page_label": "1"}),
                content_hash,
                origin,
                None,
                json_text(metadata),
                1,
                now(),
            )
        )
    with workspace.repo.connect() as connection:
        connection.executemany(
            "INSERT INTO evidence_units VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            records,
        )
    return [record[0] for record in records]


def vector(
    workspace,
    asset,
    entity_id,
    entity_type="case_evidence_unit",
    values=(1.0, 0.0),
    modality="text",
    model="fixture-text",
    revision="fixture-revision",
    input_hash=None,
):
    if input_hash is None:
        unit = workspace.repo.get("evidence_units", entity_id)
        input_hash = hashlib.sha256(unit["text"].encode()).hexdigest()
    with workspace.repo.connect() as connection:
        connection.execute(
            "INSERT INTO embeddings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                new_id("retrieval_emb_"),
                entity_type,
                entity_id,
                asset["source_id"],
                modality,
                model,
                revision,
                len(values),
                json_text(values),
                input_hash,
                now(),
            ),
        )


def test_many_pdf_passages_cannot_crowd_audio_or_image_group_budget(library):
    workspace, case, _, assets = library
    many_passages(workspace, assets["LegalDocs/source.pdf"], 650)
    audio = workspace.add_evidence_unit(
        assets["Audio/source.wav"]["id"],
        "needle source call",
        "time",
        {"kind": "time", "start_ms": 125, "end_ms": 800},
        "transcript_segment",
        "reviewed_transcript",
    )
    image = workspace.add_evidence_unit(
        assets["Images/source.png"]["id"],
        "needle image label",
        "image",
        {"kind": "image", "bbox": [0.0, 0.0, 1.0, 1.0]},
        "image_region",
        "ocr",
    )
    result = workspace.search_supporting(case["id"], "needle", top_k=1)
    assert set(result["groups"]) == {"document", "audio", "image"}
    assert len(result["results"]) == 3 and result["top_k_per_group"] == 1
    assert result["groups"]["audio"][0]["unit_id"] == audio["id"]
    assert result["groups"]["image"][0]["unit_id"] == image["id"]
    assert result["groups"]["audio"][0]["locator"] == audio["locator"]
    assert all(len(rows) == 1 for rows in result["groups"].values())


@pytest.mark.parametrize("excluded", ["production_asset", "narration_scope", "origin"])
def test_eligibility_is_applied_before_fts_limit(library, excluded):
    workspace, case, _, assets = library
    asset = assets["LegalDocs/source.pdf"]
    eligible = passage(
        workspace, asset, "needle " + "less concentrated source context " * 60
    )
    filters = {}
    if excluded == "production_asset":
        production = assets["05_Production/draft.pdf"]
        assert workspace._is_production(production)
        many_passages(workspace, production, 520)
    elif excluded == "narration_scope":
        many_passages(workspace, asset, 520, scope="narration")
    else:
        many_passages(workspace, asset, 520, origin="ocr")
        filters["origin"] = "native_pdf"
    result = workspace.search_supporting(case["id"], "needle", filters, top_k=1)
    assert [row["unit_id"] for row in result["results"]] == [eligible["id"]]


def test_stale_active_versions_cannot_starve_current_passage(library):
    workspace, case, root, assets = library
    old = assets["LegalDocs/source.pdf"]
    old_units = many_passages(workspace, old, 520)
    (root / "LegalDocs" / "source.pdf").write_bytes(
        b"%PDF-1.4\n% new immutable evidence version\n"
    )
    workspace.import_folder(case["id"], root)
    current = workspace.get_asset(old["id"])
    assert current["asset_version_id"] != old["asset_version_id"]
    permit(workspace, current)
    current_unit = passage(
        workspace, current, "needle " + "current source context " * 80
    )
    # Simulate an old active-bit left by a historical importer; the version join
    # must still filter it before the capped FTS ranking.
    with workspace.repo.connect() as connection:
        connection.execute(
            "UPDATE evidence_units SET is_active=1 WHERE asset_version_id=?",
            (old["asset_version_id"],),
        )
    result = workspace.search_supporting(case["id"], "needle", top_k=1)
    assert [row["unit_id"] for row in result["results"]] == [current_unit["id"]]
    assert result["results"][0]["asset_version_id"] == current["asset_version_id"]
    assert not set(old_units).intersection(row["unit_id"] for row in result["results"])


@pytest.mark.parametrize("change", ["revoke", "replace_original", "supersede_evidence"])
def test_semantic_inference_rechecks_current_policy_version_and_evidence(
    library, monkeypatch, change
):
    workspace, case, root, assets = library
    asset = assets["LegalDocs/source.pdf"]
    unit = passage(workspace, asset, "Original indexed statement")
    vector(workspace, asset, unit["id"])
    configure(workspace, semantic_enabled=True)
    calls, action = [], []

    class FixtureModel:
        def encode(self, texts, **kwargs):
            calls.append(texts)
            assert kwargs["normalize_embeddings"] is True
            if action:
                if change == "revoke":
                    permit(workspace, asset, "blocked")
                elif change == "replace_original":
                    (root / "LegalDocs" / "source.pdf").write_bytes(
                        b"%PDF-1.4\nreplacement during inference\n"
                    )
                    workspace.import_folder(case["id"], root)
                    permit(workspace, workspace.get_asset(asset["id"]))
                else:
                    with workspace.repo.connect() as connection:
                        connection.execute(
                            "UPDATE evidence_units SET is_active=0 WHERE id=?",
                            (unit["id"],),
                        )
            return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(case_retrieval, "_text_model", lambda *a: FixtureModel())
    monkeypatch.setattr(case_retrieval, "_revision", lambda model, revision: revision)
    query = "semantic query absent from lexical fixture"
    baseline = workspace.search_supporting(case["id"], query)
    assert [row["unit_id"] for row in baseline["results"]] == [unit["id"]]
    assert "semantic" in baseline["results"][0]["scores"]
    action.append(True)
    result = workspace.search_supporting(case["id"], query)
    assert calls == [[query], [query]] and result["results"] == []
    assert result["groups"] == {}


def test_visual_group_retains_locator_and_never_claims_verified_identity(
    library, monkeypatch
):
    workspace, case, _, assets = library
    many_passages(workspace, assets["LegalDocs/source.pdf"], 520)
    audio = workspace.add_evidence_unit(
        assets["Audio/source.wav"]["id"],
        "needle audio source",
        "time",
        {"kind": "time", "start_ms": 0, "end_ms": 400},
        "transcript_segment",
        "asr",
    )
    image = assets["Images/source.png"]
    vector(
        workspace,
        image,
        image["asset_version_id"],
        "case_asset_version",
        modality="image",
        model="fixture-clip",
        input_hash=image["sha256"],
    )
    configure(workspace, visual_enabled=True)

    class VectorResult:
        def tolist(self):
            return [1.0, 0.0]

    model = SimpleNamespace(encode_text=lambda tokens: [VectorResult()])
    monkeypatch.setattr(
        visual,
        "_vision_model",
        lambda *a: (model, None, lambda text: text, "fixture-clip", "fixture-revision"),
    )
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(no_grad=nullcontext))
    result = workspace.search_supporting(case["id"], "needle", top_k=1)
    assert set(result["groups"]) == {"document", "audio", "image"}
    hit = result["groups"]["image"][0]
    assert (
        hit["asset_id"] == image["id"]
        and hit["asset_version_id"] == image["asset_version_id"]
    )
    assert hit["locator"] == {"kind": "image"} and hit["unit_id"] is None
    assert (
        hit["assertion_status"] == "visual_match"
        and hit["metadata"]["identity_verified"] is False
    )
    assert hit["confidence"] is None and "visual" in hit["scores"]
    assert result["groups"]["audio"][0]["unit_id"] == audio["id"]


def test_stale_visual_vectors_do_not_load_large_model(library, monkeypatch):
    workspace, case, root, assets = library
    image = assets["Images/source.png"]
    vector(
        workspace,
        image,
        image["asset_version_id"],
        "case_asset_version",
        modality="image",
        model="fixture-clip",
        input_hash=image["sha256"],
    )
    (root / "Images" / "source.png").write_bytes(b"replacement image inventory fixture")
    workspace.import_folder(case["id"], root)
    current = workspace.get_asset(image["id"])
    permit(workspace, current)
    assert current["asset_version_id"] != image["asset_version_id"]
    configure(workspace, visual_enabled=True)
    monkeypatch.setattr(
        visual,
        "_vision_model",
        lambda *a: pytest.fail("Superseded vectors must not load a vision model"),
    )
    assert (
        workspace.search_supporting(case["id"], "semantic visual query")["results"]
        == []
    )
