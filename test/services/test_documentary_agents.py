"""Craft agents preserve evidence boundaries and review exact narration versions."""

import copy
import json

import pytest

from app.models.documentary import (
    DocumentaryChapter,
    DocumentaryDraft,
    DocumentaryFactualReview,
    DocumentaryOutline,
)
from app.models.search import SearchError
from app.services.targeted_search import SearchService
from app.services.targeted_search.case_workspace import CaseWorkspace, digest
from app.services.targeted_search.documentary import DocumentaryWriter
from app.services.targeted_search.documentary_agents import (
    MAX_BLUEPRINT_BYTES,
    CraftBlueprint,
    DocumentaryNarrationWriter,
    DocumentaryNarrativeReviewer,
    NarrativeReview,
    build_writer_prompt,
    load_craft_blueprint,
    validate_narrative_review,
)


@pytest.fixture
def blueprint():
    return {
        "name": "Evidence questions",
        "hook_strategy": "Open with a supported object and a precise record question.",
        "narrative_arc": ["Question", "Evidence change", "Answer"],
        "reveal_strategy": "Resolve a question when the supporting evidence arrives.",
        "pacing_rules": ["Develop new evidence before restating an earlier point."],
        "narration_rules": ["Name whose account establishes a claim."],
        "transition_rules": ["Connect each evidence change to the next question."],
        "ending_strategy": "Answer the central question with a scoped outcome.",
        "audiovisual_rules": ["Suggest a retained document insert where appropriate."],
        "avoid_rules": ["Never invent biography, dialogue, audio or private thoughts."],
    }


@pytest.fixture
def craft_case(tmp_path):
    workspace = CaseWorkspace(SearchService(tmp_path))
    case = workspace.create_case("Synthetic craft case")
    folder = workspace.repo.root / "owned" / "synthetic-craft-input"
    folder.mkdir(parents=True)
    (folder / "Opinion.pdf").write_bytes(b"%PDF synthetic retained fixture")
    asset = workspace.import_folder(case["id"], folder)["assets"][0]
    workspace.search_service.set_policy(
        asset["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Synthetic permission",
        "fixture-editor",
    )
    unit = workspace.add_evidence_unit(
        asset["id"],
        "The court found that a meeting occurred. No weapon was recovered.",
        "page",
        {"page_index": 0},
        "document_passage",
        "native_pdf",
    )
    claim = workspace.save_claim(
        case["id"],
        {
            "text": "The court found that a meeting occurred.",
            "status": "reviewed",
            "assertion_class": "court_finding",
            "reviewed_by": "fixture-editor",
            "citations": [{"unit_id": unit["id"], "quote": "a meeting occurred"}],
        },
    )
    writer = DocumentaryWriter(workspace)
    packet = writer.build_packet(case["id"])
    citation = packet["citations"][0]["id"]
    outline, draft = {"chapters": []}, {"chapters": []}
    for number in (1, 2):
        common = {"chapter_id": f"chapter_{number}", "title": f"Chapter {number}"}
        scene = {
            "scene_id": f"scene_{number}",
            "title": f"Scene {number}",
            "claim_ids": [claim["id"]],
            "citation_ids": [citation],
            "footage_queries": [],
            "evidence_gaps": [],
        }
        outline["chapters"].append(
            {
                **common,
                "scenes": [{**scene, "purpose": "Explain the attributed finding."}],
            }
        )
        draft["chapters"].append(
            {
                **common,
                "scenes": [
                    {
                        "scene_id": scene["scene_id"],
                        "title": scene["title"],
                        "passages": [
                            {
                                "passage_id": f"passage_{number}",
                                "text": f"The court found that a meeting occurred. [{citation}]",
                                "claim_ids": [claim["id"]],
                                "citation_ids": [citation],
                                "quotes": [],
                            }
                        ],
                        "footage_queries": [],
                        "evidence_gaps": [],
                    }
                ],
            }
        )
    return workspace, case, asset, writer, packet, outline, draft


def _review(draft):
    return {
        "overall_score": 8,
        "verdict": "ready_for_editorial_review",
        "scene_reviews": [
            {
                "scene_id": scene["scene_id"],
                "score": 8,
                "story_function": "Develop the record question.",
                "transition_notes": "Connect the finding to the next evidence change.",
                "passage_reviews": [
                    {
                        "passage_id": passage["passage_id"],
                        "verdict": "effective",
                        "strengths": ["Local court attribution."],
                        "issues": [],
                    }
                    for passage in scene["passages"]
                ],
                "issues": [],
            }
            for chapter in draft["chapters"]
            for scene in chapter["scenes"]
        ],
        "notes": ["Craft assessment only; factual review remains separate."],
    }


def _payload(prompt):
    return json.loads(prompt.split("\n", 1)[1])


def test_private_blueprint_loader_bounds_and_extra_fields(tmp_path, blueprint):
    root = tmp_path / "private-output"
    root.mkdir()
    source = root / "craft.json"
    source.write_text(json.dumps(blueprint), encoding="utf-8")
    assert load_craft_blueprint(source, private_root=root).name == blueprint["name"]
    source.write_text(
        json.dumps(
            {**blueprint, "reference_transcript": "Do not import raw transcript."}
        )
    )
    with pytest.raises(SearchError, match="contract"):
        load_craft_blueprint(source, private_root=root)
    source.write_bytes(b"x" * (MAX_BLUEPRINT_BYTES + 1))
    with pytest.raises(SearchError, match="bounded data budget") as error:
        load_craft_blueprint(source, private_root=root)
    assert error.value.status_code == 413
    source.write_text("{invalid JSON")
    with pytest.raises(SearchError, match="valid private"):
        load_craft_blueprint(source, private_root=root)


def test_private_blueprint_loader_rejects_escape_and_symlinks(tmp_path, blueprint):
    root = tmp_path / "private-output"
    root.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(blueprint))
    link = root / "link.json"
    link.symlink_to(outside)
    for source in (outside, link, root / ".." / "outside.json"):
        with pytest.raises(SearchError, match="private output root"):
            load_craft_blueprint(source, private_root=root)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update(narrative_arc=[]),
        lambda value: value.update(hook_strategy="x" * 2001),
        lambda value: value.update(pacing_rules=["x" * 1201]),
        lambda value: value.update(narration_rules=["a"] * 13),
        lambda value: value.update(
            pacing_rules=["x" * 1200] * 12,
            narration_rules=["x" * 1200] * 12,
            transition_rules=["x" * 1200] * 12,
        ),
    ],
)
def test_blueprint_contract_rejects_unbounded_data(blueprint, mutation):
    mutation(blueprint)
    with pytest.raises(ValueError):
        CraftBlueprint.model_validate(blueprint)


def test_chapter_writer_uses_budgets_continuity_and_authoritative_service_validation(
    craft_case, blueprint
):
    _, case, _, writer, packet, outline, draft = craft_case
    captured = []

    def generate(prompt, output_type):
        payload = _payload(prompt)
        captured.append((prompt, payload, output_type))
        if payload["stage"] == "outline":
            return outline
        return next(
            chapter
            for chapter in draft["chapters"]
            if chapter["chapter_id"] == payload["chapter_request"]["chapter_id"]
        )

    writer.response_generator = DocumentaryNarrationWriter(
        blueprint,
        generate,
        chapter_by_chapter=True,
        chapter_word_targets={"chapter_1": 1625, "chapter_2": 2000},
    )
    record = writer.generate(
        case["id"], {"title": "Original narration", "target_minutes": 25}
    )
    record = writer.generate(
        case["id"],
        {"title": "Original narration", "document_id": record["id"], "stage": "draft"},
    )
    assert record["draft"] == draft
    assert record["packet"] == packet
    assert record["human_review"] is None and record["factual_review"] is None
    assert [item[2] for item in captured] == [
        DocumentaryOutline,
        DocumentaryChapter,
        DocumentaryChapter,
    ]
    requests = [item[1]["chapter_request"] for item in captured[1:]]
    assert [request["target_spoken_words"] for request in requests] == [1625, 2000]
    assert requests[0]["completed_chapter_ids"] == []
    assert requests[1]["completed_chapter_ids"] == ["chapter_1"]
    assert (
        requests[1]["preceding_exit_narration"]
        == draft["chapters"][0]["scenes"][0]["passages"][0]["text"]
    )
    assert all(item[1]["craft_blueprint"] == blueprint for item in captured)
    assert "only factual authority" in captured[1][0]
    assert "Never invent" in captured[1][0]
    assert "human approval" in captured[1][0]
    with pytest.raises(SearchError, match="factual-review"):
        writer.review(
            case["id"], record["id"], "editor", expected_revision=record["revision"]
        )


def test_chapter_writer_cannot_import_unknown_citations(craft_case, blueprint):
    _, case, _, writer, _, outline, draft = craft_case
    broken = copy.deepcopy(draft)
    broken["chapters"][0]["scenes"][0]["passages"][0]["citation_ids"] = [
        "dcite_not_in_source_packet"
    ]
    writer.response_generator = DocumentaryNarrationWriter(
        blueprint,
        lambda prompt, output_type: (
            outline if _payload(prompt)["stage"] == "outline" else broken
        ),
    )
    record = writer.generate(case["id"], {"title": "Original narration"})
    with pytest.raises(SearchError, match="unknown or duplicate"):
        writer.generate(
            case["id"],
            {
                "title": "Original narration",
                "document_id": record["id"],
                "stage": "draft",
            },
        )
    assert writer.get_document(case["id"], record["id"])["revision"] == 1


def test_source_permission_change_during_craft_generation_is_rejected(
    craft_case, blueprint
):
    workspace, case, asset, writer, _, outline, _ = craft_case

    def generate(prompt, output_type):
        workspace.search_service.set_policy(
            asset["source_id"],
            "allowed_internal",
            "analysis,internal_review",
            "Changed source permission snapshot",
            "fixture-editor",
        )
        return outline

    writer.response_generator = DocumentaryNarrationWriter(blueprint, generate)
    with pytest.raises(SearchError, match="permissions changed"):
        writer.generate(case["id"], {"title": "Original narration"})
    assert writer.list_documents(case["id"]) == []


def test_factual_review_is_forwarded_without_blueprint_or_craft_rewriting(blueprint):
    prompt = "Authoritative factual review instructions\n" + json.dumps(
        {"stage": "factual_review"}
    )
    result = {
        "passages": [
            {
                "passage_id": "passage_1",
                "status": "insufficient",
                "reason": "Assigned excerpt does not establish the assertion.",
                "citation_ids": [],
            }
        ],
        "notes": [],
    }
    captured = []

    def generate(value, output_type):
        captured.append((value, output_type))
        return result

    response = DocumentaryNarrationWriter(blueprint, generate)(prompt)
    assert captured == [(prompt, DocumentaryFactualReview)]
    assert json.loads(response) == result


@pytest.mark.parametrize(
    "targets",
    [
        {"chapter_1": 3625},
        {"chapter_1": 10, "chapter_2": 10},
        {"chapter_1": 2000, "chapter_2": 3000},
        {"chapter_1": True, "chapter_2": 3624},
    ],
)
def test_chapter_budget_rejects_missing_ids_or_padding_targets(
    craft_case, blueprint, targets
):
    _, _, _, _, packet, outline, _ = craft_case
    with pytest.raises(SearchError, match="Chapter word targets"):
        build_writer_prompt(
            "draft",
            evidence_packet=packet,
            options={},
            blueprint=blueprint,
            outline=outline,
            chapter_id="chapter_1",
            chapter_word_targets=targets,
        )


def test_chapter_writer_rejects_changed_scene_ids(craft_case, blueprint):
    _, case, _, writer, _, outline, draft = craft_case
    changed = copy.deepcopy(draft["chapters"][0])
    changed["scenes"][0]["scene_id"] = "invented_scene"
    writer.response_generator = DocumentaryNarrationWriter(
        blueprint,
        lambda prompt, output_type: (
            outline if _payload(prompt)["stage"] == "outline" else changed
        ),
        chapter_by_chapter=True,
    )
    record = writer.generate(case["id"], {"title": "Original narration"})
    with pytest.raises(SearchError, match="preserve its outline"):
        writer.generate(
            case["id"],
            {
                "title": "Original narration",
                "document_id": record["id"],
                "stage": "draft",
            },
        )


def test_narrative_reviewer_pins_every_scene_and_passage_without_human_approval(
    craft_case, blueprint
):
    _, _, _, _, _, _, draft = craft_case
    captured = []

    def generate(prompt, output_type):
        captured.append((prompt, output_type))
        return _review(draft)

    assessment = DocumentaryNarrativeReviewer(blueprint, generate).review(
        draft, options={"language": "English", "target_minutes": 25}
    )
    assert assessment.draft_hash == digest(draft)
    assert assessment.blueprint_hash == digest(blueprint)
    assert assessment.review_kind == "model_narrative_assessment"
    assert assessment.human_approved is False
    assert captured[0][1] is NarrativeReview
    payload = _payload(captured[0][0])
    assert payload["narration_metrics"]["spoken_words"] == 14
    assert payload["narration_metrics"]["estimated_minutes"] == 14 / 145
    assert "evidence_packet" not in payload
    assert "cannot establish factual support" in captured[0][0]
    assert "proportionate attention to victims" in captured[0][0]
    assert "adjacent passages create a causal implication" in captured[0][0]
    assert "proposed authentic footage/audio" in captured[0][0]
    assert "draft_hash" not in payload["output_schema"]["properties"]
    assert validate_narrative_review(assessment, draft, blueprint) == assessment


@pytest.mark.parametrize(
    "mutation",
    [
        lambda review: review["scene_reviews"].pop(),
        lambda review: review["scene_reviews"].append(
            copy.deepcopy(review["scene_reviews"][0])
        ),
        lambda review: review["scene_reviews"][0].update(scene_id="unknown_scene"),
        lambda review: review["scene_reviews"][0]["passage_reviews"][0].update(
            passage_id="unknown_passage"
        ),
        lambda review: review["scene_reviews"][0]["passage_reviews"].append(
            copy.deepcopy(review["scene_reviews"][0]["passage_reviews"][0])
        ),
        lambda review: review["scene_reviews"][0]["passage_reviews"][0].update(
            passage_id="passage_2"
        ),
    ],
)
def test_reviewer_rejects_missing_duplicate_unknown_and_cross_scene_coverage(
    craft_case, blueprint, mutation
):
    *_, draft = craft_case
    report = _review(draft)
    mutation(report)
    with pytest.raises(SearchError, match="every current"):
        DocumentaryNarrativeReviewer(blueprint, lambda prompt, model: report).review(
            draft
        )


@pytest.mark.parametrize(
    "scope,identifiers",
    [
        ("scene", ["unknown"]),
        ("scene", ["passage_2"]),
        ("scene", ["passage_1", "passage_1"]),
        ("passage", ["passage_2"]),
    ],
)
def test_reviewer_rejects_unrelated_issue_references(
    craft_case, blueprint, scope, identifiers
):
    *_, draft = craft_case
    report = _review(draft)
    issue = {
        "category": "transition",
        "severity": "minor",
        "passage_ids": identifiers,
        "reason": "The exit is abrupt.",
        "suggested_change": "Connect to the next record question.",
    }
    target = (
        report["scene_reviews"][0]
        if scope == "scene"
        else report["scene_reviews"][0]["passage_reviews"][0]
    )
    target["issues"] = [issue]
    with pytest.raises(SearchError, match="passage"):
        DocumentaryNarrativeReviewer(blueprint, lambda prompt, model: report).review(
            draft
        )


@pytest.mark.parametrize("invalid_score", [-1, 11, float("nan"), float("inf"), True])
def test_reviewer_rejects_invalid_scores(craft_case, blueprint, invalid_score):
    *_, draft = craft_case
    report = _review(draft)
    report["overall_score"] = invalid_score
    with pytest.raises(SearchError, match="contract"):
        DocumentaryNarrativeReviewer(blueprint, lambda prompt, model: report).review(
            draft
        )


def test_reviewer_cannot_supply_protected_hash_or_human_approval(craft_case, blueprint):
    *_, draft = craft_case
    report = {**_review(draft), "human_approved": True, "draft_hash": digest(draft)}
    with pytest.raises(SearchError, match="contract"):
        DocumentaryNarrativeReviewer(blueprint, lambda prompt, model: report).review(
            draft
        )


@pytest.mark.parametrize("change", ["draft", "blueprint"])
def test_review_is_stale_after_narration_or_blueprint_change(
    craft_case, blueprint, change
):
    *_, draft = craft_case
    assessment = DocumentaryNarrativeReviewer(
        blueprint, lambda prompt, model: _review(draft)
    ).review(draft)
    if change == "draft":
        draft["chapters"][0]["scenes"][0]["passages"][0]["text"] += (
            " Further explanation."
        )
    else:
        blueprint["hook_strategy"] = "Open with a different supported record question."
    with pytest.raises(SearchError, match="stale") as error:
        validate_narrative_review(assessment, draft, blueprint)
    assert error.value.status_code == 409


@pytest.mark.parametrize("change", ["draft", "blueprint"])
def test_reviewer_rejects_inputs_changed_while_model_runs(
    craft_case, blueprint, change
):
    *_, draft = craft_case
    report = _review(draft)

    def generate(prompt, output_type):
        if change == "draft":
            draft["chapters"][0]["scenes"][0]["passages"][0]["text"] += " Changed."
        else:
            reviewer.blueprint.hook_strategy = "A changed opening question."
        return report

    reviewer = DocumentaryNarrativeReviewer(blueprint, generate)
    with pytest.raises(SearchError, match="changed during narrative review") as error:
        reviewer.review(draft)
    assert error.value.status_code == 409


def test_revision_feedback_requires_its_exact_previous_draft(craft_case, blueprint):
    _, _, _, _, packet, outline, draft = craft_case
    assessment = DocumentaryNarrativeReviewer(
        blueprint, lambda prompt, model: _review(draft)
    ).review(draft)
    prompt = build_writer_prompt(
        "draft",
        evidence_packet=packet,
        options={},
        blueprint=blueprint,
        outline=outline,
        draft=draft,
        narrative_feedback=assessment,
    )
    assert _payload(prompt)["narrative_feedback"]["draft_hash"] == digest(draft)
    draft["chapters"][0]["scenes"][0]["passages"][0]["text"] += " Changed."
    with pytest.raises(SearchError, match="stale"):
        build_writer_prompt(
            "draft",
            evidence_packet=packet,
            options={},
            blueprint=blueprint,
            outline=outline,
            draft=draft,
            narrative_feedback=assessment,
        )


def test_major_issue_requires_revision_verdict(craft_case, blueprint):
    *_, draft = craft_case
    report = _review(draft)
    report["scene_reviews"][0]["issues"] = [
        {
            "category": "repetition",
            "severity": "major",
            "passage_ids": ["passage_1"],
            "reason": "The scene repeats the previous explanation.",
            "suggested_change": "Develop a new retained evidence question.",
        }
    ]
    with pytest.raises(SearchError, match="unresolved revision findings"):
        DocumentaryNarrativeReviewer(blueprint, lambda prompt, model: report).review(
            draft
        )
    report["verdict"] = "revise"
    assert (
        DocumentaryNarrativeReviewer(blueprint, lambda prompt, model: report)
        .review(draft)
        .verdict
        == "revise"
    )


def test_other_languages_and_legacy_missing_language_have_no_runtime_estimate(
    craft_case, blueprint
):
    *_, draft = craft_case
    captured = []

    def generate(prompt, output_type):
        captured.append(_payload(prompt))
        return _review(draft)

    reviewer = DocumentaryNarrativeReviewer(blueprint, generate)
    reviewer.review(draft, options={"language": "Chinese"})
    reviewer.review(draft)
    assert all(
        payload["narration_metrics"]["estimated_minutes"] is None
        for payload in captured
    )


def test_spoken_count_handles_punctuation_and_compound_words(craft_case, blueprint):
    *_, draft = craft_case
    draft["chapters"][0]["scenes"][0]["passages"][0]["text"] = (
        "The court-record—whose account? It's clear. … … [dcite_ignored]"
    )
    captured = []

    def generate(prompt, output_type):
        captured.append(_payload(prompt))
        return _review(draft)

    DocumentaryNarrativeReviewer(blueprint, generate).review(
        draft, options={"language": "en"}
    )
    assert captured[0]["narration_metrics"]["spoken_words"] == 13
    assert captured[0]["narration_metrics"]["estimated_minutes"] == 13 / 145


def test_prompt_limit_blocks_model_work(craft_case, blueprint):
    _, _, _, _, packet, _, _ = craft_case
    with pytest.raises(SearchError, match="bounded context budget") as error:
        build_writer_prompt(
            "outline",
            evidence_packet=packet,
            options={"instructions": "x" * 220000},
            blueprint=blueprint,
        )
    assert error.value.status_code == 413


def test_writer_callback_handles_full_draft_model_output(craft_case, blueprint):
    _, case, _, writer, _, outline, draft = craft_case

    def generate(prompt, output_type):
        return (
            DocumentaryOutline.model_validate(outline)
            if output_type is DocumentaryOutline
            else DocumentaryDraft.model_validate(draft)
        )

    writer.response_generator = DocumentaryNarrationWriter(blueprint, generate)
    record = writer.generate(case["id"], {"title": "Original narration"})
    record = writer.generate(
        case["id"],
        {"title": "Original narration", "document_id": record["id"], "stage": "draft"},
    )
    assert record["draft"] == draft
