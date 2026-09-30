"""Policy-gated local or yt-dlp acquisition into private staging storage."""

from __future__ import annotations

import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from app.models.search import SearchError

from .media import PIPELINE_VERSION, _staging, probe_media, promote_artifact, run_command, setting, sha256_file, verified_artifact_path
from .policy import authorize, owned_source_path


def _local_path(source: dict) -> Path | None:
    if source.get("platform") == "local":
        # Only the validated owned-import column is authoritative. Public
        # registration metadata must never supply a filesystem read path.
        raw = source.get("local_path")
        if not raw:
            raise SearchError("Owned local source has not been imported", status_code=403)
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise SearchError("Owned local source is missing", status_code=404)
        return path
    return None


def download_source(service, payload: dict) -> dict:
    repo = service.repo
    source_id = payload["source_id"]
    requested_use = payload.get("requested_use", "internal_review")
    approval_id = payload.get("approval_id")
    if not approval_id:
        raise SearchError("Media acquisition requires a scoped download approval", status_code=403)
    start_ms, end_ms = payload.get("start_ms"), payload.get("end_ms")
    authorize(repo, source_id, requested_use, approval_id, start_ms, end_ms)
    source = repo.get("sources", source_id)
    if not source:
        raise SearchError("Source was not found", status_code=404)
    local_path = owned_source_path(repo, source) if source.get("platform") == "local" and source.get("canonical_url", "").startswith("local://case-") else _local_path(source)
    local_digest = sha256_file(local_path) if local_path else None
    approval = repo.get("approvals", approval_id)
    expected_digest = approval.get("source_file_sha256") if approval else None
    if expected_digest and local_digest and expected_digest != local_digest:
        raise SearchError("Owned source media changed after approval", status_code=403)
    with repo.connect() as connection:
        rows = connection.execute("SELECT id FROM artifacts WHERE source_id=? AND kind IN ('source','case_original') ORDER BY created_at DESC", (source_id,)).fetchall()
    for row in rows:
        artifact = repo.get("artifacts", row["id"])
        if artifact["kind"] == "case_original" and artifact.get("metadata", {}).get("asset_kind") != "video":
            continue
        if expected_digest and artifact["sha256"] != expected_digest:
            continue
        if local_digest and artifact["sha256"] != local_digest:
            continue
        verified = verified_artifact_path(repo, artifact)
        if artifact["kind"] == "case_original":
            metadata = probe_media(verified)
            if metadata["duration_ms"] > setting(service, "max_source_duration_ms", 14_400_000) or artifact["bytes"] > setting(service, "max_download_bytes", 2 * 1024 ** 3):
                raise SearchError("Imported case footage exceeds the acquisition limits", 413)
            authorize(repo, source_id, requested_use, approval_id, start_ms, end_ms)
            metadata.update(pipeline_version=PIPELINE_VERSION, adapter="case-original", requested_use=requested_use)
            artifact = repo.insert_artifact(source_id=source_id, kind="source", profile="source-original", path=verified,
                                            sha256=artifact["sha256"], bytes=artifact["bytes"], parent_artifact_id=artifact["id"],
                                            approval_id=approval_id, metadata=metadata)
            with repo.connect() as connection:
                connection.execute("UPDATE sources SET duration_ms=? WHERE id=?", (metadata["duration_ms"], source_id))
        authorize(repo, source_id, requested_use, approval_id, start_ms, end_ms)
        return {"source_id": source_id, "artifact_id": artifact["id"], "reused": True}
    with tempfile.TemporaryDirectory(prefix="acquire-", dir=_staging(repo)) as directory:
        stage = Path(directory)
        maximum_bytes = setting(service, "max_download_bytes", 2 * 1024 ** 3)
        if local_path:
            if local_path.stat().st_size > maximum_bytes:
                raise SearchError("Source exceeds the configured download size limit", status_code=413)
            staged = stage / ("source" + local_path.suffix)
            shutil.copyfile(local_path, staged)
            if sha256_file(staged) != local_digest:
                raise SearchError("Owned local source changed while it was being acquired", status_code=409)
        else:
            url = source.get("canonical_url") or source.get("url")
            parsed = urlsplit(url or "")
            allowlist = setting(service, "allowed_domains", ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "vimeo.com", "www.vimeo.com"))
            if parsed.scheme != "https" or not parsed.hostname or not any(parsed.hostname == domain or parsed.hostname.endswith("." + domain) for domain in allowlist) or parsed.username or parsed.password:
                raise SearchError("Source is outside the configured acquisition allowlist", status_code=422)
            from .discovery import verify_public_network

            verify_public_network(url, service.settings)
            if importlib.util.find_spec("yt_dlp") is None:
                raise SearchError("Source acquisition requires the targeted-search extra: uv sync --extra targeted-search", status_code=503)
            maximum_duration = setting(service, "max_source_duration_ms", 14_400_000) // 1000
            run_command([
                sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist", "--no-cache-dir", "--no-part",
                "--no-warnings", "--socket-timeout", "30", "--retries", "2", "--fragment-retries", "2",
                "--max-filesize", str(maximum_bytes), "--match-filter", f"duration <= {maximum_duration}",
                "--format", "bv*[height<=1080]+ba/b[height<=1080]", "--merge-output-format", "mp4",
                "--output", str(stage / "source.%(ext)s"), "--", url,
            ], timeout=setting(service, "command_timeout_seconds", 1800))
            files = [p for p in stage.iterdir() if p.is_file() and p.suffix in {".mp4", ".mkv", ".webm", ".mov"}]
            if len(files) != 1:
                raise SearchError("Acquisition did not produce exactly one complete media file", status_code=422)
            staged = files[0]
        if staged.stat().st_size > maximum_bytes:
            raise SearchError("Acquired source exceeds the configured size limit", status_code=413)
        metadata = probe_media(staged)
        if metadata["duration_ms"] > setting(service, "max_source_duration_ms", 14_400_000):
            raise SearchError("Acquired source exceeds the configured duration limit", status_code=413)
        authorize(repo, source_id, requested_use, approval_id, start_ms, end_ms)
        metadata.update(pipeline_version=PIPELINE_VERSION, adapter="local" if local_path else "yt-dlp",
                        requested_use=requested_use)
        artifact = promote_artifact(repo, staged, source_id=source_id, kind="source", profile="source-original",
                                    approval_id=approval_id, metadata=metadata)
    with repo.connect() as connection:
        connection.execute("UPDATE sources SET duration_ms=? WHERE id=?", (metadata["duration_ms"], source_id))
    return {"source_id": source_id, "artifact_id": artifact["id"], "reused": False}
