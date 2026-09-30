"""Imported case footage stays portable and hash checked through legacy acquisition."""

from dataclasses import replace
import shutil

import pytest

from app.models.search import SearchError
from app.services.targeted_search import acquisition
from app.services.targeted_search.case_workspace import CaseWorkspace
from app.services.targeted_search.media import (
    executable,
    probe_media,
    run_command,
    verified_artifact_path,
)
from app.services.targeted_search.service import SearchService


pytestmark = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Real retained-footage acquisition checks require FFmpeg and ffprobe",
)


@pytest.fixture
def imported_video(tmp_path, monkeypatch):
    service = SearchService(tmp_path / "library")
    service.settings = replace(
        service.settings,
        enabled=True,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
    )
    service.repo.settings = service.settings
    workspace = CaseWorkspace(service)
    case = workspace.create_case("Portable owned footage")
    incoming = service.repo.root / "owned" / "incoming"
    incoming.mkdir(parents=True)
    original = incoming / "Courthouse_Exterior.mp4"
    run_command(
        [
            executable("ffmpeg"),
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x240:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=997:sample_rate=48000:duration=2",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-preset",
            "ultrafast",
            "-threads",
            "1",
            "-c:a",
            "aac",
            "-shortest",
            str(original),
        ],
        timeout=30,
    )
    expected_bytes = original.read_bytes()
    asset = workspace.import_folder(case["id"], incoming)["assets"][0]
    retained = workspace.asset_path(asset["id"])
    original.unlink()  # The user-selected source folder is no longer required.
    old_host_path = tmp_path / "missing-old-host" / "owned" / "original.mp4"
    with service.repo.connect() as connection:
        connection.execute(
            "UPDATE sources SET local_path=? WHERE id=?",
            (str(old_host_path), asset["source_id"]),
        )
    assert not old_host_path.exists()
    service.set_policy(
        asset["source_id"],
        "allowed_internal",
        "internal_review,analysis",
        "Owned synthetic footage explicitly reviewed",
        "test-owner",
    )
    candidate = service.search(
        "Courthouse Exterior", {"source_ids": [asset["source_id"]]}
    )["results"][0]

    def no_external_acquisition(*args, **kwargs):
        pytest.fail("Retained case originals must not launch a network acquisition")

    monkeypatch.setattr(acquisition, "run_command", no_external_acquisition)
    return service, workspace, asset, candidate, retained, expected_bytes


def test_missing_host_path_approval_and_acquisition_reuse_verified_case_original(
    imported_video,
):
    service, workspace, asset, candidate, retained, expected_bytes = imported_video
    approval = service.approve_download(candidate["id"], reviewed_by="test-owner")
    assert approval["source_file_sha256"] == asset["sha256"]
    result = acquisition.download_source(
        service, service.repo.get("jobs", approval["job_id"])["payload"]
    )
    source_artifact = service.repo.get("artifacts", result["artifact_id"])
    assert result["reused"] is True
    assert source_artifact["kind"] == "source"
    assert source_artifact["parent_artifact_id"] == asset["artifact_id"]
    assert source_artifact["approval_id"] == approval["approval_id"]
    assert source_artifact["metadata"]["adapter"] == "case-original"
    assert source_artifact["metadata"]["has_audio"] is True
    assert source_artifact["metadata"]["duration_ms"] == 2000
    assert source_artifact["sha256"] == asset["sha256"]
    path = verified_artifact_path(service.repo, source_artifact)
    assert path == retained == workspace.asset_path(asset["id"])
    assert path.read_bytes() == expected_bytes
    assert probe_media(path)["has_audio"] is True
    assert service.get_source(asset["source_id"])["duration_ms"] == 2000
    repeated = acquisition.download_source(
        service, service.repo.get("jobs", approval["job_id"])["payload"]
    )
    assert repeated["artifact_id"] == source_artifact["id"]
    with service.repo.connect() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE source_id=? AND kind='source'",
                (asset["source_id"],),
            ).fetchone()[0]
            == 1
        )


@pytest.mark.parametrize("acquire_first", [False, True])
def test_tampered_case_original_blocks_acquisition_and_new_approval(
    imported_video, acquire_first
):
    service, _, asset, candidate, retained, _ = imported_video
    approval = service.approve_download(candidate["id"], reviewed_by="test-owner")
    if acquire_first:
        acquisition.download_source(
            service, service.repo.get("jobs", approval["job_id"])["payload"]
        )
    retained.write_bytes(b"tampered retained original")
    with pytest.raises(SearchError, match="digest verification"):
        acquisition.download_source(
            service, service.repo.get("jobs", approval["job_id"])["payload"]
        )
    with pytest.raises(SearchError, match="digest verification"):
        service.approve_download(candidate["id"], reviewed_by="test-owner")
    with service.repo.connect() as connection:
        assert connection.execute(
            "SELECT count(*) FROM artifacts WHERE source_id=? AND kind='source'",
            (asset["source_id"],),
        ).fetchone()[0] == int(acquire_first)
