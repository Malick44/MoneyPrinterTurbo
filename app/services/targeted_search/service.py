"""Application service shared by API, CLI and the lazy Streamlit library."""

from __future__ import annotations

import hashlib
import importlib.util
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from app.models.search import REQUESTED_USES, RIGHTS_STATUSES, SearchError
from .captions import ingest_captions
from .discovery import canonicalize_url, persist_metadata
from .policy import (
    authorize,
    current_policy,
    file_sha256,
    is_expired,
    owned_source_path,
    validate_evidence,
)
from .repository import Repository, decode, json_text, new_id, now
from .retrieval import retrieve, rerank
from .settings import Settings


def _public_job(job: dict | None) -> dict | None:
    if job is None:
        return None

    def public(value):
        if isinstance(value, dict):
            return {
                key: public(item)
                for key, item in value.items()
                if key
                not in {
                    "path",
                    "local_path",
                    "cookiefile",
                    "cookies",
                    "http_headers",
                    "raw_json",
                    "metadata_json",
                    "payload_json",
                    "result_json",
                }
            }
        if isinstance(value, list):
            return [public(item) for item in value]
        return value

    return public(job)


class SearchService:
    def __init__(self, root_dir: str | Path | None = None):
        self.repo = Repository(root_dir)
        self.settings = replace(Settings.from_config(), root_dir=self.repo.root)
        self.repo.settings = self.settings

    def _require_enabled(self) -> None:
        if not self.settings.enabled:
            raise SearchError(
                "Targeted video search is disabled in the application settings.", 503
            )

    def _require_source(self, source_id: str) -> dict:
        source = self.repo.get("sources", source_id)
        if source is None:
            raise SearchError("Source not found.", 404)
        return source

    def _require_candidate(self, candidate_id: str) -> dict:
        candidate = self.repo.get("candidates", candidate_id)
        if candidate is None:
            raise SearchError("Search candidate not found.", 404)
        return candidate

    def _register(
        self, url: str, collection_id: str | None, registration_only: bool = False
    ) -> dict:
        identifier, platform, canonical = canonicalize_url(
            url, self.settings, registration_only
        )
        if collection_id and self.repo.get("collections", collection_id) is None:
            raise SearchError("Source collection not found.", 404)
        timestamp = now()
        with self.repo.connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sources(id,platform,platform_video_id,canonical_url,discovered_at,updated_at) VALUES(?,?,?,?,?,?)",
                (
                    identifier,
                    platform,
                    identifier.split(":", 1)[1],
                    canonical,
                    timestamp,
                    timestamp,
                ),
            )
            if collection_id:
                count = connection.execute(
                    "SELECT count(*) FROM collection_sources WHERE collection_id=?",
                    (collection_id,),
                ).fetchone()[0]
                already = connection.execute(
                    "SELECT 1 FROM collection_sources WHERE collection_id=? AND source_id=?",
                    (collection_id, identifier),
                ).fetchone()
                if count >= self.settings.max_collection_sources and not already:
                    raise SearchError(
                        "This collection has reached its configured source limit."
                    )
                connection.execute(
                    "INSERT OR IGNORE INTO collection_sources VALUES(?,?)",
                    (collection_id, identifier),
                )
        return self._require_source(identifier)

    def discover(
        self, url: str, collection_id: str | None = None, metadata: dict | None = None
    ) -> dict:
        self._require_enabled()
        source = self._register(url, collection_id)
        if metadata is not None:
            if not isinstance(metadata, dict):
                raise SearchError("Source metadata must be an object.")
            local_path = metadata.get("local_path")
            if source["platform"] == "local" and local_path:
                from app.utils import utils

                owned = (self.repo.root / "owned").resolve()
                local_root = Path(
                    utils.storage_dir("local_videos", create=True)
                ).resolve()
                path = Path(local_path).expanduser().resolve()
                if (
                    not (path.is_relative_to(owned) or path.is_relative_to(local_root))
                    or not path.is_file()
                ):
                    raise SearchError(
                        "Owned media must be imported into the local material or owned-search directory."
                    )
                with self.repo.connect() as connection:
                    connection.execute(
                        "UPDATE sources SET local_path=? WHERE id=?",
                        (str(path), source["id"]),
                    )
            persist_metadata(self.repo, source["id"], metadata)
            caption_data = metadata.get("captions") or metadata.get("cues")
            if caption_data:
                ingest_captions(
                    self.repo,
                    source["id"],
                    caption_data,
                    str(metadata.get("language", "en")),
                    str(metadata.get("caption_format", "vtt")),
                    "manual",
                    "provided",
                )
            self.repo.event(
                "source_registered",
                source_id=source["id"],
                payload={
                    "metadata_only": not bool(caption_data),
                    "media_downloaded": False,
                },
            )
            return self.get_source(source["id"])
        if source["platform"] == "local":
            return self.get_source(source["id"])
        # Discovery has no media acquisition side effect and needs no rights grant.
        key = f"discovery:{source['id']}:{self.settings.pipeline_version}"
        job = self.repo.enqueue(
            "discover_source",
            {
                "source_id": source["id"],
                "url": source["canonical_url"],
                "collection_id": None,
            },
            key,
        )
        result = self.get_source(source["id"])
        result["job_id"] = job["id"]
        return result

    def refresh_source(self, source_id: str) -> dict:
        source = self._require_source(source_id)
        canonicalize_url(source["canonical_url"], self.settings)
        return _public_job(
            self.repo.enqueue(
                "discover_source",
                {
                    "source_id": source_id,
                    "url": source["canonical_url"],
                    "collection_id": None,
                },
                f"refresh:{source_id}:{new_id()}",
            )
        )

    def discover_many(self, urls: list[str], collection_id: str | None = None) -> dict:
        if (
            not isinstance(urls, list)
            or len(urls) > self.settings.max_collection_sources
        ):
            raise SearchError(
                "A selected source batch exceeds the configured collection cap."
            )
        sources = [
            self.discover(url, collection_id=collection_id)
            for url in dict.fromkeys(urls)
        ]
        return {"sources": sources, "count": len(sources)}

    def expand_collection(self, url: str, collection_id: str, limit: int = 50) -> dict:
        self._require_enabled()
        from .discovery import expand_collection

        return expand_collection(self, url, collection_id, limit)

    def register_metadata(
        self,
        url: str,
        title: str,
        description: str,
        creator_name: str = "",
        collection_id: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        self._require_enabled()
        source = self._register(url, collection_id, registration_only=True)
        supplied = {
            **(metadata or {}),
            "title": str(title),
            "description": str(description),
            "creator_name": str(creator_name),
            "evidence_type": "metadata",
        }
        # A researched catalogue entry has no transcript and no inferred duration.
        persist_metadata(self.repo, source["id"], supplied, "researched-metadata")
        self.repo.event(
            "metadata_registered",
            source_id=source["id"],
            payload={"evidence_type": "metadata", "media_downloaded": False},
        )
        return self.get_source(source["id"])

    def get_source(self, source_id: str) -> dict:
        source = self._require_source(source_id)
        source["policy"] = current_policy(self.repo, source_id)
        source["rights_status"] = (
            source["policy"]["rights_status"] if source["policy"] else "unknown"
        )
        if source["policy"] and is_expired(source["policy"].get("expires_at")):
            source["rights_status"] = "expired"
        source["artifacts"] = self.list_artifacts(source_id)
        with self.repo.connect() as connection:
            source["caption_count"] = connection.execute(
                "SELECT count(*) FROM captions WHERE source_id=? AND is_active=1",
                (source_id,),
            ).fetchone()[0]
            source["chunk_count"] = connection.execute(
                "SELECT count(*) FROM transcript_chunks t JOIN captions c ON c.id=t.caption_id WHERE t.source_id=? AND c.is_active=1",
                (source_id,),
            ).fetchone()[0]
        source.pop("local_path", None)
        source.pop("metadata_json", None)
        if source.get("metadata"):
            source["metadata"] = {
                key: value
                for key, value in source["metadata"].items()
                if key != "local_path"
            }
        return source

    def list_sources(self, collection_id: str | None = None) -> list[dict]:
        with self.repo.connect() as connection:
            if collection_id:
                rows = connection.execute(
                    "SELECT s.id FROM sources s JOIN collection_sources c ON c.source_id=s.id WHERE c.collection_id=? ORDER BY s.discovered_at DESC",
                    (collection_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id FROM sources ORDER BY discovered_at DESC"
                ).fetchall()
        return [self.get_source(row[0]) for row in rows]

    def search(self, query: str, filters: dict | None = None, top_k: int = 20) -> dict:
        self._require_enabled()
        query = str(query).strip()
        if not query or len(query) > 2000:
            raise SearchError("Enter a search query of at most 2000 characters.")
        if (
            isinstance(top_k, bool)
            or not isinstance(top_k, int)
            or not 1 <= top_k <= 100
        ):
            raise SearchError("top_k must be between 1 and 100.")
        filters = filters or {}
        if not isinstance(filters, dict):
            raise SearchError("Search filters must be an object.")
        records, versions = retrieve(self, query, filters, top_k)
        search_id, timestamp = new_id("search_"), now()
        results = []
        with self.repo.connect() as connection:
            connection.execute(
                "INSERT INTO search_runs VALUES(?,?,?,?,?)",
                (search_id, query, json_text(filters), json_text(versions), timestamp),
            )
            for record in records:
                identifier = new_id("cand_")
                evidence_hash = hashlib.sha256(
                    json_text(
                        {
                            key: record[key]
                            for key in (
                                "source_id",
                                "start_ms",
                                "end_ms",
                                "evidence",
                                "evidence_type",
                                "evidence_ids",
                            )
                        }
                    ).encode()
                ).hexdigest()
                connection.execute(
                    "INSERT INTO candidates(id,search_id,source_id,start_ms,end_ms,evidence,evidence_type,evidence_ids_json,evidence_hash,scores_json,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        search_id,
                        record["source_id"],
                        record["start_ms"],
                        record["end_ms"],
                        record["evidence"],
                        record["evidence_type"],
                        json_text(record["evidence_ids"]),
                        evidence_hash,
                        json_text(record["scores"]),
                        "ranked",
                        timestamp,
                    ),
                )
                results.append(
                    {
                        **record,
                        "id": identifier,
                        "candidate_id": identifier,
                        "search_id": search_id,
                        "evidence_hash": evidence_hash,
                    }
                )
        for result in results:
            source = self.get_source(result["source_id"])
            result.update(
                {
                    "title": source["title"],
                    "canonical_url": source["canonical_url"],
                    "creator_name": source["creator_name"],
                    "rights_status": source["rights_status"],
                }
            )
            try:
                authorize(self.repo, source["id"], "internal_review")
                policy_permits = True
            except SearchError:
                policy_permits = False
            result["actions"] = {
                "can_download": False,
                "policy_permits_internal_review": policy_permits,
                "requires_review": True,
            }
        return {
            "search_id": search_id,
            "query": query,
            "results": results,
            "model_versions": versions,
        }

    def set_policy(
        self,
        source_id: str,
        rights_status: str,
        permitted_use: str,
        reason: str,
        reviewed_by: str,
        expires_at: str | None = None,
    ) -> dict:
        self._require_source(source_id)
        if rights_status not in RIGHTS_STATUSES:
            raise SearchError("Unknown source rights status.")
        if isinstance(permitted_use, (list, tuple)):
            permitted_use = ",".join(permitted_use)
        uses = {item.strip() for item in str(permitted_use).split(",") if item.strip()}
        if not uses or not uses.issubset(REQUESTED_USES | {"*", "all"}):
            raise SearchError("Select explicit permitted media uses.")
        if not str(reason).strip() or not str(reviewed_by).strip():
            raise SearchError("A rights review needs its reason and reviewer identity.")
        if expires_at:
            try:
                date = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                if date.tzinfo is None:
                    raise ValueError
                expires_at = date.isoformat(timespec="milliseconds")
            except (ValueError, TypeError):
                raise SearchError(
                    "Policy expiry must be an ISO-8601 date with timezone."
                ) from None
        identifier = new_id("policy_")
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM policies WHERE source_id=?",
                (source_id,),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO policies VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    identifier,
                    source_id,
                    rights_status,
                    ",".join(sorted(uses)),
                    str(reason).strip(),
                    str(reviewed_by).strip(),
                    expires_at,
                    version,
                    now(),
                ),
            )
        self.repo.event(
            "rights_reviewed",
            source_id=source_id,
            payload={
                "policy_id": identifier,
                "version": version,
                "rights_status": rights_status,
                "reviewed_by": reviewed_by,
            },
        )
        return self.repo.get("policies", identifier)

    def validate(self, candidate_id: str) -> dict:
        candidate = self._require_candidate(candidate_id)
        validate_evidence(self.repo, candidate)
        search = self.repo.get("search_runs", candidate["search_id"])
        result = {
            "decision": "review",
            "relevance": None,
            "reason": "Confirm that this evidence demonstrates the requested topic and review the selected source window.",
            "evidence": [],
        }
        if candidate["start_ms"] is not None and candidate["evidence_type"] in {
            "transcript",
            "ocr",
        }:
            result["evidence"] = [
                {
                    "start_ms": candidate["start_ms"],
                    "end_ms": candidate["end_ms"],
                    "quote": candidate["evidence"],
                }
            ]
            if self.settings.rerank_enabled:
                ranked, versions = rerank(
                    self,
                    search["query"],
                    [{"evidence": candidate["evidence"], "scores": {}}],
                )
                score = ranked[0]["scores"]["reranker"]
                result.update(
                    {
                        "relevance": score,
                        "decision": "approve" if score >= 0.85 else "review",
                        "validator": "cross-encoder",
                        "model_versions": versions,
                    }
                )
        elif candidate["evidence_type"] == "metadata":
            result["reason"] = (
                "This result contains catalogue metadata only. Discover captions or manually review the actual video before approving a clip window."
            )
        with self.repo.connect() as connection:
            connection.execute(
                "UPDATE candidates SET validation_json=?,status=? WHERE id=?",
                (
                    json_text(result),
                    "verified"
                    if result["decision"] == "approve"
                    else "review_required",
                    candidate_id,
                ),
            )
        from app.config import config

        if (
            config.app.get("targeted_search_llm_validation_enabled", False)
            and candidate["start_ms"] is not None
            and candidate["evidence_type"] in {"transcript", "ocr"}
        ):
            from .llm_evidence import validate_candidate

            return validate_candidate(self, candidate_id)
        self.repo.event(
            "candidate_validated",
            source_id=candidate["source_id"],
            payload={
                "candidate_id": candidate_id,
                "decision": result["decision"],
                "evidence_hash": candidate["evidence_hash"],
            },
        )
        return result

    def _range(
        self, candidate: dict, start_ms: int | None, end_ms: int | None, required: bool
    ) -> tuple[int | None, int | None]:
        start_ms = candidate["start_ms"] if start_ms is None else start_ms
        end_ms = candidate["end_ms"] if end_ms is None else end_ms
        if start_ms is None and end_ms is None and not required:
            return None, None
        if (
            isinstance(start_ms, bool)
            or isinstance(end_ms, bool)
            or not isinstance(start_ms, int)
            or not isinstance(end_ms, int)
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise SearchError(
                "Select a valid clip start and end in source milliseconds."
            )
        source = self._require_source(candidate["source_id"])
        if source["duration_ms"] is None:
            raise SearchError(
                "Discover the source's measured duration before approving an exact clip window."
            )
        if (
            end_ms > source["duration_ms"]
            or end_ms - start_ms > self.settings.max_clip_duration_ms
        ):
            raise SearchError(
                "The clip window exceeds the source duration or configured clip limit."
            )
        return start_ms, end_ms

    def approve_download(
        self,
        candidate_id: str,
        requested_use: str = "internal_review",
        reviewed_by: str = "local-user",
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> dict:
        self._require_enabled()
        candidate = self._require_candidate(candidate_id)
        validate_evidence(self.repo, candidate)
        if not str(reviewed_by).strip():
            raise SearchError("Approval needs a reviewer identity.")
        start_ms, end_ms = self._range(candidate, start_ms, end_ms, required=False)
        policy = authorize(self.repo, candidate["source_id"], requested_use)
        source = self._require_source(candidate["source_id"])
        source_sha256 = (
            file_sha256(owned_source_path(self.repo, source))
            if source["platform"] == "local" and source.get("local_path")
            else None
        )
        identifier = new_id("approval_")
        with self.repo.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # Recheck the selected current policy while holding the write lock.
            latest = connection.execute(
                "SELECT id FROM policies WHERE source_id=? ORDER BY version DESC LIMIT 1",
                (candidate["source_id"],),
            ).fetchone()
            if latest[0] != policy["id"]:
                raise SearchError(
                    "Rights changed while approving; review the source again.", 409
                )
            existing = connection.execute(
                "SELECT * FROM approvals WHERE candidate_id=? AND requested_use=? AND start_ms IS ? AND end_ms IS ? AND policy_id=? AND evidence_hash=? AND metadata_hash IS ? AND source_file_sha256 IS ? ORDER BY created_at DESC LIMIT 1",
                (
                    candidate_id,
                    requested_use,
                    start_ms,
                    end_ms,
                    policy["id"],
                    candidate["evidence_hash"],
                    source["metadata_hash"],
                    source_sha256,
                ),
            ).fetchone()
            if existing and not is_expired(existing["expires_at"]):
                identifier = existing["id"]
            else:
                connection.execute(
                    "INSERT INTO approvals(id,candidate_id,source_id,requested_use,start_ms,end_ms,reviewed_by,policy_id,policy_version,evidence_hash,metadata_hash,expires_at,created_at,source_file_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier,
                        candidate_id,
                        candidate["source_id"],
                        requested_use,
                        start_ms,
                        end_ms,
                        str(reviewed_by).strip(),
                        policy["id"],
                        policy["version"],
                        candidate["evidence_hash"],
                        source["metadata_hash"],
                        policy.get("expires_at"),
                        now(),
                        source_sha256,
                    ),
                )
            manual = {
                "decision": "approve",
                "validator": "manual",
                "reviewed_by": str(reviewed_by).strip(),
                "evidence_hash": candidate["evidence_hash"],
                "source_start_ms": start_ms,
                "source_end_ms": end_ms,
                "created_at": now(),
            }
            connection.execute(
                "UPDATE candidates SET status='manually_verified',validation_json=? WHERE id=?",
                (json_text(manual), candidate_id),
            )
        payload = {
            "source_id": candidate["source_id"],
            "candidate_id": candidate_id,
            "approval_id": identifier,
            "requested_use": requested_use,
            "start_ms": start_ms,
            "end_ms": end_ms,
        }
        job = self.repo.enqueue(
            "download_source",
            payload,
            f"download:{identifier}:{self.settings.pipeline_version}",
        )
        self.repo.event(
            "download_approved",
            source_id=candidate["source_id"],
            job_id=job["id"],
            payload={
                "approval_id": identifier,
                "requested_use": requested_use,
                "reviewed_by": reviewed_by,
                "start_ms": start_ms,
                "end_ms": end_ms,
            },
        )
        return {
            **self.repo.get("approvals", identifier),
            "approval_id": identifier,
            "job_id": job["id"],
        }

    def _approval(
        self,
        candidate: dict,
        requested_use: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> dict:
        with self.repo.connect() as connection:
            rows = [
                decode(row)
                for row in connection.execute(
                    "SELECT * FROM approvals WHERE candidate_id=? AND requested_use=? ORDER BY created_at DESC",
                    (candidate["id"], requested_use),
                )
            ]
        for approval in rows:
            try:
                authorize(
                    self.repo,
                    candidate["source_id"],
                    requested_use,
                    approval["id"],
                    start_ms,
                    end_ms,
                )
                return approval
            except SearchError:
                continue
        raise SearchError(
            "Approve this candidate, requested use and clip window under the current rights policy first.",
            403,
        )

    def enqueue_clip(
        self,
        candidate_id: str,
        start_ms: int | None = None,
        end_ms: int | None = None,
        requested_use: str = "internal_review",
    ) -> dict:
        self._require_enabled()
        candidate = self._require_candidate(candidate_id)
        start_ms, end_ms = self._range(candidate, start_ms, end_ms, required=True)
        approval = self._approval(candidate, requested_use, start_ms, end_ms)
        key = f"clip:{approval['id']}:{start_ms}:{end_ms}:{self.settings.pipeline_version}"
        clip_id = "clip_" + hashlib.sha256(key.encode()).hexdigest()[:32]
        payload = {
            "clip_id": clip_id,
            "source_id": candidate["source_id"],
            "candidate_id": candidate_id,
            "approval_id": approval["id"],
            "requested_use": requested_use,
            "start_ms": start_ms,
            "end_ms": end_ms,
        }
        return _public_job(self.repo.enqueue("extract_clip", payload, key))

    def get_clip(self, clip_id: str) -> dict:
        artifact = self.repo.get("artifacts", clip_id)
        if artifact is None or artifact["kind"] != "clip":
            raise SearchError("Extracted clip not found.", 404)
        with self.repo.connect() as connection:
            artifact["derivatives"] = [
                decode(row)
                for row in connection.execute(
                    "SELECT * FROM artifacts WHERE parent_artifact_id=? ORDER BY created_at",
                    (clip_id,),
                )
            ]
            artifact["provenance"] = decode(
                connection.execute(
                    "SELECT * FROM clip_provenance WHERE clip_id=? ORDER BY created_at DESC LIMIT 1",
                    (clip_id,),
                ).fetchone()
            )
        return artifact

    def list_artifacts(self, source_id: str | None = None) -> list[dict]:
        with self.repo.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM artifacts"
                + (" WHERE source_id=?" if source_id else "")
                + " ORDER BY created_at DESC",
                [source_id] if source_id else [],
            ).fetchall()
        return [decode(row) for row in rows]

    def get_job(self, job_id: str) -> dict:
        job = self.repo.get("jobs", job_id)
        if job is None:
            raise SearchError("Search processing job not found.", 404)
        return _public_job(job)

    def list_jobs(self) -> list[dict]:
        with self.repo.connect() as connection:
            return [
                _public_job(decode(row))
                for row in connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200"
                )
            ]

    def add_collection(
        self, name: str, topic: str, queries: list[str] | None = None
    ) -> dict:
        if not str(name).strip():
            raise SearchError("A source collection needs a name.")
        identifier = new_id("collection_")
        with self.repo.connect() as connection:
            connection.execute(
                "INSERT INTO collections VALUES(?,?,?,?,?)",
                (
                    identifier,
                    str(name).strip(),
                    str(topic).strip(),
                    json_text(queries or []),
                    now(),
                ),
            )
        return {**self.repo.get("collections", identifier), "source_count": 0}

    def list_collections(self) -> list[dict]:
        with self.repo.connect() as connection:
            return [
                decode(row)
                for row in connection.execute(
                    "SELECT c.*,count(cs.source_id) AS source_count FROM collections c LEFT JOIN collection_sources cs ON cs.collection_id=c.id GROUP BY c.id ORDER BY c.created_at DESC"
                )
            ]

    def import_captions(
        self,
        source_id: str,
        text,
        language: str = "en",
        kind: str = "manual",
        format: str = "vtt",
    ) -> dict:
        return ingest_captions(
            self.repo,
            source_id,
            text,
            language=language,
            kind=kind,
            format=format,
            provider="provided",
        )

    def enqueue_index(self, source_id: str) -> dict:
        source = self._require_source(source_id)
        if not self.settings.semantic_enabled:
            raise SearchError(
                "Enable semantic retrieval before building its local index."
            )
        with self.repo.connect() as connection:
            hashes = [
                row[0]
                for row in connection.execute(
                    "SELECT sha256 FROM captions WHERE source_id=? AND is_active=1 ORDER BY sha256",
                    (source_id,),
                )
            ]
            ocr_hashes = [
                hashlib.sha256(row[0].encode()).hexdigest()
                for row in connection.execute(
                    "SELECT text FROM ocr_blocks WHERE source_id=? ORDER BY id",
                    (source_id,),
                )
            ]
        digest = hashlib.sha256(
            json_text([source.get("metadata_hash"), hashes, ocr_hashes]).encode()
        ).hexdigest()[:16]
        return _public_job(
            self.repo.enqueue(
                "build_embeddings",
                {"source_id": source_id},
                f"embeddings:{source_id}:{digest}:{self.settings.embedding_model}:{self.settings.embedding_revision}",
            )
        )

    enqueue_embeddings = enqueue_index

    def _analysis_job(self, source_id: str, job_type: str) -> dict:
        self._require_source(source_id)
        authorize(self.repo, source_id, "analysis")
        with self.repo.connect() as connection:
            rows = [
                decode(row)
                for row in connection.execute(
                    "SELECT * FROM approvals WHERE source_id=? ORDER BY created_at DESC",
                    (source_id,),
                )
            ]
        approval = None
        for row in rows:
            try:
                authorize(self.repo, source_id, row["requested_use"], row["id"])
                approval = row
                break
            except SearchError:
                continue
        if approval is None:
            raise SearchError(
                "Approve a relevant candidate under this source policy before analysis acquisition.",
                403,
            )
        payload = {
            "source_id": source_id,
            "candidate_id": approval["candidate_id"],
            "approval_id": approval["id"],
            "requested_use": approval["requested_use"],
            "start_ms": approval["start_ms"],
            "end_ms": approval["end_ms"],
        }
        key = (
            f"{job_type}:{source_id}:{approval['id']}:{self.settings.pipeline_version}"
        )
        return _public_job(self.repo.enqueue(job_type, payload, key))

    def enqueue_visual(self, source_id: str) -> dict:
        if not self.settings.visual_enabled and not self.settings.ocr_enabled:
            raise SearchError(
                "Enable visual or OCR indexing before requesting analysis."
            )
        return self._analysis_job(source_id, "visual_index")

    enqueue_visual_index = enqueue_visual

    def enqueue_transcription(self, source_id: str) -> dict:
        return self._analysis_job(source_id, "transcribe_source")

    def capabilities(self) -> dict:
        warnings = []
        installed = {}
        for key, module in (
            ("discovery", "yt_dlp"),
            ("semantic", "sentence_transformers"),
            ("visual", "open_clip"),
            ("ocr", "pytesseract"),
            ("transcription", "faster_whisper"),
            ("documents", "pypdf"),
            ("document_preview", "pypdfium2"),
        ):
            try:
                installed[key] = importlib.util.find_spec(module) is not None
            except (ValueError, ImportError):
                installed[key] = False
        if not installed["discovery"]:
            warnings.append(
                "Install the targeted-search extra for remote caption discovery; metadata registration and local captions remain available."
            )
        if (
            self.settings.semantic_enabled or self.settings.rerank_enabled
        ) and not installed["semantic"]:
            warnings.append(
                "Semantic retrieval and reranking need the search-semantic extra and configured local models."
            )
        if self.settings.visual_enabled and (
            not installed["visual"]
            or (self.settings.local_models_only and not self.settings.vision_checkpoint)
        ):
            warnings.append(
                "Visual similarity needs the search-visual extra and an installed local OpenCLIP checkpoint."
            )
        if self.settings.ocr_enabled and not installed["ocr"]:
            warnings.append("OCR requires pytesseract and the Tesseract executable.")
        return {
            "enabled": self.settings.enabled,
            "case_workspace": True,
            "default_search_mode": "footage",
            "lexical": True,
            "semantic_enabled": self.settings.semantic_enabled,
            "reranker_enabled": self.settings.rerank_enabled,
            "visual_enabled": self.settings.visual_enabled,
            "ocr_enabled": self.settings.ocr_enabled,
            "local_models_only": self.settings.local_models_only,
            "installed": installed,
            "warnings": warnings,
            "embedding_model": self.settings.embedding_model,
            "reranker_model": self.settings.reranker_model,
        }
