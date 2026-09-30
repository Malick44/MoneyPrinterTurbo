"""An explicit sound bank cannot silently consume evidence or stale audio."""

import math
import struct
import wave
from dataclasses import replace

import pytest

from app.models.search import SearchError
from app.services.targeted_search import SearchService
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.sound_assets import (
    canonical_category,
    list_sounds,
    match_sound,
    register_sound,
    validate_sound,
)


def write_tone(path, duration=2, frequency=400, amplitude=0.4):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(
            b"".join(
                struct.pack(
                    "<h",
                    round(
                        amplitude
                        * 32767
                        * math.sin(2 * math.pi * frequency * index / 48000)
                    ),
                )
                for index in range(round(duration * 48000))
            )
        )


@pytest.fixture
def sound_bank(tmp_path):
    service = SearchService(tmp_path)
    service.settings = replace(
        service.settings,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Synthetic sound bank")
    folder = workspace.repo.root / "owned" / "sound-bank-fixture"
    write_tone(folder / "SFX" / "low_drone.wav")
    write_tone(folder / "SFX" / "sharp_hit.wav", frequency=800)
    assets = workspace.import_folder(case["id"], folder)["assets"]
    for asset in assets:
        workspace.search_service.set_policy(
            asset["source_id"],
            "allowed_internal",
            "analysis,internal_review",
            "Synthetic permission",
            "fixture-reviewer",
        )
    return workspace, case, folder, assets


def test_registered_taxonomy_matches_specific_exact_wav(sound_bank):
    workspace, case, _, assets = sound_bank
    drone = next(asset for asset in assets if asset["filename"] == "low_drone.wav")
    hit = next(asset for asset in assets if asset["id"] != drone["id"])
    register_sound(
        workspace, drone["id"], ["low", "ominous"], "Restrained low drone", "drone"
    )
    register_sound(
        workspace, hit["id"], ["sharp", "hit"], "Brief impact accent", "impact"
    )
    matched = match_sound(
        workspace, case["id"], {"category": "drone", "query": "low ominous drone"}
    )
    assert matched["asset_id"] == drone["id"]
    assert matched["sha256"] == drone["sha256"]
    assert matched["asset_version_id"] == drone["asset_version_id"]
    assert matched["method"] == "taxonomy+tags"
    assert matched["duration_ms"] == 2000
    assert matched["sound_metadata_sha256"]
    assert canonical_category("whoosh") == "transition"
    assert all(
        workspace._is_production(asset) for asset in list_sounds(workspace, case["id"])
    )


def test_unregistered_primary_audio_is_never_automatically_matched(sound_bank):
    workspace, case, _, _ = sound_bank
    assert list_sounds(workspace, case["id"]) == []
    with pytest.raises(SearchError, match="Register"):
        match_sound(workspace, case["id"], {"category": "drone", "query": "low drone"})


def test_missing_sound_category_does_not_substitute_unrelated_sfx(sound_bank):
    workspace, case, _, assets = sound_bank
    register_sound(workspace, assets[0]["id"], ["dark", "low"], category="drone")
    with pytest.raises(SearchError, match="No sound asset matches"):
        match_sound(
            workspace, case["id"], {"category": "impact", "query": "dark low reveal"}
        )


def test_audio_cited_as_case_evidence_cannot_be_repurposed(sound_bank):
    workspace, case, _, assets = sound_bank
    asset = assets[0]
    unit = workspace.add_evidence_unit(
        asset["id"],
        "Synthetic source observation",
        "time",
        {"start_ms": 0, "end_ms": 1000},
        "audio_transcript",
        "reviewed_transcript",
    )
    workspace.save_claim(
        case["id"],
        {
            "text": "The source contains a tone",
            "status": "reviewed",
            "reviewed_by": "fixture-reviewer",
            "assertion_class": "recording_observation",
            "citations": [{"unit_id": unit["id"]}],
        },
    )
    with pytest.raises(SearchError, match="Evidence cited"):
        register_sound(workspace, asset["id"], ["tone"], category="drone")
    assert workspace.get_asset(asset["id"])["metadata"]["role"] == "source_evidence"


def test_revoked_sound_is_skipped_without_hiding_other_available_assets(sound_bank):
    workspace, case, _, assets = sound_bank
    for asset in assets:
        register_sound(workspace, asset["id"], ["tone"], category="drone")
    workspace.search_service.set_policy(
        assets[0]["source_id"],
        "blocked",
        "internal_review",
        "Fixture revoked",
        "fixture-reviewer",
    )
    assert [asset["id"] for asset in list_sounds(workspace, case["id"])] == [
        assets[1]["id"]
    ]
    assert (
        match_sound(workspace, case["id"], {"category": "drone", "query": "tone"})[
            "asset_id"
        ]
        == assets[1]["id"]
    )


def test_replaced_and_tampered_registered_sounds_do_not_remain_available(sound_bank):
    workspace, case, folder, assets = sound_bank
    register_sound(workspace, assets[0]["id"], ["tone"], category="drone")
    write_tone(folder / assets[0]["relative_path"], frequency=300)
    workspace.import_folder(case["id"], folder)
    fresh = workspace.get_asset(assets[0]["id"])
    workspace.search_service.set_policy(
        fresh["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Fixture replacement permission",
        "fixture-reviewer",
    )
    assert list_sounds(workspace, case["id"]) == []
    with pytest.raises(SearchError, match="registration"):
        validate_sound(workspace, assets[0]["id"])
    register_sound(workspace, fresh["id"], ["tone"], category="drone")
    workspace.asset_path(fresh["id"]).write_bytes(b"corrupted bytes")
    assert list_sounds(workspace, case["id"]) == []


def test_wav_suffix_does_not_authorize_invalid_file(sound_bank):
    workspace, case, folder, _ = sound_bank
    (folder / "SFX" / "invalid.wav").write_bytes(b"not an audio stream")
    assets = workspace.import_folder(case["id"], folder)["assets"]
    invalid = next(asset for asset in assets if asset["filename"] == "invalid.wav")
    workspace.search_service.set_policy(
        invalid["source_id"],
        "allowed_internal",
        "analysis,internal_review",
        "Synthetic permission",
        "fixture-reviewer",
    )
    with pytest.raises(SearchError):
        register_sound(workspace, invalid["id"], ["tone"], category="drone")
    assert workspace.get_asset(invalid["id"])["metadata"]["role"] == "source_evidence"


def test_optional_vectors_are_pinned_to_current_sound_description(
    sound_bank, monkeypatch
):
    workspace, case, _, assets = sound_bank
    workspace.settings = replace(workspace.settings, semantic_enabled=True)
    workspace.search_service.settings = workspace.settings
    workspace.repo.settings = workspace.settings
    calls = []

    def vector(workspace, text):
        calls.append(text)
        return ([1.0, 0.0] if "drone" in text else [0.0, 1.0]), "fixture-model-v1"

    monkeypatch.setattr("app.services.targeted_search.sound_assets._vector", vector)
    register_sound(workspace, assets[0]["id"], ["low"], category="drone")
    first = match_sound(
        workspace, case["id"], {"category": "drone", "query": "low drone"}
    )
    assert first["method"] == "taxonomy+text_vector"
    register_sound(workspace, assets[0]["id"], ["sharp"], category="impact")
    second = match_sound(
        workspace, case["id"], {"category": "impact", "query": "sharp hit"}
    )
    assert first["sound_metadata_sha256"] != second["sound_metadata_sha256"]
    # Re-registering the same bank metadata reuses its immutable vector input.
    register_sound(workspace, assets[0]["id"], ["sharp"], category="impact")
    with workspace.repo.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM embeddings WHERE entity_type='sound_metadata'"
            ).fetchone()[0]
            == 2
        )
    assert len(calls) == 5
