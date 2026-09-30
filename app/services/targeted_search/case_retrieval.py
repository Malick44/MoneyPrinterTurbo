"""Supporting evidence retrieval with independent budgets for each media kind."""

from __future__ import annotations

import hashlib

from app.models.search import SearchError
from .policy import authorize
from .repository import decode, json_text, new_id, now
from .retrieval import _revision, _text_model, fts_query, normalized, rerank


def _allowed(workspace, case_id, filters):
    allowed = {}
    for asset in workspace.list_assets(case_id):
        if asset["asset_kind"] == "video" and not filters.get("include_video"):
            continue
        if not filters.get("include_production") and workspace._is_production(asset):
            continue
        if filters.get("asset_id") and filters["asset_id"] != asset["id"]:
            continue
        if any(
            filters.get(name) and asset.get(name) != filters[name]
            for name in ("asset_kind", "category", "rights_status")
        ):
            continue
        if (
            filters.get("asset_kinds")
            and asset["asset_kind"] not in filters["asset_kinds"]
        ):
            continue
        if filters.get("source_id") and asset["source_id"] != filters["source_id"]:
            continue
        if filters.get("state") and asset["state"] != filters["state"]:
            continue
        metadata = asset.get("metadata") or {}
        if filters.get("tags") and not set(filters["tags"]).issubset(
            set(metadata.get("tags", []))
        ):
            continue
        published = (
            metadata.get("event_date")
            or metadata.get("published_at")
            or metadata.get("recorded_at")
        )
        if filters.get("date_from") and (
            not published or str(published) < filters["date_from"]
        ):
            continue
        if filters.get("date_to") and (
            not published or str(published) > filters["date_to"]
        ):
            continue
        try:
            authorize(workspace.repo, asset["source_id"], "analysis")
        except SearchError:
            continue
        allowed[asset["id"]] = asset
    return allowed


def build_supporting_embeddings(workspace, asset_id):
    asset = workspace.get_asset(asset_id)
    authorize(workspace.repo, asset["source_id"], "analysis")
    if not workspace.settings.semantic_enabled or asset["asset_kind"] == "video":
        return {"count": 0, "enabled": workspace.settings.semantic_enabled}
    workspace.asset_path(asset_id)
    with workspace.repo.connect() as connection:
        units = [
            decode(row)
            for row in connection.execute(
                "SELECT * FROM evidence_units WHERE asset_id=? AND asset_version_id=? AND is_active=1 AND unit_kind!='transcript_word'",
                (asset_id, asset["asset_version_id"]),
            )
        ]
    if not units:
        return {"count": 0}
    model = _text_model(
        workspace.settings.embedding_model,
        workspace.settings.embedding_revision,
        workspace.settings.local_models_only,
    )
    revision = _revision(model, workspace.settings.embedding_revision)
    count = 0
    for start in range(0, len(units), 32):
        batch = units[start : start + 32]
        vectors = model.encode(
            [unit["text"] for unit in batch],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        current = workspace.get_asset(asset_id)
        if current["asset_version_id"] != asset["asset_version_id"]:
            raise SearchError("Evidence version changed while indexing", 409)
        authorize(workspace.repo, asset["source_id"], "analysis")
        with workspace.repo.connect() as connection:
            for unit, vector in zip(batch, vectors):
                values = normalized(vector)
                connection.execute(
                    "INSERT OR IGNORE INTO embeddings(id,entity_type,entity_id,source_id,modality,model_name,model_revision,dimensions,vector_json,input_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        new_id("caseemb_"),
                        "case_evidence_unit",
                        unit["id"],
                        asset["source_id"],
                        "text",
                        workspace.settings.embedding_model,
                        revision,
                        len(values),
                        json_text(values),
                        hashlib.sha256(unit["text"].encode()).hexdigest(),
                        now(),
                    ),
                )
                count += 1
    return {
        "count": count,
        "model": workspace.settings.embedding_model,
        "revision": revision,
    }


def retrieve_supporting(workspace, case_id, query, filters=None, top_k=20):
    workspace.get_case(case_id)
    if (
        not isinstance(query, str)
        or not query.strip()
        or len(query) > 2000
        or isinstance(top_k, bool)
        or not isinstance(top_k, int)
        or not 1 <= top_k <= 100
    ):
        raise SearchError(
            "Search needs a bounded query and top_k between one and 100", 422
        )
    filters = filters or {}
    if not isinstance(filters, dict):
        raise SearchError("Case search filters must be an object", 422)
    allowed = _allowed(workspace, case_id, filters)
    versions = {
        "lexical": "sqlite-fts5-unicode61",
        "ranking": "per-media-kind-rrf-1",
        "pipeline": "case-evidence-1",
    }
    if not allowed:
        return {
            "case_id": case_id,
            "query": query,
            "results": [],
            "groups": {},
            "model_versions": versions,
        }
    placeholders = ",".join("?" for _ in allowed)
    with workspace.repo.connect() as connection:
        units = {
            row["id"]: row
            for raw in connection.execute(
                "SELECT * FROM evidence_units WHERE case_id=? AND asset_id IN ("
                + placeholders
                + ") AND is_active=1",
                (case_id, *allowed),
            )
            if (row := decode(raw))["asset_version_id"]
            == allowed[row["asset_id"]]["asset_version_id"]
            and (
                filters.get("include_production")
                or row.get("metadata", {}).get("scope")
                not in {"narration", "production"}
            )
            and (not filters.get("origin") or row["origin"] == filters["origin"])
        }
        rankings = []
        expression = fts_query(query)
        if expression:
            # Scope all eligibility before limiting, separately for each kind.
            for kind in sorted({asset["asset_kind"] for asset in allowed.values()}):
                ids = [
                    asset["id"]
                    for asset in allowed.values()
                    if asset["asset_kind"] == kind
                ]
                scope_sql = (
                    ""
                    if filters.get("include_production")
                    else " AND coalesce(json_extract(u.metadata_json,'$.scope'),'') NOT IN ('narration','production')"
                )
                origin_sql = " AND u.origin=?" if filters.get("origin") else ""
                rows = connection.execute(
                    "SELECT u.id,bm25(evidence_units_fts) AS score FROM evidence_units_fts JOIN evidence_units u ON u.rowid=evidence_units_fts.rowid JOIN case_assets a ON a.id=u.asset_id JOIN case_asset_versions v ON v.id=u.asset_version_id AND v.version=a.version WHERE evidence_units_fts MATCH ? AND u.case_id=? AND u.is_active=1 AND u.asset_id IN ("
                    + ",".join("?" for _ in ids)
                    + ")"
                    + scope_sql
                    + origin_sql
                    + " ORDER BY score LIMIT 500",
                    (
                        expression,
                        case_id,
                        *ids,
                        *([filters["origin"]] if filters.get("origin") else []),
                    ),
                ).fetchall()
                rankings.append(
                    (
                        kind,
                        "lexical",
                        [
                            (row["id"], -row["score"])
                            for row in rows
                            if row["id"] in units
                        ],
                    )
                )
        vector_rows = [
            decode(row)
            for row in connection.execute(
                "SELECT * FROM embeddings WHERE entity_type IN ('case_evidence_unit','case_asset_version') AND source_id IN ("
                + ",".join(
                    "?" for _ in {asset["source_id"] for asset in allowed.values()}
                )
                + ")",
                tuple({asset["source_id"] for asset in allowed.values()}),
            )
        ]
    text_rows = [
        row
        for row in vector_rows
        if row["entity_type"] == "case_evidence_unit"
        and row["entity_id"] in units
        and row["model_name"] == workspace.settings.embedding_model
    ]
    if workspace.settings.semantic_enabled and text_rows:
        model = _text_model(
            workspace.settings.embedding_model,
            workspace.settings.embedding_revision,
            workspace.settings.local_models_only,
        )
        revision = _revision(model, workspace.settings.embedding_revision)
        query_vector = normalized(
            model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        )
        by_kind = {}
        for row in text_rows:
            unit = units[row["entity_id"]]
            if (
                row["model_revision"] != revision
                or row["dimensions"] != len(query_vector)
                or row["input_hash"]
                != hashlib.sha256(unit["text"].encode()).hexdigest()
            ):
                continue
            score = sum(a * b for a, b in zip(query_vector, normalized(row["vector"])))
            by_kind.setdefault(allowed[unit["asset_id"]]["asset_kind"], []).append(
                (unit["id"], score)
            )
        rankings += [
            (kind, "semantic", sorted(rows, key=lambda row: row[1], reverse=True)[:100])
            for kind, rows in by_kind.items()
        ]
        versions.update(
            embedding_model=workspace.settings.embedding_model,
            embedding_revision=revision,
        )
    current_images = {asset["asset_version_id"]: asset for asset in allowed.values()}
    image_rows = [
        row
        for row in vector_rows
        if row["entity_type"] == "case_asset_version"
        and row["entity_id"] in current_images
        and row["input_hash"] == current_images[row["entity_id"]]["sha256"]
    ]
    if workspace.settings.visual_enabled and image_rows:
        import torch
        from .visual import _vision_model

        model, _, tokenizer, name, revision = _vision_model(workspace)
        with torch.no_grad():
            query_vector = normalized(model.encode_text(tokenizer([query]))[0].tolist())
        by_version = {asset["asset_version_id"]: asset for asset in allowed.values()}
        visual_hits = {}
        for row in image_rows:
            asset = by_version.get(row["entity_id"])
            if (
                not asset
                or row["model_name"] != name
                or row["model_revision"] != revision
                or row["input_hash"] != asset["sha256"]
                or row["dimensions"] != len(query_vector)
            ):
                continue
            key = "visual:" + asset["id"]
            units[key] = {
                "id": key,
                "asset_id": asset["id"],
                "asset_version_id": asset["asset_version_id"],
                "source_id": asset["source_id"],
                "locator": {"kind": "image"},
                "text": "Visual similarity to the query",
                "unit_kind": "visual",
                "origin": "visual_similarity",
                "confidence": None,
                "metadata": {"identity_verified": False},
                "content_hash": asset["sha256"],
            }
            score = sum(a * b for a, b in zip(query_vector, normalized(row["vector"])))
            visual_hits.setdefault(asset["asset_kind"], []).append((key, score))
        rankings += [
            (kind, "visual", sorted(rows, key=lambda row: row[1], reverse=True)[:100])
            for kind, rows in visual_hits.items()
        ]
        versions.update(vision_model=name, vision_revision=revision)
    grouped = {}
    for kind, modality, rows in rankings:
        group = grouped.setdefault(kind, {})
        for rank, (identifier, score) in enumerate(rows, 1):
            if identifier not in units:
                continue
            hit = group.setdefault(
                identifier, {"unit": units[identifier], "scores": {"rrf": 0.0}}
            )
            hit["scores"][modality] = score
            hit["scores"]["rrf"] += 1 / (60 + rank)
    groups = {}
    for kind, hits in grouped.items():
        records = []
        for hit in sorted(
            hits.values(), key=lambda hit: hit["scores"]["rrf"], reverse=True
        )[:100]:
            unit = hit["unit"]
            asset = allowed[unit["asset_id"]]
            records.append(
                {
                    **unit,
                    "unit_id": None if unit["unit_kind"] == "visual" else unit["id"],
                    "filename": asset["filename"],
                    "asset_kind": kind,
                    "category": asset["category"],
                    "evidence": unit["text"],
                    "evidence_type": unit["unit_kind"],
                    "scores": hit["scores"],
                    "rights_status": asset["rights_status"],
                    "assertion_status": "visual_match"
                    if unit["unit_kind"] == "visual"
                    else "source_assertion",
                }
            )
        if workspace.settings.rerank_enabled and records:
            records, rerank_versions = rerank(workspace, query, records[:50])
            versions.update(rerank_versions)
        current_assets = {}
        eligible = []
        for record in records[:top_k]:
            identifier = record["asset_id"]
            if identifier not in current_assets:
                try:
                    asset = workspace.get_asset(identifier)
                    authorize(workspace.repo, asset["source_id"], "analysis")
                    old = allowed[identifier]
                    current_assets[identifier] = (
                        asset
                        if asset["asset_version_id"] == old["asset_version_id"]
                        and asset["sha256"] == old["sha256"]
                        and (
                            filters.get("include_production")
                            or not workspace._is_production(asset)
                        )
                        else None
                    )
                except SearchError:
                    current_assets[identifier] = None
            asset = current_assets[identifier]
            if not asset:
                continue
            if record["unit_id"]:
                unit = workspace.repo.get("evidence_units", record["unit_id"])
                if (
                    not unit
                    or not unit["is_active"]
                    or unit["content_hash"] != record["content_hash"]
                ):
                    continue
            record["rights_status"] = asset["rights_status"]
            eligible.append(record)
        if eligible:
            groups[kind] = eligible
    return {
        "case_id": case_id,
        "query": query,
        "results": [row for rows in groups.values() for row in rows],
        "groups": groups,
        "model_versions": versions,
        "top_k_per_group": top_k,
    }
