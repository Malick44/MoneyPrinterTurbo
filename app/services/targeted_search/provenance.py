"""Versioned portable clip manifests, backed by operational database records."""

from __future__ import annotations

import json
import hashlib
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.models.search import SearchError

from .media import PIPELINE_VERSION, _staging, ffmpeg_version, promote_artifact, verified_artifact_path
from .policy import authorize

MANIFEST_SCHEMA_VERSION = "1.0"


def create_manifest(service, clip: dict, payload: dict, proxy: dict) -> dict:
    repo = service.repo
    source = repo.get("sources", clip["source_id"])
    parent = repo.get("artifacts", clip["parent_artifact_id"])
    approval_id = payload.get("approval_id") or clip.get("approval_id")
    if not approval_id:
        raise SearchError("Clip provenance requires a scoped approval", status_code=403)
    requested_use = payload.get("requested_use", "internal_review")
    policy = authorize(repo, clip["source_id"], requested_use, approval_id, clip["start_ms"], clip["end_ms"])
    with repo.connect() as connection:
        existing = connection.execute("SELECT manifest_artifact_id FROM clip_provenance WHERE clip_id=? ORDER BY created_at DESC LIMIT 1", (clip["id"],)).fetchone()
    if existing and existing["manifest_artifact_id"]:
        manifest = repo.get("artifacts", existing["manifest_artifact_id"])
        if manifest:
            verified_artifact_path(repo, manifest)
            return manifest
    candidate_id = payload.get("candidate_id") or clip.get("metadata", {}).get("candidate_id")
    candidate = repo.get("candidates", candidate_id) if candidate_id else None
    search_run = repo.get("search_runs", candidate["search_id"]) if candidate else None
    approval = repo.get("approvals", approval_id)
    metadata = clip.get("metadata", {})
    manifest_data = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "clip_id": clip["id"], "parent_source_id": clip["source_id"],
        "canonical_source_url": source["canonical_url"], "source_title": source["title"],
        "source_creator": source["creator_name"], "source_published_at": source.get("published_at"),
        "retrieved_at": parent["created_at"], "license_claim": source.get("metadata", {}).get("license"),
        "rights_status": policy["rights_status"], "requested_use": requested_use,
        "policy_id": policy["id"], "policy_version": policy["version"],
        "approval_id": approval_id, "approval_expires_at": approval.get("expires_at"),
        "source_start_ms": clip["start_ms"], "source_end_ms": clip["end_ms"],
        "output_duration_ms": metadata.get("duration_ms"),
        "transcript_excerpt": candidate.get("evidence", "") if candidate else "",
        "retrieval_query": search_run.get("query", "") if search_run else "",
        "retrieval_scores": candidate.get("scores", {}) if candidate else {},
        "pipeline": {"version": PIPELINE_VERSION, "ffmpeg_version": ffmpeg_version()},
        "input_sha256": parent["sha256"], "output_sha256": clip["sha256"],
        "proxy_sha256": proxy["sha256"], "proxy_artifact_id": proxy["id"],
        "artifact_profile": clip["profile"], "source_fps": parent.get("metadata", {}).get("fps"),
        "output_fps": metadata.get("fps"), "has_audio": metadata.get("has_audio"),
        "attribution_text": source.get("metadata", {}).get("attribution_text", ""),
        "created_at": clip["created_at"],
    }
    manifest_id = "manifest_" + hashlib.sha256((clip["id"] + "|manifest|" + MANIFEST_SCHEMA_VERSION).encode()).hexdigest()[:32]
    manifest = repo.get("artifacts", manifest_id)
    if manifest:
        # Reconcile a crash after manifest promotion/DB registration but before
        # the clip_provenance transaction, without creating another artifact.
        prior = json.loads(verified_artifact_path(repo, manifest).read_text(encoding="utf-8"))
        keys = ("clip_id", "parent_source_id", "input_sha256", "output_sha256", "source_start_ms", "source_end_ms", "approval_id")
        if any(prior.get(key) != manifest_data[key] for key in keys):
            raise SearchError("Existing manifest identity failed reconciliation", status_code=409)
        manifest_data = prior
    else:
        with tempfile.TemporaryDirectory(prefix="manifest-", dir=_staging(repo)) as directory:
            staged = Path(directory) / "clip.meta.json"
            staged.write_text(json.dumps(manifest_data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            manifest = promote_artifact(repo, staged, id=manifest_id, source_id=clip["source_id"], kind="manifest",
                                        profile="clip-manifest-1.0", parent_artifact_id=clip["id"],
                                        start_ms=clip["start_ms"], end_ms=clip["end_ms"], approval_id=approval_id,
                                        metadata={"schema_version": MANIFEST_SCHEMA_VERSION, "requested_use": requested_use})
    with repo.connect() as connection:
        connection.execute(
            "INSERT INTO clip_provenance(id,clip_id,source_id,candidate_id,approval_id,source_start_ms,source_end_ms,requested_use,rights_status,manifest_artifact_id,metadata_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("prov_" + uuid.uuid4().hex, clip["id"], clip["source_id"], candidate_id, approval_id,
             clip["start_ms"], clip["end_ms"], requested_use, policy["rights_status"], manifest["id"],
             json.dumps(manifest_data, ensure_ascii=False, sort_keys=True), datetime.now(timezone.utc).isoformat()),
        )
    return manifest


def verified_provenance(repo, clip: dict) -> dict:
    """Cross-check portable manifest content against immutable DB identity."""
    with repo.connect() as connection:
        row = connection.execute("SELECT * FROM clip_provenance WHERE clip_id=? ORDER BY created_at DESC LIMIT 1", (clip["id"],)).fetchone()
    if not row or not row["manifest_artifact_id"]:
        raise SearchError("Clip has no completed provenance manifest", status_code=409)
    manifest = repo.get("artifacts", row["manifest_artifact_id"])
    manifest_path = verified_artifact_path(repo, manifest)
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SearchError("Clip provenance manifest is invalid", status_code=409) from exc
    parent = repo.get("artifacts", clip["parent_artifact_id"])
    checks = {
        "clip_id": clip["id"], "parent_source_id": clip["source_id"],
        "output_sha256": clip["sha256"], "input_sha256": parent["sha256"],
        "source_start_ms": clip["start_ms"], "source_end_ms": clip["end_ms"],
        "approval_id": clip["approval_id"],
    }
    if any(data.get(key) != value for key, value in checks.items()):
        raise SearchError("Clip provenance does not agree with database records", status_code=409)
    if data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise SearchError("Clip provenance schema is unsupported", status_code=409)
    return data
