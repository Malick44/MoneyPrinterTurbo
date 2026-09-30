"""Local search metrics, recoverable snapshots, and conservative retention.

Retention has an explicit plan/apply boundary. Sources, exact clips, manifests,
task attachments, and derivatives needed by active jobs are never removed.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models.search import SearchError

from .media import sha256_file, verified_artifact_path
from .repository import Repository, decode, now


def _stamp(value: str | None) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None
        return parsed if parsed and parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def metrics(root_dir=None) -> dict:
    repo = Repository(root_dir)
    with repo.connect() as connection:
        jobs = [dict(row) for row in connection.execute("SELECT id,status,created_at,updated_at,attempts,last_error FROM jobs")]
        artifacts = [dict(row) for row in connection.execute("SELECT path,bytes,kind FROM artifacts")]
        counts = {table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("sources", "captions", "transcript_chunks", "embeddings", "visual_frames", "ocr_blocks", "attachments")}
        events = [dict(row) for row in connection.execute("SELECT job_id,event_type,created_at FROM events WHERE event_type IN ('job_started','job_complete') ORDER BY created_at")]
    states, starts, durations, latencies = {}, {}, [], []
    created = {row["id"]: _stamp(row["created_at"]) for row in jobs}
    for job in jobs:
        states[job["status"]] = states.get(job["status"], 0) + 1
    for event in events:
        stamp = _stamp(event["created_at"])
        if not stamp:
            continue
        if event["event_type"] == "job_started":
            starts[event["job_id"]] = stamp
            if created.get(event["job_id"]):
                latencies.append(max(0, (stamp - created[event["job_id"]]).total_seconds()))
        elif event["job_id"] in starts:
            durations.append(max(0, (stamp - starts[event["job_id"]]).total_seconds()))
    unique_paths = {row["path"]: row["bytes"] for row in artifacts}
    staging = repo.root / "staging"
    staged = [file for file in staging.rglob("*") if file.is_file() and not file.is_symlink()] if staging.exists() else []
    failures = [{"job_id": row["id"], "error": row["last_error"], "attempts": row["attempts"]} for row in sorted(jobs, key=lambda item: item["updated_at"], reverse=True) if row["status"] == "failed"][:10]
    ordered = sorted(durations)
    return {"schema_version": "1.0", "generated_at": now(), "records": counts,
            "jobs": {"states": states, "attempts": sum(row["attempts"] for row in jobs), "recent_failures": failures},
            "storage": {"artifact_records": len(artifacts), "unique_files": len(unique_paths),
                        "artifact_bytes": sum(unique_paths.values()), "staging_bytes": sum(file.stat().st_size for file in staged),
                        "missing_files": sum(not (repo.root / path).is_file() for path in unique_paths), "budget_bytes": repo.settings.max_storage_bytes},
            "processing": {"completed_samples": len(durations), "average_seconds": sum(durations) / len(durations) if durations else None,
                           "p95_seconds": ordered[min(len(ordered) - 1, int(len(ordered) * .95))] if ordered else None,
                           "average_queue_latency_seconds": sum(latencies) / len(latencies) if latencies else None}}


def backup_repository(root_dir=None, destination=None) -> dict:
    """Snapshot SQLite first, then copy precisely its immutable artifact set."""
    repo = Repository(root_dir)
    destination = Path(destination).expanduser().resolve() if destination else repo.root.parent / "targeted_search_backups" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    if destination == repo.root or destination.is_relative_to(repo.root):
        raise SearchError("A search backup must be outside its source storage root", status_code=422)
    if destination.exists():
        raise SearchError("Backup destination already exists", status_code=409)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".search-backup-", dir=destination.parent))
    try:
        database = stage / "search.sqlite3"
        with repo.connect() as source_connection, sqlite3.connect(database) as backup_connection:
            source_connection.backup(backup_connection, pages=128, sleep=.01)
        with sqlite3.connect(database) as connection:
            connection.row_factory = sqlite3.Row
            rows = [dict(row) for row in connection.execute("SELECT * FROM artifacts")]
        copied = set()
        for row in rows:
            original = verified_artifact_path(repo, row)
            relative = original.relative_to(repo.root)
            if str(relative) in copied:
                continue
            copied.add(str(relative))
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            if sha256_file(target) != row["sha256"]:
                raise SearchError("Backup artifact failed digest verification", status_code=409)
        files = sorted(file for file in stage.rglob("*") if file.is_file())
        checksums = {str(file.relative_to(stage)): {"sha256": sha256_file(file), "bytes": file.stat().st_size} for file in files}
        manifest = {"schema_version": "1.0", "created_at": now(), "source_root": str(repo.root),
                    "artifact_records": len(rows), "files": checksums,
                    "scope": "Search database, rights, approvals, manifests, and canonical media artifacts; generated task outputs remain in task storage."}
        (stage / "backup.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(stage, destination)
        return {"backup_dir": str(destination), "artifact_records": len(rows), "files": len(files),
                "bytes": sum(item["bytes"] for item in checksums.values()), "manifest_sha256": sha256_file(destination / "backup.json")}
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def verify_backup(backup_dir) -> dict:
    root = Path(backup_dir).expanduser().resolve()
    try:
        manifest = json.loads((root / "backup.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SearchError("Backup manifest is missing or invalid", status_code=409) from exc
    if manifest.get("schema_version") != "1.0" or "search.sqlite3" not in manifest.get("files", {}):
        raise SearchError("Unsupported or incomplete backup manifest", status_code=409)
    for relative, expected in manifest["files"].items():
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file() or path.stat().st_size != expected["bytes"] or sha256_file(path) != expected["sha256"]:
            raise SearchError("Backup checksum verification failed", status_code=409)
    with sqlite3.connect(root / "search.sqlite3") as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SearchError("Backup database integrity verification failed", status_code=409)
    return {"valid": True, "files": len(manifest["files"]), "artifact_records": manifest["artifact_records"], "source_root": manifest["source_root"]}


def restore_backup(backup_dir, root_dir) -> dict:
    """Restore into a new root only; never replace an existing search database."""
    verify_backup(backup_dir)
    backup = Path(backup_dir).expanduser().resolve()
    target = Path(root_dir).expanduser().resolve()
    if target.exists():
        raise SearchError("Restore destination must be a new storage root", status_code=409)
    if target.is_relative_to(backup) or backup.is_relative_to(target):
        raise SearchError("Restore and backup directories must be separate", status_code=422)
    manifest = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
    old_root = Path(manifest["source_root"])
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".search-restore-", dir=target.parent))
    missing_owned_sources = []
    try:
        for relative in manifest["files"]:
            source = backup / relative
            output = stage / relative
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, output)
        with sqlite3.connect(stage / "search.sqlite3") as connection:
            for table in ("artifacts", "visual_frames"):
                for identifier, path in connection.execute(f"SELECT id,path FROM {table}").fetchall():
                    original = Path(path)
                    relative = original.relative_to(old_root) if original.is_absolute() else original
                    if relative.is_absolute() or ".." in relative.parts:
                        raise SearchError("Backup contains an unsafe artifact path", status_code=409)
                    connection.execute(f"UPDATE {table} SET path=? WHERE id=?", (str(relative), identifier))
            # Owned approvals pin source bytes. Restore the authoritative read
            # path from the backed-up preservation master, never from a stale
            # host pathname or a different source/content version. No approval
            # hashes, rights decisions, or scopes are reinterpreted here.
            for source_id, in connection.execute("SELECT id FROM sources WHERE platform='local'").fetchall():
                expected = connection.execute("SELECT source_file_sha256 FROM approvals WHERE source_id=? AND source_file_sha256 IS NOT NULL ORDER BY created_at DESC LIMIT 1", (source_id,)).fetchone()
                if expected:
                    master = connection.execute("SELECT path FROM artifacts WHERE source_id=? AND kind IN ('source','case_original') AND sha256=? ORDER BY created_at DESC LIMIT 1", (source_id, expected[0])).fetchone()
                else:
                    master = connection.execute("SELECT path FROM artifacts WHERE source_id=? AND kind IN ('source','case_original') ORDER BY created_at DESC LIMIT 1", (source_id,)).fetchone()
                if master:
                    connection.execute("UPDATE sources SET local_path=? WHERE id=?", (str(target / master[0]), source_id))
                else:
                    missing_owned_sources.append(source_id)
            # A crashed worker in the snapshot cannot retain ownership here.
            connection.execute("UPDATE jobs SET status='retry',locked_by=NULL,locked_at=NULL,lease_until=NULL,available_at=? WHERE status='running'", (now(),))
        os.replace(stage, target)
        return {"root_dir": str(target), "restored_files": len(manifest["files"]), "artifact_records": manifest["artifact_records"], "missing_owned_source_ids": missing_owned_sources}
    except BaseException:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _eligible(repo, older_than_days: int) -> tuple[list[dict], list[dict]]:
    if isinstance(older_than_days, bool) or not isinstance(older_than_days, int) or older_than_days < 1:
        raise SearchError("Retention age must be at least one whole day", status_code=422)
    threshold = datetime.now(timezone.utc) - timedelta(days=older_than_days)
    with repo.connect() as connection:
        artifacts = [decode(row) for row in connection.execute("SELECT * FROM artifacts")]
        attached = {row[0] for row in connection.execute("SELECT DISTINCT artifact_id FROM attachments")}
        active = [json.loads(row[0]) for row in connection.execute("SELECT payload_json FROM jobs WHERE status IN ('queued','retry','running')")]
        frames = [dict(row) for row in connection.execute("SELECT * FROM visual_frames")]
        evidence_ids = set()
        for row in connection.execute("SELECT evidence_ids_json FROM candidates"):
            evidence_ids.update(json.loads(row[0]))
    active_sources = {row.get("source_id") for row in active}
    referenced = set(attached)
    for artifact in artifacts:
        metadata = artifact.get("metadata", {})
        referenced.update(value for key, value in metadata.items() if key in {"proxy_artifact_id", "manifest_artifact_id"} and value)
    frame_map = {row["artifact_id"]: row for row in frames}
    selected = []
    for artifact in artifacts:
        created = _stamp(artifact["created_at"])
        if artifact["kind"] not in {"review_proxy", "keyframe"} or not created or created >= threshold:
            continue
        if artifact["source_id"] in active_sources or artifact["id"] in referenced or artifact["id"] in evidence_ids or artifact.get("parent_artifact_id") in attached:
            continue
        frame = frame_map.get(artifact["id"])
        if frame and frame["id"] in evidence_ids:
            continue
        selected.append({"artifact_id": artifact["id"], "source_id": artifact["source_id"], "kind": artifact["kind"], "path": artifact["path"], "sha256": artifact["sha256"], "bytes": artifact["bytes"], "frame_id": frame["id"] if frame else None})
    staged = []
    staging = repo.root / "staging"
    if not active and staging.exists():
        for path in staging.rglob("*"):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(staging.resolve()):
                continue
            info = path.stat()
            if datetime.fromtimestamp(info.st_mtime, timezone.utc) < threshold:
                staged.append({"path": str(path), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns})
    return selected, staged


def retention_plan(root_dir=None, older_than_days: int = 30) -> dict:
    repo = Repository(root_dir)
    artifacts, staging = _eligible(repo, older_than_days)
    return {"schema_version": "1.0", "root_dir": str(repo.root), "generated_at": now(), "older_than_days": older_than_days,
            "artifacts": artifacts, "staging": staging, "candidate_bytes": sum(row["bytes"] for row in artifacts + staging),
            "protected": ["sources", "exact clips", "manifests", "attachments", "active jobs", "candidate visual evidence"]}


def apply_retention(plan: dict, root_dir=None) -> dict:
    repo = Repository(root_dir or plan.get("root_dir"))
    if plan.get("schema_version") != "1.0" or str(repo.root) != plan.get("root_dir"):
        raise SearchError("Retention plan does not match this storage root", status_code=409)
    current, staging = _eligible(repo, plan["older_than_days"])
    current_ids = {row["artifact_id"]: row for row in current}
    targets = [row for row in plan.get("artifacts", []) if current_ids.get(row.get("artifact_id")) == row]
    removed, freed = [], 0
    for record in targets:
        artifact = repo.get("artifacts", record["artifact_id"])
        path = verified_artifact_path(repo, artifact)
        with repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Recheck new task references and claims under the deletion write lock.
            if connection.execute("SELECT 1 FROM attachments WHERE artifact_id IN (?,?) LIMIT 1", (artifact["id"], artifact.get("parent_artifact_id"))).fetchone():
                continue
            if any(json.loads(row[0]).get("source_id") == artifact["source_id"] for row in connection.execute("SELECT payload_json FROM jobs WHERE status IN ('queued','retry','running')")):
                continue
            if any(artifact["id"] in {json.loads(row[0]).get("proxy_artifact_id"), json.loads(row[0]).get("manifest_artifact_id")} for row in connection.execute("SELECT metadata_json FROM artifacts")):
                continue
            if connection.execute("SELECT 1 FROM artifacts WHERE parent_artifact_id=? LIMIT 1", (artifact["id"],)).fetchone():
                continue
            if record.get("frame_id"):
                frame_id = record["frame_id"]
                if any(frame_id in json.loads(row[0]) for row in connection.execute("SELECT evidence_ids_json FROM candidates")):
                    continue
                connection.execute("DELETE FROM embeddings WHERE entity_type='visual_frame' AND entity_id=?", (frame_id,))
                connection.execute("DELETE FROM ocr_blocks WHERE frame_id=?", (frame_id,))
                connection.execute("DELETE FROM visual_frames WHERE id=?", (frame_id,))
            connection.execute("DELETE FROM artifacts WHERE id=?", (artifact["id"],))
            shared = connection.execute("SELECT 1 FROM artifacts WHERE path=? LIMIT 1", (artifact["path"],)).fetchone()
        if not shared:
            path.unlink(missing_ok=True)
            freed += artifact["bytes"]
        removed.append(artifact["id"])
    eligible_stage = {row["path"]: row for row in staging}
    staged_removed = []
    for record in plan.get("staging", []):
        if eligible_stage.get(record.get("path")) != record:
            continue
        path = Path(record["path"])
        with repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute("SELECT 1 FROM jobs WHERE status IN ('queued','retry','running') LIMIT 1").fetchone():
                continue
            if path.is_file() and not path.is_symlink() and path.stat().st_mtime_ns == record["mtime_ns"] and path.stat().st_size == record["bytes"]:
                path.unlink()
                freed += record["bytes"]
                staged_removed.append(str(path))
    repo.event("retention_applied", payload={"artifact_ids": removed, "staging_files": staged_removed, "freed_bytes": freed})
    return {"removed_artifact_ids": removed, "removed_staging_files": staged_removed, "freed_bytes": freed}


def source_deletion_impact(source_id: str, root_dir=None) -> dict:
    repo = Repository(root_dir)
    source = repo.get("sources", source_id)
    if not source:
        raise SearchError("Source was not found", status_code=404)
    with repo.connect() as connection:
        artifacts = [dict(row) for row in connection.execute("SELECT id,kind,parent_artifact_id,bytes FROM artifacts WHERE source_id=?", (source_id,))]
        attachments = [dict(row) for row in connection.execute("SELECT a.* FROM attachments a JOIN artifacts f ON f.id=a.artifact_id WHERE f.source_id=?", (source_id,))]
        counts = {table: connection.execute(f"SELECT count(*) FROM {table} WHERE source_id=?", (source_id,)).fetchone()[0] for table in ("captions", "caption_cues", "transcript_chunks", "embeddings", "visual_frames", "ocr_blocks", "candidates", "policies", "approvals", "clip_provenance")}
        jobs = [row["id"] for row in connection.execute("SELECT id,payload_json FROM jobs WHERE status IN ('queued','retry','running')") if json.loads(row["payload_json"]).get("source_id") == source_id]
    return {"source_id": source_id, "canonical_url": source["canonical_url"], "records": counts, "artifacts": artifacts,
            "attachments": attachments, "affected_task_ids": sorted({row["task_id"] for row in attachments if row["task_id"]}),
            "active_job_ids": jobs, "requires_task_review": bool(attachments), "deletion_performed": False}
