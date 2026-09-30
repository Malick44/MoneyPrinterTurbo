"""Executable staged narration workflow using authored, temporary evidence."""

import copy
import json

import pytest

from app.models.search import SearchError
from app.services.targeted_search import SearchService
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.documentary import DocumentaryWriter
from app.services.targeted_search.repository import json_text


def test_documentary_duration_defaults_and_cli_agree():
    from app.models.documentary import DocumentaryOptions
    from app.services.targeted_search.cli import parser

    assert DocumentaryOptions(title="Evidence documentary").target_minutes == 25
    args = parser().parse_args(
        ["case-documentary-write", "case-id", "Evidence documentary"]
    )
    assert args.minutes == 25
    for minutes in (22, 25, 28):
        assert (
            DocumentaryOptions(
                title="Evidence documentary", target_minutes=minutes
            ).target_minutes
            == minutes
        )


@pytest.mark.parametrize(
    "minutes", [0, 12, 21.99, 28.01, 180, float("nan"), float("inf")]
)
def test_documentary_generation_rejects_targets_outside_range(fixture_case, minutes):
    _, case, _, _, writer, _, _, _ = fixture_case
    with pytest.raises(SearchError) as error:
        writer.enqueue(
            case["id"], {"title": "Evidence documentary", "target_minutes": minutes}
        )
    assert error.value.status_code == 422
    assert writer.list_documents(case["id"]) == []


def test_writer_prompt_uses_duration_budget_and_resumes_legacy_target(fixture_case):
    _, case, _, _, writer, outline, draft, _ = fixture_case
    prompts = []

    def respond(prompt):
        payload = json.loads(prompt.split("\n", 1)[1])
        prompts.append((prompt, payload))
        return json.dumps(outline if payload["stage"] == "outline" else draft)

    writer.response_generator = respond
    document = writer.generate(case["id"], {"title": "Evidence documentary"})
    assert document["options"]["target_minutes"] == 25
    guidance = prompts[0][1]["duration_guidance"]
    assert guidance["target_spoken_words"] == 3625
    assert (guidance["min_spoken_words"], guidance["max_spoken_words"]) == (3190, 4060)
    assert "do not repeat facts or invent material" in prompts[0][0]
    # Simulate a saved project from before the new duration range; reading it
    # stays possible and regeneration applies the new target as a new revision.
    row = writer._row(case["id"], document["id"])
    row["record"]["options"]["target_minutes"] = 12
    legacy = writer._persist(case["id"], row["record"], row["revision"])
    assert (
        writer.get_document(case["id"], legacy["id"])["options"]["target_minutes"] == 12
    )
    regenerated = writer.generate(
        case["id"],
        {
            "title": "Evidence documentary",
            "document_id": legacy["id"],
            "stage": "draft",
            "target_minutes": 28,
        },
    )
    assert prompts[-1][1]["options"]["target_minutes"] == 28
    assert prompts[-1][1]["duration_guidance"]["target_spoken_words"] == 4060
    assert regenerated["options"]["target_minutes"] == 28
    assert regenerated["factual_review"] is None
    assert regenerated["human_review"] is None


def test_documentary_requests_a_long_response_budget(fixture_case, monkeypatch):
    from app.services import llm

    _, case, _, _, writer, outline, _, _ = fixture_case
    calls = []

    def respond(prompt, **kwargs):
        calls.append(kwargs)
        return json.dumps(outline)

    monkeypatch.setattr(llm, "_generate_response", respond)
    writer.response_generator = None
    writer.generate(case["id"], {"title": "Evidence documentary"})
    assert calls == [{"max_output_tokens": 16384}]


def test_model_context_preserves_sources_and_canonical_packet(fixture_case):
    workspace, case, asset, claim, writer, _, _, results = fixture_case
    support = claim["citations"][0]
    workspace.save_claim(
        case["id"],
        {
            **claim,
            "citations": [
                support,
                {
                    "unit_id": support["unit_id"],
                    "quote": "no weapon was recovered",
                    "relation": "contradicts",
                },
            ],
        },
    )
    workspace.save_claim(case["id"], {"text": "An unreviewed lead is not proof."})
    workspace.save_event(
        case["id"],
        {
            "title": "The reviewed meeting",
            "event_at": "2025-01-06",
            "time_precision": "day",
            "review_status": "reviewed",
            "reviewed_by": "editor",
            "claim_ids": [claim["id"]],
            "citations": [support],
        },
    )
    canonical = writer.build_packet(case["id"])
    prompts = []

    def respond(prompt):
        payload = json.loads(prompt.split("\n", 1)[1])
        prompts.append(payload)
        return json.dumps(results[payload["stage"]])

    writer.response_generator = respond
    record = stages(fixture_case)
    for payload in prompts:
        evidence = payload["evidence_packet"]
        assert evidence["claims"][0]["text"] == claim["text"]
        assert evidence["claims"][0]["assertion_class"] == "court_finding"
        assert evidence["claims"][0]["reviewed_by"] == "editor"
        assert (
            evidence["claims"][0]["citation_ids"]
            == canonical["claims"][0]["citation_ids"]
        )
        assert evidence["gaps"] == canonical["gaps"]
        assert evidence["timeline"][0]["event_at"] == "2025-01-06"
        assert evidence["timeline"][0]["claim_ids"] == [claim["id"]]
        assert (
            evidence["timeline"][0]["citation_ids"]
            == canonical["timeline"][0]["citation_ids"]
        )
        assert len(evidence["source_texts"]) == 1
        for citation in evidence["citations"]:
            original = next(
                item for item in canonical["citations"] if item["id"] == citation["id"]
            )
            assert evidence["source_texts"][citation["text_id"]] == original["text"]
            assert citation["quote"] == original["quote"]
            assert citation["relation"] == original["relation"]
            assert citation["filename"] == "Opinion.pdf"
            assert citation["locator"] == original["locator"]
            assert citation["asset_kind"] == "document"
            assert citation["origin"] == "native_pdf"
        assert {item["relation"] for item in evidence["citations"]} == {
            "supports",
            "contradicts",
        }
        assert "asset_sha256" not in evidence["citations"][0]
        assert "claim_hash" not in evidence["claims"][0]
    # Sharing excerpts is only a model context change. Saved revisions retain
    # original hashes, policies, unit IDs, source text and all attribution.
    assert record["packet"] == canonical
    assert writer._row(case["id"], record["id"])["record"]["packet"] == canonical
    assert record["packet"]["citations"][0]["asset_sha256"] == asset["sha256"]
    assert record["human_review"] is None
    workspace.search_service.set_policy(
        asset["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Updated permission review",
        "editor",
    )
    writer.response_generator = lambda _: pytest.fail("Stale evidence reached model")
    with pytest.raises(SearchError, match="permissions changed"):
        writer.generate(
            case["id"],
            {
                "title": "The finding",
                "document_id": record["id"],
                "stage": "factual_review",
            },
        )


def test_large_cited_draft_fits_without_dropping_evidence(fixture_case):
    _, case, _, _, writer, outline, draft, results = fixture_case
    packet = writer.build_packet(case["id"])
    citation = packet["citations"][0]
    excerpt = citation["text"] + " The indexed record provides attributed context." * 39
    packet["citations"] = [
        {
            **citation,
            "id": f"dcite_large_{index}",
            "text": f"Page {index % 42}: {excerpt}",
            "quote": excerpt[:400],
            "locator": {"kind": "page", "page_index": index % 42},
        }
        for index in range(61)
    ]
    packet["claims"] = [
        {
            **packet["claims"][0],
            "id": f"claim_large_{index}",
            "text": packet["claims"][0]["text"] * 5,
            "citation_ids": [f"dcite_large_{index}"],
        }
        for index in range(52)
    ]
    passage = draft["chapters"][0]["scenes"][0]["passages"][0]
    draft["chapters"][0]["scenes"][0]["passages"] = [
        {**passage, "passage_id": f"passage_{index}", "text": passage["text"] * 5}
        for index in range(100)
    ]
    captured = []

    def respond(prompt):
        captured.append(prompt)
        return json.dumps(results["factual_review"])

    writer.response_generator = respond
    writer._model("factual_review", packet, {"title": "Large fixture"}, outline, draft)
    prompt = captured[0]
    payload = json.loads(prompt.split("\n", 1)[1])
    assert len(prompt) <= 220000
    assert len(json_text({**payload, "evidence_packet": packet})) > 220000
    evidence = payload["evidence_packet"]
    assert len(evidence["claims"]) == 52
    assert len(evidence["citations"]) == 61
    for original, compact in zip(packet["citations"], evidence["citations"]):
        assert original["id"] == compact["id"]
        assert original["text"] == evidence["source_texts"][compact["text_id"]]
        assert original["quote"] == compact["quote"]
    assert payload["draft"] == draft
    assert payload["outline"] == outline


def test_compact_evidence_keeps_hard_prompt_budget(fixture_case):
    _, case, _, _, writer, outline, _, _ = fixture_case
    packet = writer.build_packet(case["id"])
    writer.response_generator = lambda _: pytest.fail("Oversized prompt reached model")
    with pytest.raises(SearchError, match="bounded context budget") as error:
        writer._model(
            "outline", packet, {"instructions": "x" * 220000}, outline=outline
        )
    assert error.value.status_code == 413


@pytest.fixture
def fixture_case(tmp_path):
    workspace = CaseWorkspace(SearchService(tmp_path))
    case = workspace.create_case(
        "Authored documentary fixture", "An attributed court finding"
    )
    folder = workspace.repo.root / "owned" / "input"
    folder.mkdir(parents=True)
    (folder / "Opinion.pdf").write_bytes(b"%PDF fixture retained source")
    asset = workspace.import_folder(case["id"], folder)["assets"][0]
    workspace.search_service.set_policy(
        asset["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Authored test evidence",
        "editor",
    )
    unit = workspace.add_evidence_unit(
        asset["id"],
        "The court found that the meeting occurred on Monday. The record says no weapon was recovered.",
        "page",
        {"page_index": 0},
        "document_passage",
        "native_pdf",
    )
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "The court found that the meeting occurred on Monday.",
            "status": "reviewed",
            "assertion_class": "court_finding",
            "reviewed_by": "editor",
            "citations": [
                {"unit_id": unit["id"], "quote": "the meeting occurred on Monday"}
            ],
        },
    )
    writer = DocumentaryWriter(workspace)
    packet = writer.build_packet(case["id"])
    citation = packet["citations"][0]["id"]
    outline = {
        "chapters": [
            {
                "chapter_id": "chapter_1",
                "title": "The finding",
                "scenes": [
                    {
                        "scene_id": "scene_1",
                        "title": "The record",
                        "purpose": "Attribute the finding to the court",
                        "claim_ids": [claim["id"]],
                        "citation_ids": [citation],
                        "footage_queries": ["courthouse exterior"],
                        "evidence_gaps": ["Exterior footage has not been acquired"],
                    }
                ],
            }
        ]
    }
    draft = {
        "chapters": [
            {
                "chapter_id": "chapter_1",
                "title": "The finding",
                "scenes": [
                    {
                        "scene_id": "scene_1",
                        "title": "The record",
                        "passages": [
                            {
                                "passage_id": "passage_1",
                                "text": 'The court found that "the meeting occurred on Monday". ['
                                + citation
                                + "]",
                                "claim_ids": [claim["id"]],
                                "citation_ids": [citation],
                                "quotes": [
                                    {
                                        "citation_id": citation,
                                        "text": "the meeting occurred on Monday",
                                    }
                                ],
                            }
                        ],
                        "footage_queries": ["courthouse exterior"],
                        "evidence_gaps": ["Exterior footage has not been acquired"],
                    }
                ],
            }
        ]
    }
    assessment = {
        "passages": [
            {
                "passage_id": "passage_1",
                "status": "supported",
                "reason": "The narrator explicitly attributes the exact finding to the court.",
                "citation_ids": [citation],
            }
        ],
        "notes": [],
    }
    results = {"outline": outline, "draft": draft, "factual_review": assessment}
    writer.response_generator = lambda prompt: json.dumps(
        results[json.loads(prompt.split("\n", 1)[1])["stage"]]
    )
    return workspace, case, asset, claim, writer, outline, draft, results


def stages(fixture):
    _, case, _, _, writer, _, _, _ = fixture
    options = {"title": "The finding", "target_minutes": 25, "language": "English"}
    record = writer.generate(case["id"], options)
    record = writer.generate(
        case["id"], {**options, "document_id": record["id"], "stage": "draft"}
    )
    record = writer.generate(
        case["id"], {**options, "document_id": record["id"], "stage": "factual_review"}
    )
    return record


def test_staged_workflow_human_review_export_and_script_handoff(fixture_case):
    workspace, case, _, _, writer, _, _, _ = fixture_case
    record = stages(fixture_case)
    assert record["revision"] == 3
    assert record["factual_review"]["review_kind"] == "model_assessment"
    assert not record["factual_review"]["human_approved"]
    assert record["human_review"] is None
    with pytest.raises(SearchError, match="human approval"):
        writer.export(case["id"], record["id"], final=True, expected_revision=3)
    record = writer.review(
        case["id"],
        record["id"],
        "human-editor",
        "Checked against original",
        expected_revision=3,
    )
    assert record["status"] == "approved" and record["revision"] == 4
    export = writer.export(case["id"], record["id"], final=True, expected_revision=4)
    assert export["script_asset_id"]
    assert len(export["files"]) == 5
    script = writer.export_content(
        case["id"], record["id"], "Final_Script.md", final=True, expected_revision=4
    )
    assert (
        script.read_text().strip()
        == 'The court found that "the meeting occurred on Monday".'
    )
    assert "dcite_" not in script.read_text()
    script_asset = workspace.get_asset(export["script_asset_id"])
    assert workspace._is_production(script_asset)
    assert script_asset["metadata"]["documentary_revision"] == 4
    assert script_asset["rights_status"] == "unknown"
    cited = writer.export_content(
        case["id"], record["id"], "Citation_Map.json", final=True, expected_revision=4
    )
    mapping = json.loads(cited.read_text())
    assert mapping["passages"][0]["char_start"] == 0
    assert mapping["passages"][0]["char_end"] == len(script.read_text().strip())
    assert mapping["human_review"]["reviewed_by"] == "human-editor"
    # Generated production transcripts/scripts never contaminate the evidence packet.
    assert (
        writer.build_packet(case["id"])["packet_hash"]
        == record["packet"]["packet_hash"]
    )
    with workspace.repo.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM documentary_revisions").fetchone()[
                0
            ]
            == 4
        )


def test_edit_invalidates_model_and_human_review_and_preserves_revisions(fixture_case):
    _, case, _, _, writer, _, draft, _ = fixture_case
    record = stages(fixture_case)
    record = writer.review(
        case["id"], record["id"], "editor", expected_revision=record["revision"]
    )
    revised = copy.deepcopy(draft)
    revised["chapters"][0]["scenes"][0]["footage_queries"] = [
        "archive courthouse entrance"
    ]
    edited = writer.save_revision(
        case["id"], record["id"], revised, expected_revision=4
    )
    assert edited["revision"] == 5
    assert edited["status"] == "draft_ready"
    assert edited["factual_review"] is None and edited["human_review"] is None
    with pytest.raises(SearchError, match="factual-review"):
        writer.review(case["id"], record["id"], "editor", expected_revision=5)
    with pytest.raises(SearchError, match="revision changed"):
        writer.save_revision(case["id"], record["id"], revised, expected_revision=4)


def test_queue_packet_pin_and_document_revision(fixture_case):
    _, case, _, _, writer, _, _, _ = fixture_case
    options = {"title": "The finding"}
    job = writer.enqueue(case["id"], options)
    assert job["job_type"] == "case_documentary"
    assert (
        job["payload"]["packet_hash"] == writer.build_packet(case["id"])["packet_hash"]
    )
    assert job["payload"]["document_revision"] is None
    with pytest.raises(SearchError, match="evidence changed"):
        writer.generate(case["id"], options, expected_packet_hash="obsolete")
    record = writer.generate(case["id"], options)
    next_job = writer.enqueue(
        case["id"], {**options, "stage": "draft", "document_id": record["id"]}
    )
    assert next_job["payload"]["document_revision"] == 1


def test_insufficient_model_assessment_never_allows_final_approval(fixture_case):
    _, case, _, _, writer, _, _, results = fixture_case
    results["factual_review"]["passages"][0]["status"] = "insufficient"
    record = stages(fixture_case)
    with pytest.raises(SearchError, match="Resolve factual-review"):
        writer.review(case["id"], record["id"], "editor", expected_revision=3)
    rejected = writer.review(
        case["id"], record["id"], "editor", approved=False, expected_revision=3
    )
    assert rejected["status"] == "changes_requested"
    assert not rejected["human_review"]["approved"]


def test_export_download_integrity_and_fixed_filename_guard(fixture_case):
    _, case, _, _, writer, _, _, _ = fixture_case
    record = stages(fixture_case)
    writer.export(case["id"], record["id"], expected_revision=3)
    with pytest.raises(SearchError, match="Unknown"):
        writer.export_content(case["id"], record["id"], "../../secrets")
    path = writer.export_content(
        case["id"], record["id"], "Cited_Draft.md", expected_revision=3
    )
    path.write_text("tampered", encoding="utf-8")
    with pytest.raises(SearchError, match="integrity"):
        writer.export_content(
            case["id"], record["id"], "Cited_Draft.md", expected_revision=3
        )


def test_empty_case_has_honest_packet_and_blocks_model(fixture_case):
    workspace, _, _, _, writer, _, _, _ = fixture_case
    case = workspace.create_case("Empty case", "No source evidence")
    packet = writer.build_packet(case["id"])
    assert packet["claims"] == [] and packet["citations"] == []
    with pytest.raises(SearchError, match="review supported claims"):
        writer.generate(case["id"], {"title": "Do not fabricate"})


def test_hidden_direct_quote_is_rejected_even_without_quote_records(fixture_case):
    _, case, _, _, writer, _, draft, results = fixture_case
    outline = writer.generate(case["id"], {"title": "The finding"})
    invalid = copy.deepcopy(draft)
    passage = invalid["chapters"][0]["scenes"][0]["passages"][0]
    passage["text"] = 'The suspect said "I secretly admitted everything".'
    passage["quotes"] = []
    results["draft"] = invalid
    with pytest.raises(SearchError, match="quoted narration"):
        writer.generate(
            case["id"],
            {"title": "The finding", "stage": "draft", "document_id": outline["id"]},
        )
    assert writer.get_document(case["id"], outline["id"])["revision"] == 1


def test_model_cannot_overwrite_a_concurrent_editor_revision(fixture_case):
    _, case, _, _, writer, _, draft, _ = fixture_case
    outline = writer.generate(case["id"], {"title": "The finding"})

    def edit_while_model_runs(prompt):
        writer.save_revision(case["id"], outline["id"], draft, expected_revision=1)
        return json.dumps(draft)

    writer.response_generator = edit_while_model_runs
    with pytest.raises(SearchError, match="revision changed"):
        writer.generate(
            case["id"],
            {"title": "The finding", "stage": "draft", "document_id": outline["id"]},
        )
    record = writer.get_document(case["id"], outline["id"])
    assert record["revision"] == 2 and record["status"] == "draft_ready"


def test_stale_claim_hides_document_content_and_blocks_approved_download(fixture_case):
    workspace, case, _, claim, writer, _, _, _ = fixture_case
    record = stages(fixture_case)
    record = writer.review(case["id"], record["id"], "editor", expected_revision=3)
    writer.export(case["id"], record["id"], final=True, expected_revision=4)
    edited_claim = {
        **claim,
        "text": "A revised court assertion requires new narration review.",
    }
    workspace.save_claim(case["id"], edited_claim)
    hidden = writer.get_document(case["id"], record["id"])
    assert hidden["stale"] and hidden["content_withheld"]
    assert "draft" not in hidden and "packet" not in hidden
    with pytest.raises(SearchError, match="claims or permissions changed"):
        writer.export_content(
            case["id"], record["id"], "Final_Script.md", final=True, expected_revision=4
        )


def test_model_output_budget_rejects_response_before_json_parse(fixture_case):
    _, case, _, _, writer, _, _, _ = fixture_case
    writer.response_generator = lambda prompt: "x" * 500001
    with pytest.raises(SearchError, match="text budget"):
        writer.generate(case["id"], {"title": "The finding"})
    assert writer.list_documents(case["id"]) == []
