"""Independent adversarial checks for the documentary's evidence boundary."""

import json
from dataclasses import replace

import pytest

from app.models.search import SearchError
from app.services.targeted_search import SearchService
from app.services.targeted_search.case_workspace import CaseWorkspace


@pytest.fixture
def documentary_case(tmp_path):
    service = SearchService(tmp_path)
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Synthetic documentary case")
    root = workspace.repo.root / "owned" / "documentary-adversarial-fixture"
    root.mkdir(parents=True)
    for name in ("affidavit.pdf", "opinion.pdf"):
        (root / name).write_bytes(
            b"%PDF-1.4\nsynthetic fixture original\n" + name.encode()
        )
    assets = workspace.import_folder(case["id"], root)["assets"]
    assets.sort(key=lambda asset: asset["relative_path"])
    units, claims = [], []
    for index, asset in enumerate(assets):
        service.set_policy(
            asset["source_id"],
            "allowed_internal",
            "analysis,internal_review",
            "Synthetic fixture permission",
            "fixture-rights-reviewer",
        )
        text = (
            "The affidavit alleges a meeting took place on Tuesday."
            if index == 0
            else "The court found that a contract existed."
        )
        unit = workspace.add_evidence_unit(
            asset["id"],
            text,
            "page",
            {"page_index": 0, "page_label": "1"},
            "document_passage",
            "reviewed_annotation",
        )
        claim = workspace.save_claim(
            case["id"],
            {
                "text": text,
                "status": "reviewed",
                "reviewed_by": "fixture-fact-reviewer",
                "assertion_class": "allegation" if index == 0 else "court_finding",
                "citations": [{"unit_id": unit["id"], "relation": "supports"}],
            },
        )
        units.append(unit)
        claims.append(claim)
    return workspace, case, root, assets, units, claims


def _writer(workspace, response_generator=None):
    from app.services.targeted_search.documentary import DocumentaryWriter

    return DocumentaryWriter(workspace, response_generator=response_generator)


def _options(claims, **overrides):
    return {
        "title": "Synthetic case documentary",
        "target_minutes": 25,
        "claim_ids": [claim["id"] for claim in claims],
        **overrides,
    }


def _one_scene(
    claim_id, citation_id, *, text="The affidavit alleges a meeting.", quotes=None
):
    return {
        "chapters": [
            {
                "chapter_id": "chapter1",
                "title": "The claim",
                "scenes": [
                    {
                        "scene_id": "scene1",
                        "title": "A source account",
                        "passages": [
                            {
                                "passage_id": "passage1",
                                "text": text,
                                "claim_ids": [claim_id],
                                "citation_ids": [citation_id],
                                "quotes": quotes or [],
                            }
                        ],
                        "footage_queries": ["Exterior of an unspecified courthouse"],
                    }
                ],
            }
        ],
    }


def _draft_writer(workspace, case, claims, response_generator):
    """Reach the draft stage through a valid outline before testing bad output."""
    writer = _writer(workspace)
    packet = writer.build_packet(case["id"], [claim["id"] for claim in claims])
    supported = next(
        claim for claim in packet["claims"] if claim["id"] == claims[0]["id"]
    )
    outline = {
        "chapters": [
            {
                "chapter_id": "chapter1",
                "title": "The claim",
                "scenes": [
                    {
                        "scene_id": "scene1",
                        "title": "A source account",
                        "purpose": "Attribute the reviewed account",
                        "claim_ids": [claims[0]["id"]],
                        "citation_ids": supported["citation_ids"],
                    }
                ],
            }
        ]
    }
    writer.response_generator = lambda prompt: json.dumps(outline)
    document = writer.generate(case["id"], _options(claims))
    writer.response_generator = response_generator
    return writer, _options(claims, stage="draft", document_id=document["id"])


def test_explicit_selection_cannot_cross_case_boundary(documentary_case):
    workspace, case, _, _, _, claims = documentary_case
    other = workspace.create_case("A separate synthetic case")
    with pytest.raises(SearchError):
        _writer(workspace).build_packet(other["id"], [claims[0]["id"]])
    assert workspace.list_claims(case["id"])[0]["status"] == "reviewed"


def test_metadata_only_lead_cannot_substantiate_documentary_narration(documentary_case):
    workspace, case, _, _, _, _ = documentary_case
    source = workspace.search_service.discover(
        "local://documentary-metadata-only-fixture",
        metadata={"title": "A lead with no retained original"},
    )
    asset = workspace.link_source(
        case["id"], source["id"], category="Research", asset_kind="reference"
    )
    workspace.search_service.set_policy(
        source["id"],
        "allowed_internal",
        "analysis,internal_review",
        "Synthetic fixture permission",
        "fixture-rights-reviewer",
    )
    unit = workspace.add_evidence_unit(
        asset["id"],
        "A title describes a source, without its actual contents.",
        "metadata",
        {"field": "title"},
        "source_lead",
        "manual_note",
    )
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "The unavailable document proved an allegation.",
            "status": "reviewed",
            "reviewed_by": "fixture-fact-reviewer",
            "assertion_class": "court_finding",
            "citations": [{"unit_id": unit["id"]}],
        },
    )
    with pytest.raises(SearchError):
        _writer(workspace).build_packet(case["id"], [claim["id"]])


@pytest.mark.parametrize("relation", ["mentions", "contradicts"])
def test_reviewed_label_does_not_make_nonsupporting_citations_evidence(
    documentary_case, relation
):
    workspace, case, _, _, units, _ = documentary_case
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "The source is mentioned, but does not support this assertion.",
            "status": "reviewed",
            "reviewed_by": "fixture-fact-reviewer",
            "assertion_class": "court_finding",
            "citations": [{"unit_id": units[0]["id"], "relation": relation}],
        },
    )
    with pytest.raises(SearchError):
        _writer(workspace).build_packet(case["id"], [claim["id"]])


def test_selected_reviewed_claim_requires_current_source_rights(documentary_case):
    workspace, case, _, assets, _, claims = documentary_case
    workspace.search_service.set_policy(
        assets[0]["source_id"],
        "blocked",
        "internal_review",
        "Fixture rights revoked",
        "fixture-rights-reviewer",
    )
    with pytest.raises(SearchError):
        _writer(workspace).build_packet(case["id"], [claims[0]["id"]])


def test_selected_claim_cannot_use_tampered_retained_original(documentary_case):
    workspace, case, _, assets, _, claims = documentary_case
    workspace.asset_path(assets[0]["id"]).write_bytes(b"tampered original")
    with pytest.raises(SearchError):
        _writer(workspace).build_packet(case["id"], [claims[0]["id"]])


def test_replaced_original_invalidates_selected_claim(documentary_case):
    workspace, case, root, assets, _, claims = documentary_case
    (root / assets[0]["relative_path"]).write_bytes(b"replacement original")
    workspace.import_folder(case["id"], root)
    with pytest.raises(SearchError):
        _writer(workspace).build_packet(case["id"], [claims[0]["id"]])


@pytest.mark.parametrize("unknown", ["claim", "citation"])
def test_generated_passages_reject_invented_evidence_handles(documentary_case, unknown):
    workspace, case, _, _, _, claims = documentary_case
    packet = _writer(workspace).build_packet(case["id"], [claims[0]["id"]])
    output = _one_scene(
        "invented_claim" if unknown == "claim" else claims[0]["id"],
        "invented_citation" if unknown == "citation" else packet["citations"][0]["id"],
    )
    writer, options = _draft_writer(
        workspace, case, [claims[0]], lambda prompt: json.dumps(output)
    )
    with pytest.raises(SearchError):
        writer.generate(case["id"], options)


def test_passage_cannot_borrow_citation_from_unrelated_claim(documentary_case):
    workspace, case, _, _, _, claims = documentary_case
    packet = _writer(workspace).build_packet(case["id"])
    unrelated = next(
        claim for claim in packet["claims"] if claim["id"] == claims[1]["id"]
    )["citation_ids"][0]
    writer, options = _draft_writer(
        workspace,
        case,
        claims,
        lambda prompt: json.dumps(_one_scene(claims[0]["id"], unrelated)),
    )
    with pytest.raises(SearchError):
        writer.generate(case["id"], options)


def test_generated_direct_quote_must_match_the_retained_evidence(documentary_case):
    workspace, case, _, _, _, claims = documentary_case
    packet = _writer(workspace).build_packet(case["id"], [claims[0]["id"]])
    citation_id = packet["citations"][0]["id"]
    invented_quote = "I confessed to a crime that the source never describes."
    output = _one_scene(
        claims[0]["id"],
        citation_id,
        text=invented_quote,
        quotes=[{"citation_id": citation_id, "text": invented_quote}],
    )
    writer, options = _draft_writer(
        workspace, case, [claims[0]], lambda prompt: json.dumps(output)
    )
    with pytest.raises(SearchError, match="Direct quotes"):
        writer.generate(case["id"], options)


def test_valid_cited_draft_can_be_generated_without_final_approval(documentary_case):
    workspace, case, _, _, _, claims = documentary_case
    packet = _writer(workspace).build_packet(case["id"], [claims[0]["id"]])
    output = _one_scene(claims[0]["id"], packet["citations"][0]["id"])
    writer, options = _draft_writer(
        workspace, case, [claims[0]], lambda prompt: json.dumps(output)
    )
    record = writer.generate(case["id"], options)
    assert record["status"] == "draft_ready"
    assert record["revision"] == 2 and record["draft"]


def test_rights_revoked_during_generation_blocks_persistence(documentary_case):
    workspace, case, _, assets, _, claims = documentary_case
    packet = _writer(workspace).build_packet(case["id"], [claims[0]["id"]])
    output = _one_scene(claims[0]["id"], packet["citations"][0]["id"])

    def generate_then_revoke(prompt):
        workspace.search_service.set_policy(
            assets[0]["source_id"],
            "blocked",
            "internal_review",
            "Revoked while the provider was running",
            "fixture-rights-reviewer",
        )
        return json.dumps(output)

    writer, options = _draft_writer(workspace, case, [claims[0]], generate_then_revoke)
    with pytest.raises(SearchError, match="not permitted"):
        writer.generate(case["id"], options)
    with workspace.repo.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM case_documentaries").fetchone()[0]
            == 1
        )
    document = writer.list_documents(case["id"])[0]
    assert document["revision"] == 1
    assert (
        workspace.repo.get("case_documentaries", document["id"])["record"]["draft"]
        is None
    )


def test_reviewed_claim_edited_during_generation_blocks_persistence(documentary_case):
    workspace, case, _, _, units, claims = documentary_case
    packet = _writer(workspace).build_packet(case["id"], [claims[0]["id"]])
    output = _one_scene(claims[0]["id"], packet["citations"][0]["id"])

    def generate_then_edit_claim(prompt):
        workspace.save_claim(
            case["id"],
            {
                "id": claims[0]["id"],
                "text": "The assertion was revised after the packet was built.",
                "status": "reviewed",
                "reviewed_by": "fixture-fact-reviewer",
                "assertion_class": "allegation",
                "citations": [{"unit_id": units[0]["id"]}],
            },
        )
        return json.dumps(output)

    writer, options = _draft_writer(
        workspace, case, [claims[0]], generate_then_edit_claim
    )
    with pytest.raises(SearchError, match="claims or permissions changed"):
        writer.generate(case["id"], options)
    with workspace.repo.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM case_documentaries").fetchone()[0]
            == 1
        )
    document = writer.list_documents(case["id"])[0]
    assert document["revision"] == 1
    assert (
        workspace.repo.get("case_documentaries", document["id"])["record"]["draft"]
        is None
    )
