"""Acquisition and derivative permission checks, independent of relevance models."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from pathlib import Path

from app.models.search import REQUESTED_USES, SearchError
from .repository import decode


EXPORT_USES = frozenset({"generated_export", "publication", "clip_export"})


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    try:
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
    except OSError:
        raise SearchError(
            "The approved owned media file is no longer available.", 409
        ) from None
    return digest.hexdigest()


def owned_source_path(repo, source: dict) -> Path | None:
    """Resolve case originals through their canonical, portable CAS identity."""
    if source.get("platform") != "local":
        return None
    if source.get("canonical_url", "").startswith("local://case-"):
        with repo.connect() as connection:
            row = connection.execute(
                "SELECT f.id FROM case_assets a JOIN artifacts f ON f.id=a.artifact_id WHERE a.source_id=? AND f.source_id=a.source_id AND f.kind='case_original' AND f.sha256=a.sha256 ORDER BY a.updated_at DESC LIMIT 1",
                (source["id"],),
            ).fetchone()
        if row:
            from .media import verified_artifact_path

            return verified_artifact_path(repo, repo.get("artifacts", row[0]))
    return Path(source["local_path"]) if source.get("local_path") else None


def validate_evidence(repo, candidate: dict) -> None:
    """A saved search result cannot authorize evidence replaced since retrieval."""
    ids = candidate.get("evidence_ids", [])
    if candidate["evidence_type"] in {"transcript", "visual_description"}:
        for identifier in ids:
            chunk = repo.get("transcript_chunks", identifier)
            caption = repo.get("captions", chunk["caption_id"]) if chunk else None
            if (
                chunk is None
                or chunk["source_id"] != candidate["source_id"]
                or caption is None
                or not caption["is_active"]
                or chunk["text"] not in candidate["evidence"]
            ):
                raise SearchError(
                    "The approved caption evidence was superseded; review the updated source evidence.",
                    403,
                )
    elif candidate["evidence_type"] == "ocr":
        for identifier in ids:
            block = repo.get("ocr_blocks", identifier)
            if (
                block is None
                or block["source_id"] != candidate["source_id"]
                or block["text"] not in candidate["evidence"]
            ):
                raise SearchError(
                    "The approved OCR evidence was superseded; review the updated visual evidence.",
                    403,
                )
    elif candidate["evidence_type"] == "visual":
        for identifier in ids:
            frame = repo.get("visual_frames", identifier)
            if frame is None or frame["source_id"] != candidate["source_id"]:
                raise SearchError(
                    "The approved visual evidence is no longer available.", 403
                )


def is_expired(value: str | None) -> bool:
    if not value:
        return False
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            return True
        return stamp <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return True


def current_policy(repo, source_id: str) -> dict | None:
    with repo.connect() as connection:
        return decode(
            connection.execute(
                "SELECT * FROM policies WHERE source_id=? ORDER BY version DESC LIMIT 1",
                (source_id,),
            ).fetchone()
        )


def authorize(
    repo,
    source_id: str,
    requested_use: str,
    approval_id: str | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> dict:
    if requested_use not in REQUESTED_USES:
        raise SearchError("Unknown requested media use.")
    policy = current_policy(repo, source_id)
    if policy is None or policy["rights_status"] not in {
        "allowed_internal",
        "allowed_export",
    }:
        raise SearchError(
            "This source needs a reviewed rights policy before media acquisition.", 403
        )
    if is_expired(policy.get("expires_at")):
        raise SearchError("The source rights policy has expired.", 403)
    if requested_use in EXPORT_USES and policy["rights_status"] != "allowed_export":
        raise SearchError("This source is approved for internal use only.", 403)
    uses = {item.strip() for item in policy["permitted_use"].split(",")}
    if requested_use not in uses and not uses.intersection({"all", "*"}):
        raise SearchError("The rights policy does not permit this requested use.", 403)
    if approval_id is not None:
        approval = repo.get("approvals", approval_id)
        if (
            approval is None
            or approval["source_id"] != source_id
            or approval["requested_use"] != requested_use
        ):
            raise SearchError(
                "A matching relevance and download approval is required.", 403
            )
        if (
            approval["policy_id"] != policy["id"]
            or approval["policy_version"] != policy["version"]
            or is_expired(approval.get("expires_at"))
        ):
            raise SearchError(
                "The download approval is stale; review the current rights policy.", 403
            )
        candidate = repo.get("candidates", approval["candidate_id"])
        if candidate is None or candidate["evidence_hash"] != approval["evidence_hash"]:
            raise SearchError("Candidate evidence changed after approval.", 403)
        source = repo.get("sources", source_id)
        if source is None or approval.get("metadata_hash") != source.get(
            "metadata_hash"
        ):
            raise SearchError(
                "Source metadata changed after approval; review the source again.", 403
            )
        validate_evidence(repo, candidate)
        if approval.get("source_file_sha256"):
            owned_path = owned_source_path(repo, source)
            if (
                not owned_path
                or file_sha256(owned_path) != approval["source_file_sha256"]
            ):
                raise SearchError(
                    "The owned source media changed after approval; review it again.",
                    403,
                )
        # A source-wide approval permits acquisition; a bounded approval permits only
        # this derivative window (and the full source needed to extract it).
        if start_ms is not None or end_ms is not None:
            if start_ms is None or end_ms is None or start_ms < 0 or end_ms <= start_ms:
                raise SearchError("A valid source time range is required.")
            if approval["start_ms"] is not None and (
                start_ms < approval["start_ms"] or end_ms > approval["end_ms"]
            ):
                raise SearchError(
                    "The requested clip falls outside the approved evidence window.",
                    403,
                )
    return policy
