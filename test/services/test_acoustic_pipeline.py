"""Word-grounded sound plans with real retained PCM WAVs and alignment imports."""

import json
import math
import struct
import wave
from dataclasses import replace

import pytest

from app.models.acoustic import CueEdit
from app.models.search import SearchError
from app.services.targeted_search import SearchService
from app.services.targeted_search import case_media, worker
from app.services.targeted_search.acoustic_pipeline import AcousticPipeline
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.repository import json_text
from app.services.targeted_search.sound_assets import register_sound


def wav(path, duration_ms=3000, hz=160):
    frames = round(duration_ms * 16000 / 1000)
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(
            b"".join(
                struct.pack("<h", round(1200 * math.sin(2 * math.pi * hz * i / 16000)))
                for i in range(frames)
            )
        )


@pytest.fixture
def acoustic_case(tmp_path):
    service = SearchService(tmp_path)
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Authored acoustic fixture")
    folder = service.repo.root / "owned" / "sound-input"
    folder.mkdir(parents=True)
    wav(folder / "Narration.wav")
    wav(folder / "Riser.wav", 2500, 600)
    wav(folder / "Raw_911_fixture.wav", 2000, 800)
    (folder / "Script.md").write_text("A pause then a revelation.", encoding="utf-8")
    imported = workspace.import_folder(case["id"], folder, category="05_Production")[
        "assets"
    ]
    assets = {asset["filename"]: asset for asset in imported}
    for asset in assets.values():
        service.set_policy(
            asset["source_id"],
            "allowed_internal",
            "analysis,internal_review",
            "Authored PCM fixture permission",
            "fixture-reviewer",
        )
    workspace.set_asset_state(
        assets["Narration.wav"]["id"], "imported", {"role": "narration"}
    )
    options = {
        "narration_asset_id": assets["Narration.wav"]["id"],
        "script_asset_id": assets["Script.md"]["id"],
        "title": "Sparse fixture mix",
    }
    alignment = case_media.import_whisperx(
        workspace,
        options["narration_asset_id"],
        {
            "audio_sha256": assets["Narration.wav"]["sha256"],
            "script_sha256": assets["Script.md"]["sha256"],
            "words": [
                {"word": "A", "start": 0.1, "end": 0.3},
                {"word": "pause", "start": 0.5, "end": 0.9},
                {"word": "then"},
                {"word": "a", "start": 1.2, "end": 1.4},
                {"word": "revelation.", "start": 2, "end": 2.4},
            ],
        },
        "narration",
        options["script_asset_id"],
    )
    options["transcript_artifact_id"] = alignment["transcript_artifact_id"]
    return workspace, case, folder, assets, options


def cue(**overrides):
    return {
        "cue_id": "reveal",
        "anchor_word_index": 4,
        "anchor": "end",
        "offset_ms": 0,
        "category": "riser",
        "tension": 0.7,
        "reason": "A brief editorial build before a revelation",
        "query": "restrained rising tension",
        "duration_ms": 2500,
        "gain_db": -18,
        **overrides,
    }


def analyze(fixture, cues=None, response_generator=None):
    workspace, case, _, _, options = fixture
    output = {
        "cues": [cue()] if cues is None else cues,
        "notes": ["Editorial effects are separate from source evidence."],
    }
    pipeline = AcousticPipeline(
        workspace, response_generator or (lambda prompt: json.dumps(output))
    )
    return pipeline, pipeline.analyze(case["id"], options)


def register(fixture):
    workspace, _, _, assets, _ = fixture
    return register_sound(
        workspace,
        assets["Riser.wav"]["id"],
        ["restrained", "rise", "tension"],
        "A synthetic ascending fixture tone",
        "riser",
    )


def edit(plan, **changes):
    cues = [
        CueEdit.model_validate(
            {key: value for key, value in item.items() if key in CueEdit.model_fields}
        ).model_dump()
        for item in plan["cues"]
    ]
    for item in cues:
        item.update(changes)
        item["duration_ms"] = next(
            cue for cue in plan["cues"] if cue["cue_id"] == item["cue_id"]
        )["requested_duration_ms"]
    return {"cues": cues, "mix": plan["mix"]}


def test_real_alignment_readiness_retains_null_word_and_scope(acoustic_case):
    workspace, case, _, assets, options = acoustic_case
    ready = AcousticPipeline(workspace).readiness(case["id"], options)
    assert ready["alignment_ready"] and ready["word_count"] == 5
    assert ready["unaligned_words"] == 1
    assert ready["duration_ms"] == 3000
    with workspace.repo.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM evidence_units WHERE asset_id=?",
                (assets["Narration.wav"]["id"],),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("index", [2, 500, None, True])
def test_model_cue_cannot_invent_word_anchor_or_use_null_alignment(
    acoustic_case, index
):
    workspace, case, _, _, options = acoustic_case
    output = {"cues": [cue(anchor_word_index=index)], "notes": []}
    pipeline = AcousticPipeline(workspace, lambda prompt: json.dumps(output))
    with pytest.raises(SearchError):
        pipeline.analyze(case["id"], options)
    assert pipeline.list_plans(case["id"]) == []


def test_real_registered_riser_ends_on_word_and_clips_source_beginning(acoustic_case):
    effect = register(acoustic_case)
    _, plan = analyze(acoustic_case)
    placed = plan["cues"][0]
    assert placed["asset_id"] == effect["id"] and placed["enabled"]
    assert placed["anchor_ms"] == 2000
    assert placed["start_ms"] == 0 and placed["source_start_ms"] == 500
    assert placed["duration_ms"] == 2000
    assert placed["start_ms"] + placed["duration_ms"] == placed["anchor_ms"]
    assert placed["match"]["method"] == "taxonomy+tags"


def test_start_effect_clips_at_narration_end(acoustic_case):
    register(acoustic_case)
    _, plan = analyze(acoustic_case, [cue(anchor="start")])
    placed = plan["cues"][0]
    assert placed["start_ms"] == 2000 and placed["duration_ms"] == 1000
    assert placed["source_start_ms"] == 0


def test_unregistered_source_audio_is_never_selected_and_unmatched_is_visible(
    acoustic_case,
):
    workspace, case, _, _, _ = acoustic_case
    pipeline, plan = analyze(acoustic_case)
    placed = plan["cues"][0]
    assert placed["asset_id"] is None and not placed["enabled"]
    assert placed["match"]["method"] == "unmatched" and placed["match"]["reason"]
    summary = pipeline.list_plans(case["id"])[0]
    assert summary["status"] == "needs_assets" and summary["can_mix"]
    # Re-enabling a missing effect is an explicit editing action and must fail.
    with pytest.raises(SearchError):
        pipeline.save_plan(
            case["id"], plan["id"], edit(plan, enabled=True), expected_revision=1
        )
    assert pipeline.get_plan(case["id"], plan["id"])["revision"] == 1
    saved = pipeline.save_plan(
        case["id"], plan["id"], edit(plan, enabled=False), expected_revision=1
    )
    assert saved["revision"] == 2 and not saved["cues"][0]["enabled"]
    job = pipeline.enqueue_mix(case["id"], plan["id"], expected_revision=2)
    assert job["job_type"] == "case_acoustic_mix"
    assert job["payload"]["revision"] == 2


@pytest.mark.parametrize("target", ["Narration.wav", "Script.md"])
def test_rights_revocation_during_model_blocks_plan_persistence(acoustic_case, target):
    workspace, case, _, assets, options = acoustic_case

    def response(prompt):
        workspace.search_service.set_policy(
            assets[target]["source_id"],
            "blocked",
            "internal_review",
            "Fixture revoked during model",
            "reviewer",
        )
        return json.dumps({"cues": [], "notes": []})

    pipeline = AcousticPipeline(workspace, response)
    with pytest.raises(SearchError, match="reviewed rights"):
        pipeline.analyze(case["id"], options)
    assert pipeline.list_plans(case["id"]) == []


@pytest.mark.parametrize("target", ["Narration.wav", "Script.md"])
def test_replacement_before_analysis_rejects_old_alignment(acoustic_case, target):
    workspace, case, folder, _, options = acoustic_case
    if target.endswith(".wav"):
        wav(folder / target, 3100, 250)
    else:
        (folder / target).write_text("Edited narration script.", encoding="utf-8")
    result = workspace.import_folder(case["id"], folder, category="05_Production")
    for asset in result["assets"]:
        workspace.search_service.set_policy(
            asset["source_id"],
            "allowed_internal",
            "analysis,internal_review",
            "Replacement fixture approval",
            "reviewer",
        )
    with pytest.raises(SearchError, match="superseded|matching narration"):
        AcousticPipeline(
            workspace, lambda prompt: json.dumps({"cues": [], "notes": []})
        ).analyze(case["id"], options)


def test_after_model_script_replacement_prevents_persistence(acoustic_case):
    workspace, case, folder, _, options = acoustic_case

    def response(prompt):
        (folder / "Script.md").write_text(
            "A different working script.", encoding="utf-8"
        )
        result = workspace.import_folder(case["id"], folder, category="05_Production")
        for asset in result["assets"]:
            workspace.search_service.set_policy(
                asset["source_id"],
                "allowed_internal",
                "analysis,internal_review",
                "Updated fixture approval",
                "reviewer",
            )
        return json.dumps({"cues": [], "notes": []})

    pipeline = AcousticPipeline(workspace, response)
    with pytest.raises(SearchError, match="superseded"):
        pipeline.analyze(case["id"], options)
    assert pipeline.list_plans(case["id"]) == []


def test_explicit_unregistered_source_and_other_case_effect_are_rejected(acoustic_case):
    workspace, case, _, assets, _ = acoustic_case
    register(acoustic_case)
    pipeline, plan = analyze(acoustic_case)
    with pytest.raises(SearchError, match="current authorized WAV"):
        pipeline.save_plan(
            case["id"],
            plan["id"],
            edit(plan, asset_id=assets["Raw_911_fixture.wav"]["id"]),
            expected_revision=1,
        )
    other = workspace.create_case("Other fixture case")
    with pytest.raises(SearchError, match="does not belong"):
        pipeline.get_plan(other["id"], plan["id"])


def test_cue_budget_duplicates_and_model_output_size_are_checked(acoustic_case):
    workspace, case, _, _, options = acoustic_case
    outputs = [
        json.dumps({"cues": [cue(), cue()], "notes": []}),
        "x" * 200001,
        "{ invalid json",
    ]
    for output in outputs:
        with pytest.raises(SearchError):
            AcousticPipeline(workspace, lambda prompt, value=output: value).analyze(
                case["id"], options
            )
    assert AcousticPipeline(workspace).list_plans(case["id"]) == []


def test_editing_and_queued_mix_pin_plan_revision_hash(acoustic_case):
    workspace, case, _, _, _ = acoustic_case
    register(acoustic_case)
    pipeline, plan = analyze(acoustic_case)
    queued = pipeline.enqueue_mix(case["id"], plan["id"], expected_revision=1)
    revised = pipeline.save_plan(
        case["id"], plan["id"], edit(plan, gain_db=-25), expected_revision=1
    )
    assert revised["revision"] == 2 and revised["content_hash"] != plan["content_hash"]
    with pytest.raises(SearchError, match="changed"):
        worker._dispatch(workspace.search_service, queued)
    with pytest.raises(SearchError, match="changed"):
        pipeline.save_plan(case["id"], plan["id"], edit(revised), expected_revision=1)
    with workspace.repo.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM acoustic_plan_versions WHERE plan_id=?",
                (plan["id"],),
            ).fetchone()[0]
            == 2
        )


def test_sound_registration_changes_make_existing_plan_stale(acoustic_case):
    workspace, case, _, assets, _ = acoustic_case
    register(acoustic_case)
    pipeline, plan = analyze(acoustic_case)
    register_sound(
        workspace,
        assets["Riser.wav"]["id"],
        ["modified", "tone"],
        "Changed editorial catalog",
        "riser",
    )
    with pytest.raises(SearchError, match="catalog changed"):
        pipeline.get_plan(case["id"], plan["id"])
    assert pipeline.list_plans(case["id"])[0]["content_withheld"]


def test_queue_uses_actual_alignment_hash_and_dispatches_same_pipeline(
    acoustic_case, monkeypatch
):
    from app.services import llm

    workspace, case, _, _, options = acoustic_case
    pipeline = AcousticPipeline(workspace)
    queued = pipeline.enqueue(case["id"], options)
    assert queued["job_type"] == "case_acoustic_analyze"
    assert (
        queued["payload"]["options"]["transcript_artifact_id"]
        == options["transcript_artifact_id"]
    )
    monkeypatch.setattr(
        llm,
        "_generate_response",
        lambda prompt: json.dumps({"cues": [], "notes": ["No effect is warranted."]}),
    )
    plan = worker._dispatch(workspace.search_service, queued)
    assert plan["cues"] == [] and plan["revision"] == 1
    with pytest.raises(SearchError, match="alignment changed"):
        pipeline.analyze(case["id"], options, expected_input_hash="outdated")


def test_plan_record_tampering_fails_hash_check(acoustic_case):
    workspace, case, _, _, _ = acoustic_case
    pipeline, plan = analyze(acoustic_case, [])
    row = workspace.repo.get("acoustic_plans", plan["id"])
    corrupted = {**row["record"], "title": "Tampered stored plan"}
    with workspace.repo.connect() as connection:
        connection.execute(
            "UPDATE acoustic_plans SET record_json=? WHERE id=?",
            (json_text(corrupted), plan["id"]),
        )
    with pytest.raises(SearchError, match="content verification"):
        pipeline.get_plan(case["id"], plan["id"])


def test_concurrent_editor_revision_is_preserved_and_outer_save_rejected(
    acoustic_case, monkeypatch
):
    workspace, case, _, _, _ = acoustic_case
    register(acoustic_case)
    pipeline, plan = analyze(acoustic_case)
    original = pipeline._placements
    nested = False

    def placements(*args, **kwargs):
        nonlocal nested
        computed = original(*args, **kwargs)
        if not nested:
            nested = True
            pipeline.save_plan(
                case["id"], plan["id"], edit(plan, gain_db=-27), expected_revision=1
            )
        return computed

    monkeypatch.setattr(pipeline, "_placements", placements)
    with pytest.raises(SearchError, match="changed"):
        pipeline.save_plan(
            case["id"], plan["id"], edit(plan, gain_db=-14), expected_revision=1
        )
    current = pipeline.get_plan(case["id"], plan["id"])
    assert current["revision"] == 2 and current["cues"][0]["gain_db"] == -27


def test_revocation_at_persistence_boundary_leaves_no_stale_plan(
    acoustic_case, monkeypatch
):
    workspace, case, _, assets, options = acoustic_case
    pipeline = AcousticPipeline(
        workspace, lambda prompt: json.dumps({"cues": [], "notes": []})
    )
    original = pipeline._persist

    def persist(*args, **kwargs):
        workspace.search_service.set_policy(
            assets["Narration.wav"]["source_id"],
            "blocked",
            "internal_review",
            "Revoked just before transaction",
            "reviewer",
        )
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "_persist", persist)
    with pytest.raises(SearchError, match="reviewed rights"):
        pipeline.analyze(case["id"], options)
    assert pipeline.list_plans(case["id"]) == []


def test_auto_alignment_queue_pins_inputs_and_imports_real_word_record(
    acoustic_case, monkeypatch
):
    workspace, case, _, assets, options = acoustic_case
    with workspace.repo.connect() as connection:
        connection.execute(
            "DELETE FROM case_transcripts WHERE asset_id=? AND scope='narration'",
            (assets["Narration.wav"]["id"],),
        )
    options = {
        key: value for key, value in options.items() if key != "transcript_artifact_id"
    } | {"auto_align": True}
    pipeline = AcousticPipeline(
        workspace, lambda prompt: json.dumps({"cues": [], "notes": []})
    )
    assert not pipeline.readiness(case["id"], options)["alignment_ready"]
    queued = pipeline.enqueue(case["id"], options)
    called = []

    def aligned(workspace_arg, asset_id, script_asset_id, scope):
        called.append((asset_id, script_asset_id, scope))
        return case_media.import_whisperx(
            workspace_arg,
            asset_id,
            {
                "audio_sha256": assets["Narration.wav"]["sha256"],
                "script_sha256": assets["Script.md"]["sha256"],
                "words": [{"word": "A", "start": 0.1, "end": 0.3}],
            },
            scope,
            script_asset_id,
        )

    monkeypatch.setattr(case_media, "align_audio", aligned)
    plan = pipeline.analyze(
        case["id"],
        queued["payload"]["options"],
        expected_input_hash=queued["payload"]["input_hash"],
    )
    assert called == [
        (assets["Narration.wav"]["id"], assets["Script.md"]["id"], "narration")
    ]
    assert plan["word_count"] == 1
    assert pipeline.readiness(case["id"], plan["options"])["alignment_ready"]


def approved_documentary_script(fixture):
    """Produce the actual reviewed writer handoff to audit acoustic dependencies."""
    from app.services.targeted_search.documentary import DocumentaryWriter

    workspace, case, _, _, options = fixture
    folder = workspace.repo.root / "owned" / "court-evidence"
    folder.mkdir()
    (folder / "CourtOpinion.pdf").write_bytes(b"%PDF authored evidence")
    asset = workspace.import_folder(case["id"], folder, category="01_Legal_Docs")[
        "assets"
    ][0]
    workspace.search_service.set_policy(
        asset["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Authored court fixture",
        "reviewer",
    )
    text = "The court found that an agreement existed."
    unit = workspace.add_evidence_unit(
        asset["id"], text, "page", {"page_index": 0}, "document_passage", "native_pdf"
    )
    claim = workspace.save_claim(
        case["id"],
        {
            "text": text,
            "status": "reviewed",
            "reviewed_by": "fact-reviewer",
            "assertion_class": "court_finding",
            "citations": [{"unit_id": unit["id"]}],
        },
    )
    writer = DocumentaryWriter(workspace)
    cite_id = writer.build_packet(case["id"], [claim["id"]])["citations"][0]["id"]
    outline = {
        "chapters": [
            {
                "chapter_id": "chapter1",
                "title": "The finding",
                "scenes": [
                    {
                        "scene_id": "scene1",
                        "title": "The record",
                        "purpose": "Attribute a court finding",
                        "claim_ids": [claim["id"]],
                        "citation_ids": [cite_id],
                    }
                ],
            }
        ]
    }
    draft = {
        "chapters": [
            {
                "chapter_id": "chapter1",
                "title": "The finding",
                "scenes": [
                    {
                        "scene_id": "scene1",
                        "title": "The record",
                        "passages": [
                            {
                                "passage_id": "passage1",
                                "text": text,
                                "claim_ids": [claim["id"]],
                                "citation_ids": [cite_id],
                            }
                        ],
                    }
                ],
            }
        ]
    }
    factual = {
        "passages": [
            {
                "passage_id": "passage1",
                "status": "supported",
                "reason": "Attributes the exact court finding to its source",
                "citation_ids": [cite_id],
            }
        ]
    }
    outputs = {"outline": outline, "draft": draft, "factual_review": factual}
    writer.response_generator = lambda prompt: json.dumps(
        outputs[json.loads(prompt.split("\n", 1)[1])["stage"]]
    )
    opts = {"title": "Authored court documentary", "claim_ids": [claim["id"]]}
    record = writer.generate(case["id"], opts)
    for stage in ("draft", "factual_review"):
        record = writer.generate(
            case["id"], {**opts, "stage": stage, "document_id": record["id"]}
        )
    record = writer.review(
        case["id"], record["id"], "human-editor", expected_revision=record["revision"]
    )
    exported = writer.export(
        case["id"], record["id"], final=True, expected_revision=record["revision"]
    )
    script = workspace.get_asset(exported["script_asset_id"])
    workspace.search_service.set_policy(
        script["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Explicit generated script review",
        "reviewer",
    )
    audio = workspace.get_asset(options["narration_asset_id"])
    imported = case_media.import_whisperx(
        workspace,
        audio["id"],
        {
            "audio_sha256": audio["sha256"],
            "script_sha256": script["sha256"],
            "words": [{"word": "The", "start": 0.1, "end": 0.3}],
        },
        "narration",
        script["id"],
    )
    options = {
        **options,
        "script_asset_id": script["id"],
        "transcript_artifact_id": imported["transcript_artifact_id"],
    }
    return writer, record, script, options


def test_later_documentary_revision_invalidates_approved_soundtrack_script(
    acoustic_case,
):
    workspace, case, _, _, _ = acoustic_case
    writer, document, script, options = approved_documentary_script(acoustic_case)
    pipeline = AcousticPipeline(
        workspace, lambda prompt: json.dumps({"cues": [], "notes": []})
    )
    plan = pipeline.analyze(case["id"], options)
    assert script["metadata"]["documentary_revision"] == document["revision"]
    assert script["metadata"]["documentary_content_hash"] == document["content_hash"]
    assert script["metadata"]["final_script_sha256"] == script["sha256"]
    # Even another approval of identical speech is a new reviewed writer revision.
    newer = writer.review(
        case["id"],
        document["id"],
        "second-editor",
        expected_revision=document["revision"],
    )
    assert newer["human_review"]["approved"]
    with pytest.raises(SearchError, match="current approval"):
        pipeline.get_plan(case["id"], plan["id"])
    assert pipeline.list_plans(case["id"])[0]["content_withheld"]


def test_changed_script_cannot_borrow_documentary_approval_metadata(acoustic_case):
    workspace, case, _, _, _ = acoustic_case
    _, _, script, options = approved_documentary_script(acoustic_case)
    # The SHA must agree with the bytes that received the writing review.
    workspace.set_asset_state(
        script["id"],
        "generated",
        {"final_script_sha256": "different-approved-script-sha"},
    )
    with pytest.raises(SearchError, match="current approval"):
        AcousticPipeline(workspace).readiness(case["id"], options)
