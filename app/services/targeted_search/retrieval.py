"""Local lexical, semantic, OCR and visual retrieval with versioned evidence."""

from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache

from app.models.search import SearchError
from .policy import is_expired
from .repository import decode, json_text, new_id, now


def fts_query(query: str) -> str:
    words = re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    stop = {
        "a",
        "an",
        "the",
        "and",
        "or",
        "of",
        "in",
        "to",
        "for",
        "with",
        "show",
        "find",
        "video",
        "clip",
    }
    words = [word for word in words if word.casefold() not in stop]
    return " OR ".join('"' + word.replace('"', '""') + '"' for word in words[:80])


@lru_cache(maxsize=2)
def _text_model(name: str, revision: str, local_only: bool):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise SearchError(
            "Semantic search requires the search-semantic extra.", 503
        ) from None
    try:
        return SentenceTransformer(
            name,
            revision=revision,
            device="cpu",
            local_files_only=local_only,
            trust_remote_code=False,
        )
    except Exception:
        raise SearchError(
            "The configured embedding model is unavailable locally. Install it or explicitly allow model downloads.",
            503,
        ) from None


@lru_cache(maxsize=2)
def _cross_model(name: str, revision: str, local_only: bool):
    try:
        from sentence_transformers import CrossEncoder
        from torch.nn import Identity
    except ImportError:
        raise SearchError(
            "Context reranking requires the search-semantic extra.", 503
        ) from None
    try:
        return CrossEncoder(
            name,
            revision=revision,
            device="cpu",
            local_files_only=local_only,
            trust_remote_code=False,
            activation_fn=Identity(),
        )
    except Exception:
        raise SearchError(
            "The configured cross-encoder is unavailable locally.", 503
        ) from None


def _revision(model, configured: str) -> str:
    try:
        return str(model[0].auto_model.config._commit_hash or configured)
    except (AttributeError, TypeError, IndexError):
        try:
            return str(model.model.config._commit_hash or configured)
        except AttributeError:
            return configured


def normalized(vector) -> list[float]:
    result = [float(value) for value in vector]
    if not result or any(not math.isfinite(value) for value in result):
        raise SearchError("The embedding model returned an invalid vector.", 502)
    norm = math.sqrt(sum(value * value for value in result))
    if norm <= 0:
        raise SearchError("The embedding model returned a zero vector.", 502)
    return [value / norm for value in result]


def build_embeddings(service, payload: dict) -> dict:
    if not service.settings.semantic_enabled:
        raise SearchError("Enable semantic retrieval before building its local index.")
    source_id = payload.get("source_id")
    model = _text_model(
        service.settings.embedding_model,
        service.settings.embedding_revision,
        service.settings.local_models_only,
    )
    revision = _revision(model, service.settings.embedding_revision)
    with service.repo.connect() as connection:
        where, args = (" AND t.source_id=?", [source_id]) if source_id else ("", [])
        records = [
            dict(row, entity_type="chunk")
            for row in connection.execute(
                "SELECT t.id,t.source_id,t.text FROM transcript_chunks t JOIN captions c ON c.id=t.caption_id WHERE c.is_active=1"
                + where,
                args,
            )
        ]
        where, args = (" WHERE source_id=?", [source_id]) if source_id else ("", [])
        records += [
            dict(row, entity_type="ocr")
            for row in connection.execute(
                "SELECT id,source_id,text FROM ocr_blocks" + where, args
            )
        ]
        where, args = (" WHERE id=?", [source_id]) if source_id else ("", [])
        records += [
            {
                "id": row["id"],
                "source_id": row["id"],
                "text": " ".join(
                    (row["title"], row["description"], row["creator_name"])
                ),
                "entity_type": "source",
            }
            for row in connection.execute(
                "SELECT id,title,description,creator_name FROM sources" + where, args
            )
        ]
    count = 0
    for offset in range(0, len(records), 32):
        batch = [
            record for record in records[offset : offset + 32] if record["text"].strip()
        ]
        if not batch:
            continue
        vectors = model.encode(
            [record["text"] for record in batch],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        with service.repo.connect() as connection:
            for record, vector in zip(batch, vectors):
                values = normalized(vector)
                digest = hashlib.sha256(record["text"].encode()).hexdigest()
                connection.execute(
                    "INSERT OR IGNORE INTO embeddings VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("emb_"),
                        record["entity_type"],
                        record["id"],
                        record["source_id"],
                        "text",
                        service.settings.embedding_model,
                        revision,
                        len(values),
                        json_text(values),
                        digest,
                        now(),
                    ),
                )
                count += 1
    service.repo.event(
        "semantic_indexed",
        source_id=source_id,
        payload={
            "count": count,
            "model": service.settings.embedding_model,
            "revision": revision,
        },
    )
    return {
        "source_id": source_id,
        "count": count,
        "model": service.settings.embedding_model,
        "revision": revision,
    }


def _source_ids(service, filters: dict) -> set[str]:
    with service.repo.connect() as connection:
        rows = [
            decode(row)
            for row in connection.execute(
                "SELECT s.*,p.rights_status,p.expires_at AS policy_expires_at FROM sources s LEFT JOIN policies p ON p.id=(SELECT id FROM policies WHERE source_id=s.id ORDER BY version DESC LIMIT 1)"
            )
        ]
        collection_ids = None
        production_ids = {row[0] for row in connection.execute("SELECT source_id FROM case_assets WHERE json_extract(metadata_json,'$.role') IN ('production','narration','script','production_transcript') OR lower(category) IN ('production','05_production')")}
        if filters.get("collection_id"):
            collection_ids = {
                row[0]
                for row in connection.execute(
                    "SELECT source_id FROM collection_sources WHERE collection_id=?",
                    (filters["collection_id"],),
                )
            }
    source_filter = filters.get("source_ids")
    if filters.get("source_id"):
        source_filter = [filters["source_id"]]
    result = set()
    for row in rows:
        # The footage retriever owns only video candidates. Supporting case
        # assets have their own indexes and never consume its ranking budget.
        # Untyped legacy catalogue records retain their existing behavior.
        if (row.get("metadata") or {}).get("asset_kind", "video") != "video":
            continue
        if not filters.get("include_production") and row["id"] in production_ids:
            continue
        effective_rights = (
            "expired"
            if is_expired(row.get("policy_expires_at"))
            else (row.get("rights_status") or "unknown")
        )
        if not filters.get("rights_status") and effective_rights in {
            "blocked",
            "expired",
        }:
            continue
        if collection_ids is not None and row["id"] not in collection_ids:
            continue
        if source_filter is not None and row["id"] not in source_filter:
            continue
        if (
            filters.get("rights_status")
            and effective_rights != filters["rights_status"]
        ):
            continue
        if filters.get("language") and row.get("language") not in {
            None,
            "",
            filters["language"],
        }:
            continue
        if filters.get("creator_id") and row.get("creator_id") != filters["creator_id"]:
            continue
        if (
            filters.get("creator_name")
            and filters["creator_name"].casefold() not in row["creator_name"].casefold()
        ):
            continue
        if filters.get("platform") and row["platform"] != filters["platform"]:
            continue
        if filters.get("max_duration_ms") is not None and (
            row["duration_ms"] is None
            or row["duration_ms"] > filters["max_duration_ms"]
        ):
            continue
        if filters.get("min_duration_ms") is not None and (
            row["duration_ms"] is None
            or row["duration_ms"] < filters["min_duration_ms"]
        ):
            continue
        if filters.get("published_after") and (
            not row.get("published_at")
            or row["published_at"] < filters["published_after"]
        ):
            continue
        if filters.get("published_before") and (
            not row.get("published_at")
            or row["published_at"] > filters["published_before"]
        ):
            continue
        result.add(row["id"])
    return result


def _lexical(
    service,
    query: str,
    source_ids: set[str],
    limit: int = 100,
    language: str | None = None,
) -> list[dict]:
    expression = fts_query(query)
    if not expression or not source_ids:
        return []
    result = []
    with service.repo.connect() as connection:
        # Filter before top-k so unselected collections cannot crowd out results.
        placeholders = ",".join("?" for _ in source_ids)
        params = [expression, *sorted(source_ids)]
        lang_sql = " AND t.language=?" if language else ""
        for row in connection.execute(
            "SELECT t.*,c.kind AS caption_kind,bm25(transcript_chunks_fts) AS score FROM transcript_chunks_fts JOIN transcript_chunks t ON t.rowid=transcript_chunks_fts.rowid JOIN captions c ON c.id=t.caption_id WHERE transcript_chunks_fts MATCH ? AND c.is_active=1 AND t.source_id IN ("
            + placeholders
            + ")"
            + lang_sql
            + " ORDER BY score LIMIT ?",
            [*params, *([language] if language else []), limit],
        ):
            if (
                row["caption_kind"] != "visual_description"
                and len(re.findall(r"[^\W_]+", row["text"], flags=re.UNICODE)) < 3
            ):
                continue
            result.append(
                {
                    "entity_type": "chunk",
                    "entity_id": row["id"],
                    "source_id": row["source_id"],
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "evidence": row["text"],
                    "evidence_type": "visual_description"
                    if row["caption_kind"] == "visual_description"
                    else "transcript",
                    "lexical": -row["score"],
                }
            )
        for row in connection.execute(
            "SELECT o.*,bm25(ocr_fts) AS score FROM ocr_fts JOIN ocr_blocks o ON o.rowid=ocr_fts.rowid WHERE ocr_fts MATCH ? AND o.source_id IN ("
            + placeholders
            + ") ORDER BY score LIMIT ?",
            [*params, limit],
        ):
            result.append(
                {
                    "entity_type": "ocr",
                    "entity_id": row["id"],
                    "source_id": row["source_id"],
                    "start_ms": row["start_ms"],
                    "end_ms": row["end_ms"],
                    "evidence": row["text"],
                    "evidence_type": "ocr",
                    "lexical": -row["score"],
                }
            )
        for row in connection.execute(
            "SELECT s.*,bm25(footage_metadata_fts) AS score FROM footage_metadata_fts JOIN sources s ON s.rowid=footage_metadata_fts.rowid WHERE footage_metadata_fts MATCH ? AND s.id IN ("
            + placeholders
            + ") ORDER BY score LIMIT ?",
            [*params, limit],
        ):
            result.append(
                {
                    "entity_type": "source",
                    "entity_id": row["id"],
                    "source_id": row["id"],
                    "start_ms": None,
                    "end_ms": None,
                    "evidence": " — ".join(
                        item for item in (row["title"], row["description"]) if item
                    ),
                    "evidence_type": "metadata",
                    "lexical": -row["score"],
                }
            )
    return sorted(result, key=lambda item: item["lexical"], reverse=True)[:limit]


def _entity(service, entity_type: str, identifier: str) -> dict | None:
    table = {
        "chunk": "transcript_chunks",
        "ocr": "ocr_blocks",
        "source": "sources",
    }.get(entity_type)
    if table is None:
        return None
    entity = service.repo.get(table, identifier)
    if entity is None:
        return None
    if entity_type == "chunk":
        caption = service.repo.get("captions", entity["caption_id"])
        if caption is None or not caption["is_active"]:
            return None
        if (
            caption["kind"] != "visual_description"
            and len(re.findall(r"[^\W_]+", entity["text"], flags=re.UNICODE)) < 3
        ):
            return None
    text = (
        " — ".join(item for item in (entity["title"], entity["description"]) if item)
        if entity_type == "source"
        else entity["text"]
    )
    evidence_type = (
        "visual_description"
        if entity_type == "chunk" and caption["kind"] == "visual_description"
        else {"source": "metadata", "chunk": "transcript", "ocr": "ocr"}[entity_type]
    )
    return {
        "entity_type": entity_type,
        "entity_id": identifier,
        "source_id": identifier if entity_type == "source" else entity["source_id"],
        "start_ms": entity.get("start_ms"),
        "end_ms": entity.get("end_ms"),
        "evidence": text,
        "evidence_type": evidence_type,
    }


def _semantic(
    service,
    query: str,
    source_ids: set[str],
    language: str | None = None,
    limit: int = 100,
) -> tuple[list[dict], dict]:
    model = _text_model(
        service.settings.embedding_model,
        service.settings.embedding_revision,
        service.settings.local_models_only,
    )
    revision = _revision(model, service.settings.embedding_revision)
    query_vector = normalized(
        model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
    )
    with service.repo.connect() as connection:
        vectors = [
            decode(row)
            for row in connection.execute(
                "SELECT * FROM embeddings WHERE modality='text' AND model_name=? AND model_revision=?",
                (service.settings.embedding_model, revision),
            )
        ]
    scores = []
    for row in vectors:
        if row["source_id"] not in source_ids or row["dimensions"] != len(query_vector):
            continue
        entity = _entity(service, row["entity_type"], row["entity_id"])
        if entity is None:
            continue
        if (
            language
            and row["entity_type"] == "chunk"
            and service.repo.get("transcript_chunks", row["entity_id"])["language"]
            != language
        ):
            continue
        # Replaced text must never reuse an old vector merely because IDs match.
        text = entity["evidence"]
        if row["entity_type"] == "source":
            source = service.repo.get("sources", row["source_id"])
            text = " ".join(
                (source["title"], source["description"], source["creator_name"])
            )
        if hashlib.sha256(text.encode()).hexdigest() != row["input_hash"]:
            continue
        vector = normalized(row["vector"])
        entity["semantic"] = sum(a * b for a, b in zip(query_vector, vector))
        scores.append(entity)
    return sorted(scores, key=lambda item: item["semantic"], reverse=True)[:limit], {
        "embedding_model": service.settings.embedding_model,
        "embedding_revision": revision,
    }


def rank_fusion(rankings: list[list[dict]], k: int = 60) -> list[dict]:
    fused = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, 1):
            key = (item["entity_type"], item["entity_id"])
            if key not in fused:
                fused[key] = {**item, "scores": {}, "evidence_ids": [item["entity_id"]]}
            candidate = fused[key]
            candidate["scores"]["rrf"] = candidate["scores"].get("rrf", 0.0) + 1 / (
                k + rank
            )
            for name in ("lexical", "semantic", "visual"):
                if name in item:
                    candidate["scores"][name] = item[name]
    return sorted(fused.values(), key=lambda item: item["scores"]["rrf"], reverse=True)


def rerank(service, query: str, records: list[dict]) -> tuple[list[dict], dict]:
    model = _cross_model(
        service.settings.reranker_model,
        service.settings.reranker_revision,
        service.settings.local_models_only,
    )
    version = _revision(model, service.settings.reranker_revision)
    values = model.predict(
        [(query, item["evidence"]) for item in records], show_progress_bar=False
    )
    for item, value in zip(records, values):
        value = float(value)
        if not math.isfinite(value):
            raise SearchError("The reranker returned an invalid score.", 502)
        item["scores"]["reranker"] = 1 / (1 + math.exp(-max(-50, min(50, value))))
    return sorted(records, key=lambda item: item["scores"]["reranker"], reverse=True), {
        "reranker_model": service.settings.reranker_model,
        "reranker_revision": version,
    }


def consolidate(records: list[dict], max_duration_ms: int) -> list[dict]:
    output = []
    for item in records:
        previous = next(
            (
                old
                for old in output
                if old["source_id"] == item["source_id"]
                and old["evidence_type"] == item["evidence_type"]
                and old["start_ms"] is not None
                and item["start_ms"] is not None
                and item["start_ms"] <= old["end_ms"] + 5000
                and old["start_ms"] <= item["end_ms"] + 5000
                and max(old["end_ms"], item["end_ms"])
                - min(old["start_ms"], item["start_ms"])
                <= max_duration_ms
            ),
            None,
        )
        if previous is None:
            output.append({**item})
        else:
            if item["evidence"] not in previous["evidence"]:
                previous["evidence"] = (
                    " ".join((item["evidence"], previous["evidence"]))
                    if item["start_ms"] < previous["start_ms"]
                    else " ".join((previous["evidence"], item["evidence"]))
                )
            previous["start_ms"] = min(previous["start_ms"], item["start_ms"])
            previous["end_ms"] = max(previous["end_ms"], item["end_ms"])
            previous["evidence_ids"] = list(
                dict.fromkeys(previous["evidence_ids"] + item["evidence_ids"])
            )
    return output


def retrieve(service, query: str, filters: dict, top_k: int) -> tuple[list[dict], dict]:
    source_ids = _source_ids(service, filters)
    versions = {
        "lexical": "sqlite-fts5-unicode61",
        "fusion": "rrf-k60",
        "pipeline": service.settings.pipeline_version,
    }
    lexical = _lexical(service, query, source_ids, language=filters.get("language"))
    rankings = [lexical]
    if service.settings.semantic_enabled and source_ids:
        semantic, model_versions = _semantic(
            service, query, source_ids, filters.get("language")
        )
        rankings.append(semantic)
        versions.update(model_versions)
    if service.settings.visual_enabled and source_ids:
        from .visual import visual_search

        visual = visual_search(service, query, source_ids, limit=100)
        rankings.append(
            [
                {**item, "entity_type": "visual", "visual": item["score"]}
                for item in visual
            ]
        )
    records = rank_fusion(rankings)
    if service.settings.rerank_enabled and records:
        records, model_versions = rerank(service, query, records[:50])
        versions.update(model_versions)
    return consolidate(records, service.settings.max_clip_duration_ms)[:top_k], versions
