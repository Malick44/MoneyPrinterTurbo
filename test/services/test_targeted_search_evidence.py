import json

import pytest

from app.models.search import SearchError
from app.services.targeted_search.evaluation import evaluate, range_iou
from app.services.targeted_search.kurator import ArchivePage, caption_allowed, ingest_source
from app.services.targeted_search.llm_evidence import check_decision
from app.services.targeted_search.rate_limit import RequestBudget
from app.services.targeted_search.service import SearchService


def decision(**kwargs):
    return json.dumps({"decision": "approve", "relevance": 0.9, "primary_topic": "oil fields",
                       "evidence": [{"start_ms": 1000, "end_ms": 2000, "quote": "Bakken oil fields"}],
                       "reason": "Evidence directly supports this topic", **kwargs})


def test_llm_evidence_cannot_grant_rights_or_fabricate_quotes_and_ranges():
    cues = [{"start_ms": 1000, "end_ms": 2000, "text": "Bakken oil fields"}]
    assert check_decision(decision(), cues)["decision"] == "approve"
    invalid = [decision(rights_status="allowed_export"), decision(relevance=0.4),
               decision(evidence=[]), decision(evidence=[{"start_ms": 0, "end_ms": 2000, "quote": "Bakken oil fields"}]),
               decision(evidence=[{"start_ms": 1000, "end_ms": 2000, "quote": "A court conviction"}])]
    for raw in invalid:
        with pytest.raises(SearchError):
            check_decision(raw, cues)


def test_archive_delivery_host_and_video_identity_are_pinned():
    assert caption_allowed("https://ddk426d1rikbf.cloudfront.net/transcription_files/123.vtt?Signature=temporary", "123", "Audio")
    for url in ["https://other.example/transcription_files/123.vtt", "http://ddk426d1rikbf.cloudfront.net/transcription_files/123.vtt",
                "https://ddk426d1rikbf.cloudfront.net/transcription_files/456.vtt", "https://user@ddk426d1rikbf.cloudfront.net/transcription_files/123.vtt"]:
        assert not caption_allowed(url, "123", "Audio")
    page = ArchivePage()
    page.feed('<script type="application/ld+json">{"@type":"VideoObject","name":"Oil field"}</script>'
              '<track label="Audio" src="https://example.com/caption.vtt?x=1&amp;y=2">')
    assert page.video["name"] == "Oil field"
    assert page.tracks[0]["src"].endswith("x=1&y=2")


def test_archive_discovery_retains_description_provenance_and_bounds_known_duration(tmp_path, monkeypatch):
    from app.config import config
    from app.services.targeted_search import kurator

    monkeypatch.setitem(config.app, "targeted_search_semantic_enabled", False)
    service = SearchService(tmp_path)
    source = service.discover("https://tegna.kurator.com/video/detail/123")
    page = ('<script type="application/ld+json">{"@type":"VideoObject","name":"KREM archive","description":"Case footage"}</script>'
            '<script>{"title":"Duration","value":"00:00:05"}</script>'
            '<track label="Description" srclang="en" src="https://ddk426d1rikbf.cloudfront.net/video_transcription_files/123_42.vtt?Signature=DO_NOT_PERSIST">')
    monkeypatch.setattr(kurator, "_read", lambda url, settings, maximum: page if "kurator.com" in url else "WEBVTT\n\n00:00.000 --> 00:08.000\nOil rig beside a road\n")
    result = ingest_source(service, {"source_id": source["id"]})
    assert result["media_downloaded"] is False
    matches = service.search("Oil rig")["results"]
    assert matches[0]["evidence_type"] == "visual_description"
    assert matches[0]["start_ms"] == 0 and matches[0]["end_ms"] == 5000
    with service.repo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        snapshots = connection.execute("SELECT raw_json FROM metadata_snapshots").fetchall()
    assert "DO_NOT_PERSIST" not in str(snapshots)


def test_budget_refills_and_does_not_use_wall_clock():
    timestamp = [0.0]
    budget = RequestBudget(2, clock=lambda: timestamp[0])
    assert budget.allow("client") and budget.allow("client")
    assert not budget.allow("client")
    timestamp[0] = 30.0
    assert budget.allow("client")
    assert not budget.allow("client")


def test_gold_metrics_count_duplicate_hits_once_and_require_time_evidence():
    class Library:
        def search(self, *args):
            return {"results": [{"source_id": "s1", "start_ms": None, "end_ms": None},
                                {"source_id": "s1", "start_ms": 1000, "end_ms": 2000},
                                {"source_id": "s1", "start_ms": 1000, "end_ms": 2000}]}
    label = {"source_id": "s1", "start_ms": 1000, "end_ms": 2000}
    result = evaluate(Library(), [{"query": "case", "relevant": [label]}], 3)
    assert result["metrics"]["recall"] == 1
    assert result["metrics"]["precision"] == pytest.approx(1 / 3)
    assert result["metrics"]["reciprocal_rank"] == 0.5
    assert range_iou({"start_ms": 1500, "end_ms": 2500}, label) == pytest.approx(1 / 3)
