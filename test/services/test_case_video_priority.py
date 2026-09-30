"""Supporting case indexes cannot consume or alter footage metadata ranking."""

from dataclasses import replace
import json
from pathlib import Path

from app.services.targeted_search.service import SearchService


def _service(tmp_path):
    service = SearchService(tmp_path)
    service.settings = replace(
        service.settings,
        enabled=True,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
    )
    service.repo.settings = service.settings
    return service


def _signature(results):
    return [
        (
            row["source_id"],
            row["start_ms"],
            row["end_ms"],
            row["evidence_type"],
            row["scores"],
        )
        for row in results
    ]


def test_supporting_assets_do_not_change_footage_top_k_or_scores(tmp_path):
    service = _service(tmp_path)
    for index, description in enumerate(
        [
            "Courthouse exterior establishing shot",
            "Courthouse exterior wide shot at sunset",
            "Court hearing inside the courthouse",
        ]
    ):
        service.discover(
            f"local://footage{index}",
            metadata={
                "title": description,
                "description": description,
                "asset_kind": "video",
            },
        )
    before = _signature(service.search("courthouse exterior", top_k=2)["results"])
    assert before
    for index in range(120):
        service.discover(
            f"local://document{index}",
            metadata={
                "title": "Courthouse exterior courthouse exterior",
                "description": "Courthouse exterior establishing shot",
                "asset_kind": "document",
            },
        )
    after = _signature(service.search("courthouse exterior", top_k=2)["results"])
    assert after == before
    assert (
        service.search("courthouse exterior", {"source_ids": ["local:document0"]})[
            "results"
        ]
        == []
    )


def test_metadata_updates_keep_footage_index_consistent(tmp_path):
    service = _service(tmp_path)
    source = service.discover(
        "local://changing",
        metadata={"title": "Courthouse exterior", "asset_kind": "video"},
    )
    assert service.search("courthouse exterior")["results"]
    service.discover(
        "local://changing",
        metadata={"title": "Courthouse exterior", "asset_kind": "document"},
    )
    assert service.search("courthouse exterior")["results"] == []
    service.discover(
        "local://changing",
        metadata={"title": "Courthouse entrance", "asset_kind": "video"},
    )
    assert (
        service.search("courthouse entrance")["results"][0]["source_id"] == source["id"]
    )
    with service.repo.connect() as connection:
        connection.execute(
            "INSERT INTO footage_metadata_fts(footage_metadata_fts,rank) VALUES('integrity-check',0)"
        )


def test_existing_fifty_query_footage_benchmark_survives_case_documents(tmp_path):
    from app.services.targeted_search.evaluation import evaluate

    service = _service(tmp_path)
    gold = json.loads(
        (
            Path(__file__).parents[1] / "fixtures" / "targeted_search_gold.json"
        ).read_text()
    )
    for fixture in gold["fixtures"]:
        service.discover(fixture["url"], metadata=fixture["metadata"])
    before = evaluate(service, gold)
    for index in range(80):
        service.discover(
            f"local://case-record{index}",
            metadata={
                "asset_kind": "document",
                "title": "North Dakota drilling court fractions evidence",
                "description": "Courtroom prosecutor defense Bakken pumpjack fractions bread rescue verdict sentence.",
            },
        )
    after = evaluate(service, gold)
    assert after == before
    assert after["query_count"] == 50
