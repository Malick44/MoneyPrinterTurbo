"""Real FFmpeg waveform checks and immutable acoustic-delivery guards."""

import array
import copy
import json
import math
from dataclasses import replace

import pytest

from app.models.search import SearchError
from app.services.targeted_search import SearchService
from app.services.targeted_search.acoustic_compositor import (
    authorize_mix_artifact,
    render_mix,
    validate_inputs,
)
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.media import executable, promote_artifact, run_command
from app.services.targeted_search.sound_assets import match_sound, register_sound
from test.services.test_sound_assets import write_tone


def _alignment(workspace, narration, script):
    staging = workspace.repo.root / "staging"
    staging.mkdir(exist_ok=True)
    alignment_path = staging / "alignment.json"
    alignment_path.write_text(
        json.dumps(
            {
                "words": [{"word": "synthetic", "start": 0.5, "end": 1}],
                "input_sha256": narration["sha256"],
            }
        )
    )
    alignment = promote_artifact(
        workspace.repo,
        alignment_path,
        source_id=narration["source_id"],
        kind="case_word_alignment",
        metadata={
            "asset_version_id": narration["asset_version_id"],
            "input_sha256": narration["sha256"],
        },
    )
    workspace.store_transcript(
        narration["id"],
        {
            "transcript_artifact_id": alignment["id"],
            "scope": "narration",
            "script_asset_id": script["id"],
            "script_asset_version_id": script["asset_version_id"],
            "input_script_sha256": script["sha256"],
        },
    )
    workspace.record_transcript_words(
        narration["id"],
        alignment["id"],
        [{"text": "synthetic", "start_ms": 500, "end_ms": 1000}],
    )
    return alignment


@pytest.fixture
def acoustic_fixture(tmp_path, monkeypatch):
    service = SearchService(tmp_path)
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Synthetic acoustic composition")
    folder = workspace.repo.root / "owned" / "acoustic-fixture"
    write_tone(
        folder / "05_Production" / "voiceover.wav", frequency=1000, amplitude=0.2
    )
    write_tone(folder / "SFX" / "low_drone.wav", frequency=400, amplitude=0.4)
    (folder / "05_Production" / "script.md").write_text("A synthetic acoustic test.")
    assets = workspace.import_folder(case["id"], folder)["assets"]
    by_name = {asset["filename"]: asset for asset in assets}
    narration, sound, script = [
        by_name[name] for name in ("voiceover.wav", "low_drone.wav", "script.md")
    ]
    for asset in assets:
        service.set_policy(
            asset["source_id"],
            "allowed_internal",
            "analysis,internal_review",
            "Synthetic permission",
            "fixture-reviewer",
        )
    register_sound(workspace, sound["id"], ["low", "drone"], category="drone")
    matched = match_sound(
        workspace, case["id"], {"category": "drone", "query": "low drone"}
    )
    alignment = _alignment(workspace, narration, script)
    record = {
        "id": "acoustic_fixture",
        "case_id": case["id"],
        "revision": 1,
        "content_hash": "fixture-plan-hash",
        "title": "Synthetic acoustic composition",
        "duration_ms": 2000,
        "narration_asset_id": narration["id"],
        "narration_asset_version_id": narration["asset_version_id"],
        "narration_sha256": narration["sha256"],
        "script_asset_id": script["id"],
        "script_asset_version_id": script["asset_version_id"],
        "script_sha256": script["sha256"],
        "transcript_artifact_id": alignment["id"],
        "alignment_sha256": alignment["sha256"],
        "cues": [
            {
                "cue_id": "cue1",
                "anchor_word_index": 0,
                "anchor_word": "synthetic",
                "anchor_ms": 500,
                "anchor": "start",
                "offset_ms": 0,
                "category": "drone",
                "tension": 0.5,
                "reason": "Synthetic cue",
                "query": "low drone",
                **matched,
                "enabled": True,
                "start_ms": 0,
                "source_start_ms": 0,
                "duration_ms": 2000,
                "gain_db": -4,
                "fade_in_ms": 0,
                "fade_out_ms": 0,
                "match": matched,
            }
        ],
        "mix": {"narration_gain_db": 0, "duck_db": -8, "headroom_db": -1},
    }
    # Plan revision/cue anchoring is verified separately by AcousticPipeline tests.
    monkeypatch.setattr(
        "app.services.targeted_search.acoustic_compositor._authorize_plan",
        lambda workspace, record: record,
    )
    return workspace, record, folder, narration, sound, script


def _samples(path):
    values = array.array("f")
    values.frombytes(
        run_command(
            [
                executable("ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(path),
                "-af",
                "pan=mono|c0=c0",
                "-ar",
                "48000",
                "-f",
                "f32le",
                "-",
            ],
            timeout=30,
        ).stdout
    )
    return values


def _amplitude(samples, frequency, start, end):
    first, last = round(start * 48000), round(end * 48000)
    segment = samples[first:last]
    phase = [
        2 * math.pi * frequency * (first + index) / 48000
        for index in range(len(segment))
    ]
    sine = (
        2
        * sum(value * math.sin(angle) for value, angle in zip(segment, phase))
        / len(segment)
    )
    cosine = (
        2
        * sum(value * math.cos(angle) for value, angle in zip(segment, phase))
        / len(segment)
    )
    return math.hypot(sine, cosine)


def test_real_mix_preserves_narration_and_ducks_effects_at_aligned_words(
    acoustic_fixture,
):
    workspace, record, _, _, _, _ = acoustic_fixture
    result = render_mix(workspace, record)
    path = authorize_mix_artifact(workspace, result["artifact_id"])
    samples = _samples(path)
    assert len(samples) == 96000
    narration_before = _amplitude(samples, 1000, 0.1, 0.3)
    narration_during = _amplitude(samples, 1000, 0.6, 0.9)
    effects_before = _amplitude(samples, 400, 0.1, 0.3)
    effects_during = _amplitude(samples, 400, 0.6, 0.9)
    assert narration_during == pytest.approx(narration_before, rel=0.025)
    assert effects_during / effects_before == pytest.approx(10 ** (-8 / 20), rel=0.025)
    timeline = json.loads(
        authorize_mix_artifact(workspace, result["timeline_artifact_id"]).read_text()
    )
    assert timeline["duck_intervals_ms"] == [[460, 1120]]
    assert timeline["sample_rate"] == 48000
    assert timeline["cues"][0]["sha256"] == record["cues"][0]["sha256"]
    otio = json.loads(
        authorize_mix_artifact(workspace, result["otio_artifact_id"]).read_text()
    )
    assert otio["OTIO_SCHEMA"] == "Timeline.1"
    assert len(otio["tracks"]["children"]) == 2
    assert (
        otio["tracks"]["children"][1]["children"][0]["source_range"]["duration"][
            "value"
        ]
        == 96000
    )


def test_cue_onset_uses_requested_sample_clock_and_source_window(acoustic_fixture):
    workspace, record, _, _, _, _ = acoustic_fixture
    record["cues"][0].update(start_ms=400, source_start_ms=200, duration_ms=1000)
    record["mix"]["duck_db"] = 0
    result = render_mix(workspace, record)
    samples = _samples(authorize_mix_artifact(workspace, result["artifact_id"]))
    assert _amplitude(samples, 400, 0.1, 0.3) < 0.0001
    assert _amplitude(samples, 400, 0.42, 0.62) == pytest.approx(
        0.4 * 10 ** (-4 / 20) / math.sqrt(2), rel=0.025
    )
    assert _amplitude(samples, 400, 1.5, 1.7) < 0.0001
    otio = json.loads(
        authorize_mix_artifact(workspace, result["otio_artifact_id"]).read_text()
    )
    gap, clip = otio["tracks"]["children"][1]["children"]
    assert gap["source_range"]["duration"]["value"] == 19200
    assert clip["source_range"]["start_time"]["value"] == 9600


def test_render_uses_canonical_plan_cues_instead_of_untrusted_caller_fields(
    acoustic_fixture, monkeypatch
):
    workspace, record, _, _, _, _ = acoustic_fixture
    canonical = copy.deepcopy(record)
    canonical["cues"][0].update(start_ms=400, duration_ms=1000)
    monkeypatch.setattr(
        "app.services.targeted_search.acoustic_compositor._authorize_plan",
        lambda workspace, record: canonical,
    )
    result = render_mix(workspace, record)
    timeline = json.loads(
        authorize_mix_artifact(workspace, result["timeline_artifact_id"]).read_text()
    )
    assert timeline["cues"][0]["start_ms"] == 400


def test_limiter_enforces_headroom_without_auto_makeup_gain(acoustic_fixture):
    workspace, record, folder, narration, _, script = acoustic_fixture
    write_tone(folder / narration["relative_path"], frequency=1000, amplitude=0.8)
    workspace.import_folder(record["case_id"], folder)
    fresh = workspace.get_asset(narration["id"])
    # This waveform-specific test updates its fixture snapshot and transcript scope.
    workspace.search_service.set_policy(
        fresh["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "New fixture permission",
        "fixture-reviewer",
    )
    record["narration_asset_version_id"], record["narration_sha256"] = (
        fresh["asset_version_id"],
        fresh["sha256"],
    )
    alignment = _alignment(workspace, fresh, script)
    record["transcript_artifact_id"], record["alignment_sha256"] = (
        alignment["id"],
        alignment["sha256"],
    )
    record["cues"] = []
    record["mix"]["narration_gain_db"] = 6
    result = render_mix(workspace, record)
    samples = _samples(authorize_mix_artifact(workspace, result["artifact_id"]))
    assert max(abs(value) for value in samples) <= 10 ** (-1 / 20) + 0.00001
    assert max(abs(value) for value in samples) > 0.8


@pytest.mark.parametrize(
    "change",
    [
        "unmatched",
        "bool_time",
        "out_of_bounds",
        "bad_gain",
        "overlap_fades",
        "stale_sound",
        "source_scope",
    ],
)
def test_invalid_or_unmatched_cues_fail_before_ffmpeg(acoustic_fixture, change):
    workspace, record, _, _, _, _ = acoustic_fixture
    cue = record["cues"][0]
    if change == "unmatched":
        cue["asset_id"] = None
    elif change == "bool_time":
        cue["start_ms"] = True
    elif change == "out_of_bounds":
        cue["source_start_ms"] = 1
    elif change == "bad_gain":
        cue["gain_db"] = float("nan")
    elif change == "overlap_fades":
        cue.update(fade_in_ms=1500, fade_out_ms=1500)
    elif change == "stale_sound":
        cue["asset_version_id"] = "old-version"
    else:
        with workspace.repo.connect() as connection:
            connection.execute("UPDATE case_transcripts SET scope='source'")
    with pytest.raises(SearchError):
        validate_inputs(workspace, record)


def test_explicitly_disabled_unmatched_cue_is_retained_but_not_rendered(
    acoustic_fixture,
):
    workspace, record, _, _, _, _ = acoustic_fixture
    record["cues"][0].update(asset_id=None, enabled=False)
    result = render_mix(workspace, record)
    timeline = json.loads(
        authorize_mix_artifact(workspace, result["timeline_artifact_id"]).read_text()
    )
    assert timeline["cues"] == []
    assert len(timeline["disabled_cues"]) == 1
    assert (
        _amplitude(
            _samples(authorize_mix_artifact(workspace, result["artifact_id"])),
            400,
            0.1,
            0.3,
        )
        < 0.0001
    )


def test_taxonomy_edit_invalidates_already_matched_sound(acoustic_fixture):
    workspace, record, _, _, sound, _ = acoustic_fixture
    register_sound(workspace, sound["id"], ["new-description"], category="impact")
    with pytest.raises(SearchError, match="taxonomy"):
        validate_inputs(workspace, record)


def test_revocation_during_ffmpeg_prevents_artifact_promotion(
    acoustic_fixture, monkeypatch
):
    workspace, record, _, _, sound, _ = acoustic_fixture
    actual = run_command

    def revoke_after_encoding(args, timeout=900):
        result = actual(args, timeout=timeout)
        if "-filter_complex" in args:
            workspace.search_service.set_policy(
                sound["source_id"],
                "blocked",
                "internal_review",
                "Fixture revoked during render",
                "fixture-reviewer",
            )
        return result

    monkeypatch.setattr(
        "app.services.targeted_search.acoustic_compositor.run_command",
        revoke_after_encoding,
    )
    with pytest.raises(SearchError):
        render_mix(workspace, record)
    with workspace.repo.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE kind LIKE 'acoustic_%'"
            ).fetchone()[0]
            == 0
        )


def test_delivery_rechecks_current_source_policy(acoustic_fixture):
    workspace, record, _, _, sound, _ = acoustic_fixture
    result = render_mix(workspace, record)
    workspace.search_service.set_policy(
        sound["source_id"],
        "blocked",
        "internal_review",
        "Fixture revoked before delivery",
        "fixture-reviewer",
    )
    for key in ("artifact_id", "timeline_artifact_id", "otio_artifact_id"):
        with pytest.raises(SearchError):
            authorize_mix_artifact(workspace, result[key])


def test_same_source_from_another_case_cannot_be_used_as_editorial_audio(
    acoustic_fixture,
):
    workspace, record, _, _, sound, _ = acoustic_fixture
    other = workspace.create_case("Other acoustic case")
    other_asset = workspace.link_source(
        other["id"], sound["source_id"], category="SFX", asset_kind="audio"
    )
    register_sound(workspace, other_asset["id"], ["drone"], category="drone")
    wrong = copy.deepcopy(record)
    wrong["cues"][0].update(
        asset_id=other_asset["id"],
        asset_version_id=other_asset["asset_version_id"],
        sha256=other_asset["sha256"],
    )
    with pytest.raises(SearchError, match="different case"):
        validate_inputs(workspace, wrong)
