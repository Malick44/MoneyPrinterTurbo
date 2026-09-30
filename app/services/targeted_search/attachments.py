"""Verified generation bridge and policy-aware artifact delivery."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.models.search import SearchError

from .media import sha256_file, verified_artifact_path
from .policy import authorize
from .provenance import verified_provenance
from .repository import Repository


def _bridge_root(repo) -> Path:
    from app.utils import utils

    return Path(utils.storage_dir("local_videos")).resolve()


def _task_root(repo, task_id: str) -> Path:
    from app.utils import utils

    return Path(utils.task_dir(task_id)).resolve()


def _check_artifact(repo, artifact: dict, requested_use: str) -> Path:
    if not artifact:
        raise SearchError("Artifact was not found", status_code=404)
    approval_id = artifact.get("approval_id")
    intrinsic_use = artifact.get("metadata", {}).get("requested_use", "internal_review")
    if approval_id:
        approval = repo.get("approvals", approval_id)
        intrinsic_use = approval["requested_use"] if approval else intrinsic_use
        authorize(repo, artifact["source_id"], intrinsic_use, approval_id, artifact.get("start_ms"), artifact.get("end_ms"))
    authorize(repo, artifact["source_id"], requested_use, start_ms=artifact.get("start_ms"), end_ms=artifact.get("end_ms"))
    return verified_artifact_path(repo, artifact)


def artifact_content(artifact_id: str, requested_use: str = "internal_review", root_dir=None) -> Path:
    repo = Repository(root_dir)
    return _check_artifact(repo, repo.get("artifacts", artifact_id), requested_use)


def attach_clip(artifact_id: str, requested_use: str = "generated_export", root_dir=None) -> dict:
    repo = Repository(root_dir)
    if requested_use not in {"generated_export", "publication"}:
        raise SearchError("Generation attachment requires an export permission", status_code=403)
    clip = repo.get("artifacts", artifact_id)
    source_path = _check_artifact(repo, clip, requested_use)
    if clip["kind"] != "clip":
        raise SearchError("Only an extracted source-quality clip can be attached", status_code=422)
    provenance = verified_provenance(repo, clip)
    # Clip identity is server-derived and cannot introduce path components.
    filename = "search-" + uuid.uuid5(uuid.NAMESPACE_URL, artifact_id).hex + ".mp4"
    local_root = _bridge_root(repo)
    local_root.mkdir(parents=True, exist_ok=True)
    target = local_root / filename
    if target.exists():
        if sha256_file(target) != clip["sha256"]:
            raise SearchError("Generation bridge copy failed digest verification", status_code=409)
    else:
        with tempfile.NamedTemporaryFile(dir=local_root, prefix=".search-", suffix=".tmp", delete=False) as stream:
            staged = Path(stream.name)
        try:
            shutil.copyfile(source_path, staged)
            if sha256_file(staged) != clip["sha256"]:
                raise SearchError("Generation bridge copy failed digest verification", status_code=409)
            _check_artifact(repo, clip, requested_use)
            os.replace(staged, target)
        finally:
            staged.unlink(missing_ok=True)
    with repo.connect() as connection:
        found = connection.execute("SELECT id FROM attachments WHERE artifact_id=? AND task_id IS NULL AND local_path=?", (artifact_id, str(target))).fetchone()
        if not found:
            connection.execute("INSERT INTO attachments(id,artifact_id,task_id,local_path,sha256,created_at) VALUES(?,?,?,?,?,?)", ("att_" + uuid.uuid4().hex, artifact_id, None, str(target), clip["sha256"], datetime.now(timezone.utc).isoformat()))
    return {
        "provider": "local", "url": str(target), "local_file": str(target),
        "duration": round(clip.get("metadata", {}).get("duration_ms", 0) / 1000),
        "artifact_id": artifact_id,
        "source_info": {"source_id": clip["source_id"], "artifact_id": artifact_id,
                        "source_start_ms": clip["start_ms"], "source_end_ms": clip["end_ms"],
                        "sha256": clip["sha256"], "canonical_source_url": provenance["canonical_source_url"]},
    }


def _field(material: Any, key: str, default=None):
    return material.get(key, default) if isinstance(material, dict) else getattr(material, key, default)


def _find_reference(repo, material) -> tuple[str | None, Path | None]:
    supplied_id = _field(material, "artifact_id") or _field(material, "material_artifact_id")
    raw_path = _field(material, "url") or _field(material, "local_path", "")
    resolved = Path(raw_path).expanduser().resolve() if raw_path else None
    with repo.connect() as connection:
        mappings = connection.execute("SELECT * FROM attachments WHERE local_path=?", (str(resolved),)).fetchall() if resolved else []
        if not mappings and raw_path and Path(raw_path).name.startswith("search-"):
            mappings = connection.execute("SELECT * FROM attachments WHERE local_path LIKE ?", ("%/" + Path(raw_path).name,)).fetchall()
        mapping_ids = {row["artifact_id"] for row in mappings}
        # Renaming a registered search clip must not turn it into an unchecked
        # upload. Only fingerprint files within the existing local-video root.
        if not mappings and not supplied_id and resolved and resolved.is_relative_to(_bridge_root(repo)) and resolved.is_file():
            digest = sha256_file(resolved)
            mapping_ids = {row["id"] for row in connection.execute("SELECT id FROM artifacts WHERE kind='clip' AND sha256=?", (digest,))}
    if supplied_id and mapping_ids and supplied_id not in mapping_ids:
        raise SearchError("Material artifact reference does not match the bridge file", status_code=409)
    if len(mapping_ids) > 1 and not supplied_id:
        raise SearchError("Material bridge mapping is ambiguous", status_code=409)
    artifact_id = supplied_id or (next(iter(mapping_ids)) if len(mapping_ids) == 1 else None)
    if not artifact_id and raw_path and Path(raw_path).name.startswith("search-"):
        raise SearchError("Search material requires a retained artifact mapping", status_code=403)
    return artifact_id, resolved


def authorize_attachments(materials, requested_use: str = "clip_export", root_dir=None) -> list[dict]:
    """Read-only authorization for delivery/publishing of existing task material."""
    repo = Repository(root_dir)
    results = []
    for material in materials or []:
        artifact_id, local_path = _find_reference(repo, material)
        if not artifact_id:
            continue
        clip = repo.get("artifacts", artifact_id)
        _check_artifact(repo, clip, requested_use)
        if clip["kind"] != "clip":
            raise SearchError("Task material is not an extracted clip", status_code=409)
        if local_path and (not local_path.is_file() or sha256_file(local_path) != clip["sha256"]):
            raise SearchError("Search material file failed digest verification", status_code=409)
        results.append(verified_provenance(repo, clip))
    return results


def resolve_attachments(materials, task_id: str, root_dir=None, requested_use: str = "generated_export") -> list[dict]:
    """Verify trusted refs and bridge filenames even when a client drops IDs.

    Returns canonical provenance only; ordinary uploaded local materials have
    no search provenance and continue through the existing local path checks.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", task_id):
        raise SearchError("Task identifier is invalid", status_code=422)
    repo = Repository(root_dir)
    verified = []
    seen = set()
    for material in materials or []:
        supplied_id = _field(material, "artifact_id")
        raw_path = _field(material, "url", "")
        artifact_id, resolved = _find_reference(repo, material)
        if not artifact_id:
            continue
        material_ref = attach_clip(artifact_id, requested_use=requested_use, root_dir=repo.root)
        target = Path(material_ref["url"])
        if raw_path and supplied_id and resolved != target:
            if not resolved or not resolved.is_relative_to(_bridge_root(repo)) or not resolved.is_file() or sha256_file(resolved) != service_sha(repo, artifact_id):
                raise SearchError("Material path does not match its search artifact", status_code=409)
        clip = repo.get("artifacts", artifact_id)
        if sha256_file(target) != clip["sha256"]:
            raise SearchError("Search material file failed digest verification", status_code=409)
        if isinstance(material, dict):
            material.update(url=str(target), artifact_id=artifact_id, provider="local")
        else:
            material.url, material.artifact_id, material.provider = str(target), artifact_id, "local"
        if artifact_id in seen:
            continue
        seen.add(artifact_id)
        provenance = verified_provenance(repo, clip)
        provenance.update(local_path=str(target), material_artifact_id=artifact_id, generation_use=requested_use)
        verified.append(provenance)
        with repo.connect() as connection:
            connection.execute("INSERT OR IGNORE INTO attachments(id,artifact_id,task_id,local_path,sha256,created_at) VALUES(?,?,?,?,?,?)", ("att_" + uuid.uuid4().hex, artifact_id, task_id, str(target), clip["sha256"], datetime.now(timezone.utc).isoformat()))
    if verified and requested_use == "generated_export":
        # Dedicated file survives both legacy and Codex script.json rewrites.
        task_root = _task_root(repo, task_id)
        task_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=task_root, prefix=".search-materials-", suffix=".tmp", delete=False) as stream:
            json.dump({"schema_version": "1.0", "task_id": task_id, "requested_use": requested_use, "materials": verified}, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            staged = Path(stream.name)
        os.replace(staged, task_root / "search-materials.json")
    return verified


def service_sha(repo, artifact_id: str) -> str:
    return repo.get("artifacts", artifact_id)["sha256"]
