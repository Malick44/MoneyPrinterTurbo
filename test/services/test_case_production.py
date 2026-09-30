"""Real mixed-media rendering, source sound and delivery authorization."""

from dataclasses import replace
import json
import math
from pathlib import Path
import struct
import subprocess
import wave

import pytest
from PIL import Image

from app.models.search import SearchError
from app.services.targeted_search.case_production import (
    authorize_render_artifact,
    render_storyboard,
)
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.media import executable
from app.services.targeted_search.operations import backup_repository, restore_backup
from app.services.targeted_search.service import SearchService
from app.services.targeted_search.worker import process_once


def _tone(path, hz=440):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(
            b"".join(
                struct.pack("<hh", value, value)
                for index in range(32000)
                if (value := round(8000 * math.sin(index * hz * 2 * math.pi / 16000)))
                is not None
            )
        )


@pytest.fixture
def case(tmp_path):
    service = SearchService(tmp_path / "library")
    service.settings = replace(
        service.settings,
        enabled=True,
        semantic_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
        rerank_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Owned renderer fixtures")
    incoming = workspace.repo.root / "owned" / "incoming"
    incoming.mkdir(parents=True)
    Image.new("RGB", (320, 240), "blue").save(incoming / "Courthouse.png")
    _tone(incoming / "Source_Call.wav")
    _tone(incoming / "Narration.wav", 880)
    imported = workspace.import_folder(case["id"], incoming)
    assets = {asset["filename"]: asset for asset in imported["assets"]}
    for asset in assets.values():
        service.set_policy(
            asset["source_id"],
            "allowed_export",
            "internal_review,analysis,generated_export",
            "Owned synthetic test assets",
            "test-owner",
        )
    return workspace, case, assets, incoming


def _story(workspace, case, assets, narration=False):
    record = {
        "title": "Explicit quote placement",
        "scenes": [
            {
                "scene_id": "exterior",
                "asset_id": assets["Courthouse.png"]["id"],
                "duration_ms": 1000,
                "role": "still",
            },
            {
                "scene_id": "call",
                "asset_id": assets["Source_Call.wav"]["id"],
                "source_start_ms": 500,
                "source_end_ms": 1500,
                "duration_ms": 1000,
                "role": "original_sound",
                "visual_asset_id": assets["Courthouse.png"]["id"],
            },
        ],
    }
    if narration:
        record["narration_asset_id"] = assets["Narration.wav"]["id"]
        record["scenes"].append(
            {
                "scene_id": "after_quote",
                "asset_id": assets["Courthouse.png"]["id"],
                "duration_ms": 1000,
                "role": "still",
            }
        )
    return workspace.save_storyboard(case["id"], record)


def _audio_samples(path):
    result = subprocess.run(
        [
            executable("ffmpeg"),
            "-v",
            "error",
            "-i",
            str(path),
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-f",
            "f32le",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    return struct.unpack("<" + "f" * (len(result.stdout) // 4), result.stdout)


def _rms(samples):
    return math.sqrt(sum(value * value for value in samples) / len(samples))


def test_render_preserves_explicit_original_sound_and_provenance(case):
    workspace, case, assets, _ = case
    storyboard = _story(workspace, case, assets)
    job = workspace.enqueue_render(storyboard["id"])
    completed = process_once(workspace.search_service)
    assert completed["id"] == job["id"]
    assert completed["status"] == "complete", completed.get("last_error")
    result = completed["result"]
    path = authorize_render_artifact(workspace, result["artifact_id"])
    samples = _audio_samples(path)
    assert _rms(samples[3200:12800]) < 0.001
    assert _rms(samples[19200:28800]) > 0.08
    manifest = json.loads(
        authorize_render_artifact(workspace, result["manifest_artifact_id"]).read_text()
    )
    assert manifest["scenes"][1]["source_start_ms"] == 500
    assert manifest["scenes"][1]["source_end_ms"] == 1500
    assert manifest["scenes"][1]["output_start_ms"] == 1000
    assert manifest["scenes"][1]["original_audio"] is True
    workspace.search_service.set_policy(
        assets["Source_Call.wav"]["source_id"],
        "blocked",
        "internal_review",
        "Revoked fixture permission",
        "test-owner",
    )
    with pytest.raises(SearchError, match="reviewed rights"):
        authorize_render_artifact(workspace, result["artifact_id"])


def test_narration_is_audible_over_visuals_and_quote_keeps_source_sound(case):
    workspace, case, assets, _ = case
    result = render_storyboard(
        workspace, _story(workspace, case, assets, narration=True)["id"]
    )
    samples = _audio_samples(
        authorize_render_artifact(workspace, result["artifact_id"])
    )
    assert _rms(samples[3200:12800]) > 0.08
    assert _rms(samples[19200:28800]) > 0.08
    assert _rms(samples[35200:44800]) > 0.08
    manifest = json.loads(
        authorize_render_artifact(workspace, result["manifest_artifact_id"]).read_text()
    )
    assert manifest["narration_time_mappings"][1]["source_start_ms"] == 1000
    assert manifest["narration_time_mappings"][1]["output_start_ms"] == 2000
    assert manifest["mux_timing"]
    assert manifest["duration_ms"] == result["duration_ms"]


def test_queued_render_rejects_replaced_source_even_with_new_permission(case):
    workspace, case, assets, incoming = case
    storyboard = _story(workspace, case, assets)
    workspace.enqueue_render(storyboard["id"])
    _tone(incoming / "Source_Call.wav", 660)
    workspace.import_folder(case["id"], incoming)
    workspace.search_service.set_policy(
        assets["Source_Call.wav"]["source_id"],
        "allowed_export",
        "internal_review,analysis,generated_export",
        "Reviewed replacement",
        "test-owner",
    )
    completed = process_once(workspace.search_service)
    assert completed["status"] == "failed"
    assert "changed" in completed["last_error"]


def test_case_original_paths_and_render_survive_backup_restore(case, tmp_path):
    workspace, case, assets, _ = case
    storyboard = _story(workspace, case, assets)
    backup = tmp_path / "backup"
    backup_repository(workspace.repo.root, backup)
    destination = tmp_path / "restored"
    restored = restore_backup(backup, destination)
    assert restored["missing_owned_source_ids"] == []
    service = SearchService(destination)
    service.settings = workspace.settings
    service.repo.settings = service.settings
    recovered = CaseWorkspace(service)
    for asset in assets.values():
        assert recovered.asset_path(asset["id"]).is_relative_to(destination)
        assert Path(
            service.repo.get("sources", asset["source_id"])["local_path"]
        ).is_relative_to(destination)
    result = render_storyboard(recovered, storyboard["id"])
    assert authorize_render_artifact(recovered, result["artifact_id"]).is_file()


def test_selected_audio_channel_and_background_crop_are_rendered(case):
    workspace, case, assets, incoming = case
    with wave.open(str(incoming / "Source_Call.wav"), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(
            b"".join(
                struct.pack(
                    "<hh", 0, round(8000 * math.sin(index * 440 * 2 * math.pi / 16000))
                )
                for index in range(32000)
            )
        )
    image = Image.new("RGB", (320, 240), "red")
    image.paste("blue", (160, 0, 320, 240))
    image.save(incoming / "Courthouse.png")
    workspace.import_folder(case["id"], incoming)
    for asset in assets.values():
        workspace.search_service.set_policy(
            asset["source_id"],
            "allowed_export",
            "internal_review,analysis,generated_export",
            "Owned updated fixture",
            "test-owner",
        )
    storyboard = workspace.save_storyboard(
        case["id"],
        {
            "title": "Channel and crop",
            "scenes": [
                {
                    "scene_id": "selected",
                    "asset_id": assets["Source_Call.wav"]["id"],
                    "duration_ms": 1000,
                    "role": "original_sound",
                    "locator": {
                        "kind": "time",
                        "start_ms": 0,
                        "end_ms": 1000,
                        "channel": 0,
                    },
                    "visual_asset_id": assets["Courthouse.png"]["id"],
                    "visual_locator": {"kind": "image", "bbox": [0.5, 0, 1, 1]},
                }
            ],
        },
    )
    result = render_storyboard(workspace, storyboard["id"])
    path = authorize_render_artifact(workspace, result["artifact_id"])
    assert _rms(_audio_samples(path)[3200:12800]) < 0.001
    pixel = subprocess.run(
        [
            executable("ffmpeg"),
            "-v",
            "error",
            "-ss",
            "0.4",
            "-i",
            str(path),
            "-vf",
            "scale=1:1",
            "-frames:v",
            "1",
            "-pix_fmt",
            "rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ],
        capture_output=True,
        check=True,
        timeout=30,
    ).stdout
    assert len(pixel) == 3
    assert pixel[2] > pixel[0] * 2


def test_imported_narration_words_map_around_quote_insert(case):
    from app.services.targeted_search.case_media import import_whisperx

    workspace, case, assets, incoming = case
    script_path = incoming / "Script.md"
    script_path.write_text("first last\n")
    workspace.import_folder(case["id"], incoming)
    script = next(
        asset
        for asset in workspace.list_assets(case["id"])
        if asset["filename"] == "Script.md"
    )
    workspace.search_service.set_policy(
        script["source_id"],
        "allowed_internal",
        "internal_review,analysis",
        "Owned timing fixture script",
        "test-owner",
    )
    narrator = assets["Narration.wav"]
    alignment = import_whisperx(
        workspace,
        narrator["id"],
        {
            "audio_sha256": narrator["sha256"],
            "script_sha256": script["sha256"],
            "words": [
                {"word": "first", "start": 0.2, "end": 0.6},
                {"word": "last", "start": 1.2, "end": 1.6},
            ],
        },
        scope="narration",
        script_asset_id=script["id"],
    )
    initial = _story(workspace, case, assets, narration=True)
    scenes = [
        {
            key: value
            for key, value in scene.items()
            if key
            not in {
                "asset_version_id",
                "input_sha256",
                "visual_asset_version_id",
                "visual_input_sha256",
                "visual_sha256",
            }
        }
        for scene in initial["scenes"]
    ]
    story = workspace.save_storyboard(
        case["id"],
        {
            "title": "Narration word mapping",
            "script": "first last",
            "narration_asset_id": narrator["id"],
            "metadata": {"script_asset_id": script["id"]},
            "scenes": scenes,
        },
    )
    result = render_storyboard(workspace, story["id"])
    manifest = json.loads(
        authorize_render_artifact(workspace, result["manifest_artifact_id"]).read_text()
    )
    assert manifest["narration_alignment_artifact_id"] == alignment["artifact_id"]
    assert manifest["narration_words"][1]["output_ranges"] == [
        {"start_ms": 2200, "end_ms": 2600}
    ]
    assert manifest["script_asset_sha256"] == script["sha256"]
