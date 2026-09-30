"""Owned synthetic media exercises the complete authorized clip pipeline."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import struct
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.models.search import SearchError
from app.services.targeted_search.acquisition import download_source
from app.services.targeted_search.attachments import artifact_content, attach_clip, authorize_attachments, resolve_attachments
from app.services.targeted_search.media import encode_clip, extract_clip, probe_media, run_command, sha256_file, verified_artifact_path
from app.services.targeted_search.repository import Repository, now
from app.services.targeted_search.operations import apply_retention, backup_repository, metrics, restore_backup, retention_plan, source_deletion_impact, verify_backup
from app.services.targeted_search.settings import Settings
from app.services.targeted_search.visual import scene_sample_times, visual_index, visual_search
from app.services.targeted_search.worker import process_once


pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg and ffprobe required")


@pytest.fixture(autouse=True)
def isolated_generation_storage(monkeypatch):
    monkeypatch.setattr("app.services.targeted_search.attachments._bridge_root", lambda repo: repo.root.parent / "local_videos")
    monkeypatch.setattr("app.services.targeted_search.attachments._task_root", lambda repo, task_id: repo.root.parent / "tasks" / task_id)


@pytest.fixture(scope="module")
def synthetic_sources(tmp_path_factory):
    root = tmp_path_factory.mktemp("owned-footage")
    audio = root / "owned-audio.mp4"
    silent = root / "owned-silent.mp4"
    # Long GOP and disabled scene-cut keyframes make 1275ms a non-keyframe
    # boundary. The first frame must be red, while source time zero is blue.
    run_command([
        shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=blue:s=1280x800:r=27:d=1",
        "-f", "lavfi", "-i", "color=c=red:s=1280x800:r=27:d=4",
        "-f", "lavfi", "-i", "sine=frequency=997:sample_rate=48000:duration=5",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=5",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-map", "3:a", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-threads", "2", "-g", "270", "-keyint_min", "270", "-sc_threshold", "0",
        "-c:a", "aac", "-shortest", str(audio),
    ], timeout=60)
    run_command([shutil.which("ffmpeg"), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                 "-i", str(audio), "-map", "0:v:0", "-c", "copy", "-an", str(silent)], timeout=30)
    return audio, silent


def seeded_service(tmp_path: Path, source_path: Path, use: str = "generated_export"):
    repo = Repository(tmp_path / "storage" / "targeted_search")
    settings = dataclasses.replace(Settings(), max_clip_duration_ms=90_000, visual_sample_seconds=1,
                                   max_visual_frames=8, local_models_only=True)
    source_id, candidate_id, approval_id = "src_" + uuid.uuid4().hex, "candidate_" + uuid.uuid4().hex, "approval_" + uuid.uuid4().hex
    timestamp = now()
    evidence = "Owned synthetic color footage for exact extraction"
    digest = hashlib.sha256(evidence.encode()).hexdigest()
    with repo.connect() as connection:
        connection.execute("INSERT INTO sources(id,platform,platform_video_id,canonical_url,title,creator_name,duration_ms,local_path,discovered_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (source_id, "local", source_id, source_path.as_uri(), "Owned fixture", "Test suite", 5000, str(source_path), timestamp, timestamp))
        connection.execute("INSERT INTO search_runs VALUES(?,?,?,?,?)", ("run_" + source_id, "color footage", "{}", "{}", timestamp))
        connection.execute("INSERT INTO candidates(id,search_id,source_id,start_ms,end_ms,evidence,evidence_type,evidence_hash,scores_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (candidate_id, "run_" + source_id, source_id, 1275, 3525, evidence, "manual", digest, "{}", timestamp))
        connection.execute("INSERT INTO policies VALUES(?,?,?,?,?,?,?,?,?)", ("policy_" + source_id, source_id, "allowed_export", "internal_review,analysis,generated_export,publication,clip_export", "Synthetic fixture owned by tests", "test-reviewer", None, 1, timestamp))
        connection.execute("INSERT INTO approvals(id,candidate_id,source_id,requested_use,start_ms,end_ms,reviewed_by,policy_id,policy_version,evidence_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (approval_id, candidate_id, source_id, use, 1275, 3525, "test-reviewer", "policy_" + source_id, 1, digest, timestamp))
    service = SimpleNamespace(repo=repo, settings=settings)
    payload = {"source_id": source_id, "candidate_id": candidate_id, "approval_id": approval_id,
               "requested_use": use, "start_ms": 1275, "end_ms": 3525, "clip_id": "clip_" + uuid.uuid4().hex}
    return service, payload


def test_non_keyframe_extract_preserves_fps_audio_proxy_and_manifest(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[0])
    result = extract_clip(service, payload)
    clip = service.repo.get("artifacts", result["artifact_id"])
    path = verified_artifact_path(service.repo, clip)
    probe = probe_media(path)
    assert abs(probe["duration_ms"] - 2250) <= 100
    assert abs(probe["fps_value"] - 27) < 0.1
    assert probe["has_audio"]
    assert probe["audio_stream_count"] == 2
    first_pixel = run_command([shutil.which("ffmpeg"), "-nostdin", "-loglevel", "error", "-i", str(path),
                               "-vf", "scale=1:1", "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]).stdout
    assert first_pixel[0] > 200 and first_pixel[2] < 60
    # Verify original source audio survives: decode a short mono window and
    # count positive zero crossings of the known 997Hz source tone.
    pcm = run_command([shutil.which("ffmpeg"), "-nostdin", "-loglevel", "error", "-i", str(path),
                       "-ss", "0.25", "-t", "0.25", "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"]).stdout
    samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
    crossings = sum(1 for first, second in zip(samples, samples[1:]) if first <= 0 < second)
    assert abs(crossings / (len(samples) / 16000) - 997) < 20
    proxy = service.repo.get("artifacts", result["proxy_artifact_id"])
    proxy_probe = probe_media(verified_artifact_path(service.repo, proxy))
    assert proxy_probe["height"] == 720 and proxy_probe["width"] == 1152
    assert proxy_probe["has_audio"] and abs(proxy_probe["fps_value"] - 27) < 0.1
    assert proxy_probe["audio_stream_count"] == 2
    manifest = service.repo.get("artifacts", result["manifest_artifact_id"])
    data = json.loads(verified_artifact_path(service.repo, manifest).read_text())
    assert data["input_sha256"] == service.repo.get("artifacts", clip["parent_artifact_id"])["sha256"]
    assert data["output_sha256"] == sha256_file(path) == clip["sha256"]
    assert data["source_start_ms"] == 1275 and data["source_end_ms"] == 3525
    assert data["output_fps"] == clip["metadata"]["fps"]
    assert data["approval_id"] == payload["approval_id"] and data["schema_version"] == "1.0"
    repeated = extract_clip(service, payload)
    assert repeated["artifact_id"] == result["artifact_id"]
    assert repeated["proxy_artifact_id"] == result["proxy_artifact_id"]
    assert repeated["manifest_artifact_id"] == result["manifest_artifact_id"]
    with service.repo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 4


def test_silent_source_and_out_of_range(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[1])
    result = extract_clip(service, payload)
    assert not probe_media(artifact_content(result["artifact_id"], root_dir=service.repo.root))["has_audio"]
    with pytest.raises(SearchError, match="source duration"):
        encode_clip(synthetic_sources[1], tmp_path / "bad.mp4", 0, 6000)
    with pytest.raises(SearchError, match="approved evidence window"):
        extract_clip(service, {**payload, "clip_id": "bad-range", "end_ms": 4500})


def test_worker_time_policy_and_approval_are_rechecked(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[0])
    with service.repo.connect() as connection:
        connection.execute("UPDATE policies SET expires_at=?", ("2000-01-01T00:00:00+00:00",))
    with pytest.raises(SearchError, match="expired"):
        download_source(service, payload)
    with service.repo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        connection.execute("UPDATE policies SET expires_at=NULL,rights_status='blocked'")
    with pytest.raises(SearchError, match="reviewed rights"):
        extract_clip(service, payload)
    with service.repo.connect() as connection:
        connection.execute("UPDATE policies SET rights_status='allowed_export'")
    with pytest.raises(SearchError, match="scoped download approval"):
        download_source(service, {key: value for key, value in payload.items() if key != "approval_id"})


def test_trusted_attachment_hidden_reference_rechecks_policy_and_hash(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[0])
    result = extract_clip(service, payload)
    attached = attach_clip(result["artifact_id"], root_dir=service.repo.root)
    assert Path(attached["url"]).parent == service.repo.root.parent / "local_videos"
    hidden_id_material = {"provider": "local", "url": attached["url"]}
    provenance = resolve_attachments([hidden_id_material], "owned-task", root_dir=service.repo.root)
    assert provenance[0]["clip_id"] == result["artifact_id"]
    assert hidden_id_material["artifact_id"] == result["artifact_id"]
    saved = service.repo.root.parent / "tasks" / "owned-task" / "search-materials.json"
    assert json.loads(saved.read_text())["materials"][0]["output_sha256"] == provenance[0]["output_sha256"]
    with service.repo.connect() as connection:
        connection.execute("UPDATE policies SET rights_status='allowed_internal'")
    with pytest.raises(SearchError, match="internal use only"):
        resolve_attachments([{"url": attached["url"]}], "later-task", root_dir=service.repo.root)
    with service.repo.connect() as connection:
        connection.execute("UPDATE policies SET rights_status='allowed_export'")
    Path(attached["url"]).write_bytes(b"tampered copy")
    with pytest.raises(SearchError, match="digest verification"):
        resolve_attachments([{"url": attached["url"]}], "tampered-task", root_dir=service.repo.root)


def test_internal_review_clip_cannot_be_attached(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[1], use="internal_review")
    with service.repo.connect() as connection:
        connection.execute("UPDATE policies SET rights_status='allowed_internal',permitted_use='internal_review,analysis'")
    result = extract_clip(service, payload)
    assert artifact_content(result["artifact_id"], root_dir=service.repo.root).is_file()
    with pytest.raises(SearchError, match="internal use only"):
        attach_clip(result["artifact_id"], root_dir=service.repo.root)


def test_artifact_content_rejects_corruption_and_path_escape(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[1])
    result = extract_clip(service, payload)
    artifact = service.repo.get("artifacts", result["artifact_id"])
    with service.repo.connect() as connection:
        connection.execute("UPDATE artifacts SET path=? WHERE id=?", (str(synthetic_sources[1]), artifact["id"]))
    with pytest.raises(SearchError, match="outside immutable storage"):
        artifact_content(artifact["id"], root_dir=service.repo.root)
    with service.repo.connect() as connection:
        connection.execute("UPDATE artifacts SET path=? WHERE id=?", (artifact["path"], artifact["id"]))
    verified_artifact_path(service.repo, artifact).write_bytes(b"tampered master")
    with pytest.raises(SearchError, match="digest verification"):
        artifact_content(artifact["id"], root_dir=service.repo.root)


def test_scene_sampling_and_visual_index_are_bounded_idempotent(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[0])
    samples = scene_sample_times(synthetic_sources[0], 5000, interval_seconds=1, maximum=3)
    assert len(samples) <= 3 and all(0 <= item[0] < 5000 for item in samples)
    indexed = visual_index(service, {**payload, "ocr": False, "embeddings": False})
    assert indexed["frames"] >= 2 and indexed["frames"] <= 8
    repeated = visual_index(service, {**payload, "ocr": False, "embeddings": False})
    assert repeated["frames"] == indexed["frames"]
    with service.repo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM visual_frames").fetchone()[0] == indexed["frames"]
        assert connection.execute("SELECT count(*) FROM ocr_blocks").fetchone()[0] == 0
    assert indexed["vectors"] == 0 and not indexed["visual_embeddings_enabled"]


def test_worker_renews_short_lease_and_missing_dependencies_fail_explicitly(tmp_path, synthetic_sources, monkeypatch):
    service, payload = seeded_service(tmp_path, synthetic_sources[0])
    service.repo.settings = dataclasses.replace(service.repo.settings, job_lease_seconds=1)
    queued = service.repo.enqueue("embedding", {"source_id": payload["source_id"]}, "lease-renewal")

    def slow_job(*_):
        time.sleep(1.4)
        return {"count": 1}

    monkeypatch.setattr("app.services.targeted_search.worker._dispatch", slow_job)
    completed = process_once(service, worker_id="test-lease-worker")
    assert completed["id"] == queued["id"] and completed["status"] == "complete"
    assert completed["attempts"] == 1
    monkeypatch.setattr("app.services.targeted_search.worker._dispatch", lambda *_: (_ for _ in ()).throw(SearchError("Optional embedding model missing", 503)))
    failed_job = service.repo.enqueue("embedding", {"source_id": payload["source_id"]}, "missing-model")
    failed = process_once(service, worker_id="test-missing-model")
    assert failed["id"] == failed_job["id"] and failed["status"] == "failed"
    assert "model missing" in failed["last_error"]
    assert failed["attempts"] == 1


def test_renamed_attachment_and_readonly_delivery_gate(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[0])
    result = extract_clip(service, payload)
    attached = attach_clip(result["artifact_id"], root_dir=service.repo.root)
    saved = resolve_attachments([attached], "readonly-task", root_dir=service.repo.root)
    sidecar = service.repo.root.parent / "tasks" / "readonly-task" / "search-materials.json"
    original = sidecar.read_bytes()
    assert authorize_attachments(saved, requested_use="publication", root_dir=service.repo.root)[0]["clip_id"] == result["artifact_id"]
    assert sidecar.read_bytes() == original
    renamed = Path(attached["url"]).parent / "ordinary-upload.mp4"
    shutil.copyfile(attached["url"], renamed)
    with service.repo.connect() as connection:
        connection.execute("UPDATE policies SET rights_status='allowed_internal'")
    with pytest.raises(SearchError, match="internal use only"):
        resolve_attachments([{"url": str(renamed)}], "rename-bypass", root_dir=service.repo.root)


def test_backup_restore_metrics_and_checksums(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[1])
    result = extract_clip(service, payload)
    report = metrics(service.repo.root)
    assert report["storage"]["artifact_records"] == 4
    assert report["storage"]["artifact_bytes"] > 0 and report["storage"]["missing_files"] == 0
    backup = backup_repository(service.repo.root, tmp_path / "search-backup")
    assert verify_backup(backup["backup_dir"])["valid"]
    restored = restore_backup(backup["backup_dir"], tmp_path / "restored" / "targeted_search")
    restored_path = artifact_content(result["artifact_id"], root_dir=restored["root_dir"])
    assert restored_path.is_relative_to(Path(restored["root_dir"]))
    assert sha256_file(restored_path) == service.repo.get("artifacts", result["artifact_id"])["sha256"]
    with pytest.raises(SearchError, match="already exists"):
        backup_repository(service.repo.root, backup["backup_dir"])
    with pytest.raises(SearchError, match="new storage root"):
        restore_backup(backup["backup_dir"], restored["root_dir"])
    manifest = json.loads((Path(backup["backup_dir"]) / "backup.json").read_text())
    file = next(name for name in manifest["files"] if name.endswith(".mp4"))
    (Path(backup["backup_dir"]) / file).write_bytes(b"corrupted backup")
    with pytest.raises(SearchError, match="checksum verification"):
        verify_backup(backup["backup_dir"])


def test_retention_rechecks_active_jobs_and_preserves_attached_clips(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[0])
    result = extract_clip(service, payload)
    visual_index(service, {**payload, "ocr": False, "embeddings": False})
    attached = attach_clip(result["artifact_id"], root_dir=service.repo.root)
    resolve_attachments([attached], "retain-task", root_dir=service.repo.root)
    with service.repo.connect() as connection:
        connection.execute("UPDATE artifacts SET created_at='2000-01-01T00:00:00+00:00'")
    stale = service.repo.root / "staging" / "abandoned.partial"
    stale.write_bytes(b"old incomplete media")
    old_time = time.time() - 40 * 86400
    os.utime(stale, (old_time, old_time))
    plan = retention_plan(service.repo.root)
    assert plan["artifacts"] and all(row["kind"] == "keyframe" for row in plan["artifacts"])
    assert plan["staging"][0]["path"] == str(stale)
    pending = service.repo.enqueue("visual_index", payload, "protect-live-job")
    blocked_cleanup = apply_retention(plan, service.repo.root)
    assert not blocked_cleanup["removed_artifact_ids"] and stale.is_file()
    with service.repo.connect() as connection:
        connection.execute("UPDATE jobs SET status='failed' WHERE id=?", (pending["id"],))
    cleaned = apply_retention(retention_plan(service.repo.root), service.repo.root)
    assert cleaned["removed_artifact_ids"] and not stale.exists()
    assert artifact_content(result["artifact_id"], root_dir=service.repo.root).is_file()
    assert artifact_content(result["proxy_artifact_id"], root_dir=service.repo.root).is_file()
    impact = source_deletion_impact(payload["source_id"], service.repo.root)
    assert impact["affected_task_ids"] == ["retain-task"] and not impact["deletion_performed"]
    assert len(impact["artifacts"]) == 4


def test_storage_quota_is_enforced_before_artifact_registration(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[1])
    service.repo.settings = dataclasses.replace(service.repo.settings, max_storage_bytes=1)
    with pytest.raises(SearchError, match="storage budget"):
        download_source(service, payload)
    with service.repo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_caption_only_search_does_not_load_vision_model(tmp_path, synthetic_sources, monkeypatch):
    service, _ = seeded_service(tmp_path, synthetic_sources[1])
    monkeypatch.setattr("app.services.targeted_search.visual._vision_model", lambda *_: (_ for _ in ()).throw(AssertionError("Vision model must not load without visual vectors")))
    assert visual_search(service, "red diagram") == []
    assert visual_search(service, "red diagram", source_ids=set()) == []


def test_manifest_registration_crash_is_reconciled_without_duplicate_artifacts(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[1])
    result = extract_clip(service, payload)
    # Simulate the final transaction being lost after immutable derivatives
    # were registered. All artifacts should be reused by their stable IDs.
    with service.repo.connect() as connection:
        connection.execute("DELETE FROM clip_provenance WHERE clip_id=?", (result["clip_id"],))
        row = connection.execute("SELECT metadata_json FROM artifacts WHERE id=?", (result["clip_id"],)).fetchone()
        metadata = json.loads(row[0])
        metadata.pop("manifest_artifact_id")
        connection.execute("UPDATE artifacts SET metadata_json=? WHERE id=?", (json.dumps(metadata), result["clip_id"]))
    recovered = extract_clip(service, payload)
    assert recovered["manifest_artifact_id"] == result["manifest_artifact_id"]
    with service.repo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 4
        assert connection.execute("SELECT count(*) FROM clip_provenance").fetchone()[0] == 1
    with pytest.raises(SearchError, match="identity does not match"):
        extract_clip(service, {**payload, "start_ms": 1400})


def test_task_delivery_uses_db_refs_after_sidecar_reference_or_file_is_lost(tmp_path, synthetic_sources, monkeypatch):
    from app.services.search_bridge import authorize_task_sources

    first, first_payload = seeded_service(tmp_path, synthetic_sources[0])
    first_clip = extract_clip(first, first_payload)
    first_material = attach_clip(first_clip["artifact_id"], root_dir=first.repo.root)
    second, second_payload = seeded_service(tmp_path, synthetic_sources[1])
    second_clip = extract_clip(second, second_payload)
    second_material = attach_clip(second_clip["artifact_id"], root_dir=second.repo.root)
    resolve_attachments([first_material, second_material], "canonical-db-task", root_dir=first.repo.root)
    tasks = first.repo.root.parent / "tasks"
    monkeypatch.setattr("app.services.targeted_search.settings.storage_root", lambda root_dir=None: Path(root_dir).resolve() if root_dir else first.repo.root)
    monkeypatch.setattr("app.services.targeted_search.repository.storage_root", lambda root_dir=None: Path(root_dir).resolve() if root_dir else first.repo.root)
    monkeypatch.setattr("app.services.search_bridge.utils.task_dir", lambda sub_dir="": str(tasks / sub_dir))
    authorize_task_sources("canonical-db-task")
    with first.repo.connect() as connection:
        connection.execute("UPDATE policies SET rights_status='allowed_internal' WHERE source_id=?", (second_payload["source_id"],))
    manifest = tasks / "canonical-db-task" / "search-materials.json"
    saved = json.loads(manifest.read_text())
    saved["materials"] = [saved["materials"][0]]
    manifest.write_text(json.dumps(saved))
    with pytest.raises(SearchError, match="internal use only"):
        authorize_task_sources("canonical-db-task")
    manifest.unlink()
    with pytest.raises(SearchError, match="internal use only"):
        authorize_task_sources("canonical-db-task")


def test_local_metadata_cannot_supply_an_unvalidated_media_path(tmp_path, synthetic_sources):
    service, payload = seeded_service(tmp_path, synthetic_sources[0])
    with service.repo.connect() as connection:
        connection.execute("UPDATE sources SET local_path=NULL,metadata_json=? WHERE id=?", (json.dumps({"local_path": str(synthetic_sources[0])}), payload["source_id"]))
    with pytest.raises(SearchError, match="has not been imported"):
        download_source(service, payload)
    with service.repo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_restore_uses_preservation_master_when_pinned_owned_input_is_lost(tmp_path, synthetic_sources):
    owned = tmp_path / "owned-before-storage-loss.mp4"
    shutil.copyfile(synthetic_sources[1], owned)
    service, payload = seeded_service(tmp_path, owned)
    expected_digest = sha256_file(owned)
    with service.repo.connect() as connection:
        connection.execute("UPDATE approvals SET source_file_sha256=? WHERE id=?", (expected_digest, payload["approval_id"]))
    result = extract_clip(service, payload)
    backup = backup_repository(service.repo.root, tmp_path / "owned-source-backup")
    owned.unlink()
    with pytest.raises(SearchError, match="no longer available"):
        artifact_content(result["artifact_id"], root_dir=service.repo.root)
    restored = restore_backup(backup["backup_dir"], tmp_path / "recovered-search")
    assert restored["missing_owned_source_ids"] == []
    restored_repo = Repository(restored["root_dir"])
    source = restored_repo.get("sources", payload["source_id"])
    assert Path(source["local_path"]).is_relative_to(restored_repo.root)
    assert sha256_file(Path(source["local_path"])) == expected_digest
    assert restored_repo.get("approvals", payload["approval_id"])["source_file_sha256"] == expected_digest
    restored_clip = artifact_content(result["artifact_id"], root_dir=restored_repo.root)
    assert restored_clip.is_file() and not probe_media(restored_clip)["has_audio"]
