"""Offline retrieval metrics from human-provided source and range labels."""

from __future__ import annotations

import math

from app.models.search import SearchError


def range_iou(actual: dict, expected: dict) -> float:
    if expected.get("start_ms") is None:
        return 1.0
    if actual.get("start_ms") is None:
        return 0.0
    start = max(actual["start_ms"], expected["start_ms"])
    end = min(actual["end_ms"], expected["end_ms"])
    union = max(actual["end_ms"], expected["end_ms"]) - min(actual["start_ms"], expected["start_ms"])
    return max(0, end - start) / union if union else 0.0


def evaluate(service, data: dict | list, top_k: int = 10) -> dict:
    queries = data.get("queries", []) if isinstance(data, dict) else data
    if not queries:
        raise SearchError("The gold dataset must contain labeled queries.")
    rows = []
    for item in queries:
        expected = item.get("relevant", [])
        if not expected:
            raise SearchError("Each gold query needs at least one explicit source/range label.")
        results = service.search(item["query"], item.get("filters", {}), top_k)["results"]
        hits, overlaps, matched = [], [], set()
        for result in results:
            matches = [(i, range_iou(result, label)) for i, label in enumerate(expected) if label["source_id"] == result["source_id"]]
            matches = [(i, overlap) for i, overlap in matches if overlap >= item.get("minimum_iou", 0.1)]
            fresh = [(i, overlap) for i, overlap in matches if i not in matched]
            hits.append(bool(fresh))
            overlaps.append(max((overlap for _, overlap in matches), default=0))
            matched.update(i for i, _ in fresh)
        first = next((i + 1 for i, value in enumerate(hits) if value), None)
        dcg = sum(1 / math.log2(i + 2) for i, value in enumerate(hits) if value)
        ideal = sum(1 / math.log2(i + 2) for i in range(min(len(expected), top_k)))
        rows.append({"query": item["query"], "recall": len(matched) / len(expected),
                     "precision": sum(hits) / top_k, "reciprocal_rank": 1 / first if first else 0,
                     "ndcg": dcg / ideal, "best_range_iou": max(overlaps, default=0),
                     "retrieved": len(results)})
    metrics = {key: sum(row[key] for row in rows) / len(rows) for key in ["recall", "precision", "reciprocal_rank", "ndcg", "best_range_iou"]}
    return {"query_count": len(rows), "top_k": top_k, "metrics": metrics, "queries": rows,
            "note": "Human source/range labels define relevance; these metrics do not grant download permission."}
