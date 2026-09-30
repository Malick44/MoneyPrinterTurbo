"""Offline contracts for caption-first search, policy scopes and durable jobs."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.models.search import SearchError
from app.services.targeted_search import Repository, SearchService
from app.services.targeted_search.captions import ingest_captions, parse_captions
from app.services.targeted_search.discovery import ingest_source
from app.services.targeted_search.policy import authorize
from app.services.targeted_search.retrieval import build_embeddings


@pytest.fixture
def service(tmp_path):
    result = SearchService(tmp_path)
    result.settings = replace(
        result.settings,
        enabled=True,
        semantic_enabled=False,
        rerank_enabled=False,
        visual_enabled=False,
        ocr_enabled=False,
    )
    result.repo.settings = result.settings
    return result


def fixture_source(service, identifier="lesson", **extra):
    return service.discover(
        f"local://{identifier}",
        metadata={
            "title": "Fraction demonstration",
            "duration_ms": 60000,
            "captions": [
                {
                    "start_ms": 12000,
                    "end_ms": 16000,
                    "text": "Subtract fractions with unlike denominators using the least common denominator.",
                },
                {
                    "start_ms": 16000,
                    "end_ms": 20000,
                    "text": "Rewrite both fractions and subtract the numerators.",
                },
            ],
            **extra,
        },
    )


def candidate(service, query="subtract fractions"):
    return next(
        item
        for item in service.search(query)["results"]
        if item["evidence_type"] == "transcript"
    )


def permit(service, source_id, uses="internal_review", status="allowed_internal"):
    return service.set_policy(
        source_id,
        status,
        uses,
        "Owned fixture with explicit permitted use",
        "test-reviewer",
    )


def test_discovery_preserves_cues_and_never_downloads(service):
    source = fixture_source(service)
    again = fixture_source(service)
    assert again["id"] == source["id"]
    assert again["chunk_count"] == 1
    assert source["caption_count"] == 1
    assert service.list_artifacts() == []
    result = candidate(service)
    assert (result["start_ms"], result["end_ms"]) == (12000, 20000)
    assert "least common denominator" in result["evidence"]
    with service.repo.connect() as connection:
        assert (
            connection.execute("SELECT count(*) FROM metadata_snapshots").fetchone()[0]
            == 1
        )
        assert (
            connection.execute("SELECT count(*) FROM caption_cues").fetchone()[0] == 2
        )
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_researched_metadata_has_no_invented_timestamps(service):
    source = service.register_metadata(
        "https://www.justice.gov/usao/example",
        "Oil patch murder investigation",
        "A contextual article, not timestamped video evidence.",
    )
    result = service.search("oil patch murder")["results"][0]
    assert result["evidence_type"] == "metadata"
    assert result["start_ms"] is None and result["end_ms"] is None
    assert source["duration_ms"] is None
    assert service.list_jobs() == []
    permit(service, source["id"])
    with pytest.raises(SearchError, match="measured duration"):
        service.approve_download(result["id"], start_ms=0, end_ms=5000)


@pytest.mark.parametrize(
    "url",
    [
        "http://youtube.com/watch?v=abcdefghi",
        "https://user:pass@youtube.com/watch?v=abcdefghi",
        "https://127.0.0.1/foo",
        "https://localhost/foo",
        "https://youtube.com.evil.example/watch?v=abcdefghi",
        "https://youtube.com/playlist?list=abcdefghi",
    ],
)
def test_untrusted_urls_cannot_enter_discovery(service, url):
    with pytest.raises(SearchError):
        service.discover(url)
    assert service.list_jobs() == []


def test_registration_and_reference_urls_do_not_accept_private_or_signed_urls(service):
    with pytest.raises(SearchError, match="Private network"):
        service.register_metadata("https://127.0.0.1/source", "Private", "No request")
    source = service.register_metadata(
        "https://www.justice.gov/public",
        "Public contextual article",
        "No download",
        metadata={
            "reference_urls": [
                "https://www.justice.gov/other?token=SECRET",
                "https://user:password@example.org/article",
                "https://127.0.0.1/secrets",
                "local://private",
            ],
            "local_path": "/outside/private.mp4",
        },
    )
    assert source["metadata"]["reference_urls"] == ["https://www.justice.gov/other"]
    assert "local_path" not in source["metadata"]


def test_metadata_only_local_reference_cannot_set_authoritative_media_path(service):
    source = service.register_metadata(
        "local://unimported",
        "Unimported metadata",
        "No file read",
        metadata={"local_path": "/outside/private.mp4"},
    )
    assert service.repo.get("sources", source["id"])["local_path"] is None


def test_youtube_identity_is_canonical_and_query_safe(service):
    first = service.discover("https://youtu.be/Abcdefghi12?utm_source=x")
    second = service.discover("https://www.youtube.com/watch?v=Abcdefghi12&t=62")
    assert first["id"] == second["id"]
    assert first["canonical_url"] == "https://www.youtube.com/watch?v=Abcdefghi12"
    assert len(service.list_jobs()) == 1
    other = service.discover("https://youtube.com/shorts/Zyxwvutsr98")
    assert other["id"] != first["id"]


def test_caption_formats_preserve_precise_times():
    vtt = "WEBVTT\n\n00:07:42.125 --> 00:07:43.875 align:start\n<c>Least common denominator</c>\n"
    assert parse_captions(vtt) == [
        {"start_ms": 462125, "end_ms": 463875, "text": "Least common denominator"}
    ]
    srt = "1\n00:00:01,200 --> 00:00:02,350\nA &amp; B\n"
    assert parse_captions(srt, "srt")[0] == {
        "start_ms": 1200,
        "end_ms": 2350,
        "text": "A & B",
    }
    json3 = {
        "events": [
            {
                "tStartMs": 321,
                "dDurationMs": 1234,
                "segs": [{"utf8": "first"}, {"utf8": " second"}],
            }
        ]
    }
    assert parse_captions(json3)[0]["end_ms"] == 1555
    assert (
        parse_captions('[{"start_ms":1200,"end_ms":2350,"text":"A & B"}]', "json")[0][
            "start_ms"
        ]
        == 1200
    )


def test_caption_changes_retire_old_search_evidence(service):
    source = fixture_source(service)
    ingest_captions(
        service.repo,
        source["id"],
        [{"start_ms": 24000, "end_ms": 28000, "text": "Multiply the values instead."}],
    )
    assert not any(
        item["evidence_type"] == "transcript"
        for item in service.search("subtract")["results"]
    )
    assert any(
        item["start_ms"] == 24000 for item in service.search("multiply")["results"]
    )
    with service.repo.connect() as connection:
        assert connection.execute("SELECT count(*) FROM captions").fetchone()[0] == 2


def test_caption_restoration_reactivates_immutable_original_version(service):
    source = fixture_source(service)
    first = [
        {
            "start_ms": 12000,
            "end_ms": 16000,
            "text": "First authentic version of captions.",
        }
    ]
    second = [
        {
            "start_ms": 12000,
            "end_ms": 16000,
            "text": "Second replacement version of captions.",
        }
    ]
    original = ingest_captions(service.repo, source["id"], first)
    ingest_captions(service.repo, source["id"], second)
    restored = ingest_captions(service.repo, source["id"], first)
    assert restored["id"] == original["id"] and restored["is_active"]
    assert any(
        item["evidence_type"] == "transcript"
        for item in service.search("authentic")["results"]
    )
    assert not any(
        item["evidence_type"] == "transcript"
        for item in service.search("replacement")["results"]
    )


def test_provider_cue_tails_are_trimmed_without_inflating_source_duration(service):
    source = fixture_source(service)
    ingest_captions(
        service.repo,
        source["id"],
        [
            {"start_ms": 59000, "end_ms": 63000, "text": "Final valid source cue"},
            {"start_ms": 61000, "end_ms": 63000, "text": "Outside source"},
        ],
        kind="automatic",
    )
    result = service.search("final valid")["results"][0]
    assert result["end_ms"] == 60000
    assert service.get_source(source["id"])["duration_ms"] == 60000
    with service.repo.connect() as connection:
        event = connection.execute(
            "SELECT payload_json FROM events WHERE event_type='captions_indexed' ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    assert '"cues_trimmed_to_source":1' in event
    assert '"cues_outside_source_skipped":1' in event


def test_fts_update_delete_stays_synchronized(service):
    source = fixture_source(service)
    with service.repo.connect() as connection:
        chunk_id = connection.execute(
            "SELECT id FROM transcript_chunks WHERE source_id=?", (source["id"],)
        ).fetchone()[0]
        connection.execute(
            "UPDATE transcript_chunks SET text='Unique synchronized edit' WHERE id=?",
            (chunk_id,),
        )
    assert any(
        item["evidence_type"] == "transcript"
        for item in service.search("synchronized")["results"]
    )
    with service.repo.connect() as connection:
        connection.execute("DELETE FROM transcript_chunks WHERE id=?", (chunk_id,))
    assert not service.search("synchronized")["results"]


def test_collection_filter_is_applied_before_results(service):
    one = service.add_collection("Math", "fractions", ["subtract fractions"])
    two = service.add_collection("Elsewhere", "other")
    source = fixture_source(service)
    service.discover(
        "local://lesson",
        collection_id=one["id"],
        metadata={"title": "Fraction demonstration", "duration_ms": 60000},
    )
    fixture_source(service, "other")
    service.discover(
        "local://other",
        collection_id=two["id"],
        metadata={"title": "Fraction demonstration", "duration_ms": 60000},
    )
    result = service.search("fractions", {"collection_id": one["id"]})
    assert {item["source_id"] for item in result["results"]} == {source["id"]}
    assert service.list_collections()[0]["source_count"] == 1


def test_policy_cannot_be_bypassed_by_validator_or_clip_job(service):
    fixture_source(service)
    result = candidate(service)
    assert service.validate(result["id"])["decision"] == "review"
    with pytest.raises(SearchError):
        service.approve_download(result["id"])
    with pytest.raises(SearchError):
        service.enqueue_clip(result["id"])
    assert service.list_jobs() == []


def test_export_needs_explicit_export_rights_and_intent(service):
    source = fixture_source(service)
    result = candidate(service)
    permit(service, source["id"], "internal_review,generated_export")
    with pytest.raises(SearchError, match="internal use only"):
        service.approve_download(result["id"], "generated_export")
    permit(service, source["id"], "internal_review", "allowed_export")
    with pytest.raises(SearchError, match="requested use"):
        service.approve_download(result["id"], "generated_export")
    permit(service, source["id"], "generated_export", "allowed_export")
    approval = service.approve_download(result["id"], "generated_export")
    assert approval["requested_use"] == "generated_export"
    assert (
        service.repo.get("candidates", result["id"])["validation"]["validator"]
        == "manual"
    )


def test_scope_and_policy_revocation_are_checked_at_execution(service):
    source = fixture_source(service)
    result = candidate(service)
    permit(service, source["id"])
    approval = service.approve_download(result["id"], start_ms=12000, end_ms=20000)
    job = service.enqueue_clip(result["id"], start_ms=12000, end_ms=18000)
    assert job["payload"]["approval_id"] == approval["id"]
    assert (
        service.enqueue_clip(result["id"], start_ms=12000, end_ms=18000)["id"]
        == job["id"]
    )
    with pytest.raises(SearchError):
        service.enqueue_clip(result["id"], start_ms=20000, end_ms=30000)
    service.set_policy(
        source["id"], "blocked", "internal_review", "Rights revoked", "reviewer"
    )
    with pytest.raises(SearchError):
        authorize(
            service.repo, source["id"], "internal_review", approval["id"], 12000, 18000
        )


def test_approval_invalid_after_source_snapshot_change(service):
    source = fixture_source(service)
    result = candidate(service)
    permit(service, source["id"])
    approval = service.approve_download(result["id"])
    service.register_metadata(
        "local://lesson",
        "Changed source",
        "A different semantic snapshot",
        metadata={"duration_ms": 60000},
    )
    with pytest.raises(SearchError, match="metadata changed"):
        authorize(service.repo, source["id"], "internal_review", approval["id"])


def test_approval_invalid_after_caption_evidence_superseded(service):
    source = fixture_source(service)
    result = candidate(service)
    permit(service, source["id"])
    approval = service.approve_download(result["id"])
    ingest_captions(
        service.repo,
        source["id"],
        [{"start_ms": 12000, "end_ms": 20000, "text": "Revised evidence entirely."}],
    )
    with pytest.raises(SearchError, match="superseded"):
        authorize(service.repo, source["id"], "internal_review", approval["id"])
    with pytest.raises(SearchError, match="superseded"):
        service.approve_download(result["id"])


def test_expired_policy_cannot_authorize(service):
    source = fixture_source(service)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    service.set_policy(
        source["id"],
        "allowed_internal",
        "internal_review",
        "Time limited rights",
        "reviewer",
        expired,
    )
    with pytest.raises(SearchError, match="expired"):
        authorize(service.repo, source["id"], "internal_review")
    assert service.search("fractions")["results"] == []
    assert service.get_source(source["id"])["rights_status"] == "expired"


def test_blocked_sources_are_excluded_from_default_retrieval(service):
    source = fixture_source(service)
    service.set_policy(
        source["id"],
        "blocked",
        "internal_review",
        "Unrelated misleading catalogue description",
        "reviewer",
    )
    assert service.search("fractions")["results"] == []
    assert service.search("fractions", {"rights_status": "blocked"})["results"]


def test_job_claim_is_atomic_and_idempotent(service):
    repo = service.repo
    jobs = [
        repo.enqueue("test", {"index": index}, f"idempotent-{index}")
        for index in range(8)
    ]
    assert repo.enqueue("test", {"index": 0}, "idempotent-0")["id"] == jobs[0]["id"]
    with ThreadPoolExecutor(max_workers=8) as executor:
        claimed = list(
            executor.map(lambda index: repo.claim_job(f"worker-{index}"), range(8))
        )
    assert len({job["id"] for job in claimed}) == 8
    assert all(job["attempts"] == 1 for job in claimed)
    assert repo.claim_job("ninth") is None
    assert not repo.finish_job(claimed[0]["id"], {}, worker_id="wrong-owner")
    assert repo.finish_job(
        claimed[0]["id"], {"ok": True}, worker_id=claimed[0]["locked_by"]
    )


def test_queue_backpressure_preserves_existing_idempotent_job(service):
    service.repo.settings = replace(service.repo.settings, max_pending_jobs=1)
    job = service.repo.enqueue("test", {}, "one")
    assert service.repo.enqueue("test", {}, "one")["id"] == job["id"]
    with pytest.raises(SearchError, match="queue is full"):
        service.repo.enqueue("test", {}, "two")


def test_mutated_owned_media_invalidates_reviewed_approval(service):
    owned = service.repo.root / "owned" / "fixture.mp4"
    owned.parent.mkdir()
    owned.write_bytes(b"first reviewed version")
    source = fixture_source(service, local_path=str(owned), owned=True)
    result = candidate(service)
    permit(service, source["id"])
    approval = service.approve_download(result["id"])
    assert approval["source_file_sha256"]
    owned.write_bytes(b"replaced source bytes")
    with pytest.raises(SearchError, match="source media changed"):
        authorize(service.repo, source["id"], "internal_review", approval["id"])


def test_abandoned_lease_reclaims_and_retry_budget_is_durable(service):
    repo = service.repo
    job = repo.enqueue("test", {}, "abandoned")
    first = repo.claim_job("dead-worker")
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with repo.connect() as connection:
        connection.execute(
            "UPDATE jobs SET lease_until=? WHERE id=?", (past, job["id"])
        )
    second = Repository(repo.root).claim_job("replacement-worker")
    assert second["attempts"] == first["attempts"] + 1
    assert not repo.renew_lease(job["id"], "dead-worker")
    assert repo.fail_job(
        job["id"], "Temporary network outage", True, worker_id="replacement-worker"
    )
    with repo.connect() as connection:
        connection.execute(
            "UPDATE jobs SET available_at=? WHERE id=?", (past, job["id"])
        )
    third = repo.claim_job("third")
    assert third["attempts"] == third["max_attempts"]
    assert repo.fail_job(job["id"], "Temporary network outage", True, worker_id="third")
    assert repo.get("jobs", job["id"])["status"] == "failed"
    assert repo.claim_job("fourth") is None


def test_snapshots_never_persist_extractor_credentials(service):
    source = service.discover(
        "https://youtu.be/Abcdefghi12",
        metadata={
            "title": "Caption fixture",
            "duration": 30,
            "http_headers": {"Authorization": "secret"},
            "url": "https://cdn.example/video?token=secret",
            "subtitles": {
                "en": [{"url": "https://example.com/captions?signature=secret"}]
            },
        },
    )
    with service.repo.connect() as connection:
        raw = connection.execute(
            "SELECT raw_json FROM metadata_snapshots WHERE source_id=?", (source["id"],)
        ).fetchone()[0]
    assert "secret" not in raw and "signature" not in raw and "Authorization" not in raw
    assert "subtitles_languages" in raw


def test_captionless_discovery_is_metadata_only(monkeypatch, service):
    import sys
    import types
    from app.services.targeted_search import discovery

    calls = []

    class Downloader:
        def __init__(self, options):
            assert options["skip_download"]
            assert not options["writesubtitles"]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            calls.append(download)
            return {
                "id": "Abcdefghi12",
                "title": "An accessible source",
                "duration": 12,
            }

    monkeypatch.setitem(
        sys.modules, "yt_dlp", types.SimpleNamespace(YoutubeDL=Downloader)
    )
    monkeypatch.setattr(
        discovery, "verify_public_network", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        discovery.importlib.metadata, "version", lambda name: "fixture-version"
    )
    source = service.discover("https://youtu.be/Abcdefghi12")
    result = ingest_source(service, {"source_id": source["id"]})
    assert result["state"] == "captions_unavailable"
    assert calls == [False]
    assert not service.list_artifacts()


def test_semantic_paraphrase_fuses_versioned_normalized_local_vectors(
    monkeypatch, service
):
    from app.services.targeted_search import retrieval

    fixture_source(service)
    service.settings = replace(service.settings, semantic_enabled=True)

    class Encoder:
        def encode(self, texts, **kwargs):
            return [
                [3.0, 4.0]
                if ("fraction" in text or "different denominators" in text)
                else [0.0, 5.0]
                for text in texts
            ]

    monkeypatch.setattr(retrieval, "_text_model", lambda *args: Encoder())
    indexed = build_embeddings(service, {"source_id": "local:lesson"})
    assert indexed["count"] == 2
    result = service.search("different denominators")
    assert any(
        item["evidence_type"] == "transcript"
        and item["scores"].get("semantic", 0) > 0.99
        for item in result["results"]
    )
    assert (
        result["model_versions"]["embedding_model"] == service.settings.embedding_model
    )
    with service.repo.connect() as connection:
        vectors = [
            row[0] for row in connection.execute("SELECT vector_json FROM embeddings")
        ]
    assert "[0.6,0.8]" in vectors


def test_sparse_speech_cannot_become_semantic_timestamp_evidence(monkeypatch, service):
    from app.services.targeted_search import retrieval

    source = fixture_source(
        service, captions=[{"start_ms": 10000, "end_ms": 12000, "text": "I know"}]
    )
    service.settings = replace(service.settings, semantic_enabled=True)

    class Encoder:
        def encode(self, texts, **kwargs):
            return [[1.0, 1.0] for text in texts]

    monkeypatch.setattr(retrieval, "_text_model", lambda *args: Encoder())
    build_embeddings(service, {"source_id": source["id"]})
    results = service.search("Bakken oil rigs")["results"]
    assert not any(item["evidence_type"] == "transcript" for item in results)
    assert results[0]["evidence_type"] == "metadata" and results[0]["start_ms"] is None


def test_visual_description_is_honest_timed_evidence(service):
    source = fixture_source(service)
    ingest_captions(
        service.repo,
        source["id"],
        [
            {
                "start_ms": 30000,
                "end_ms": 34000,
                "text": "Oil drilling rig in North Dakota",
            }
        ],
        kind="visual_description",
        provider="licensed catalogue",
    )
    result = service.search("oil drilling rig")["results"][0]
    assert result["evidence_type"] == "visual_description"
    assert result["start_ms"] == 30000


def test_shared_cas_bytes_allow_distinct_provenance(service):
    first = fixture_source(service)
    second = fixture_source(service, "another")
    shared = service.repo.root / "artifacts" / "same.mp4"
    shared.parent.mkdir()
    shared.write_bytes(b"identical content")
    one = service.repo.insert_artifact(
        id="one",
        source_id=first["id"],
        kind="source",
        path=shared,
        sha256="a" * 64,
        bytes=17,
    )
    two = service.repo.insert_artifact(
        id="two",
        source_id=second["id"],
        kind="source",
        path=shared,
        sha256="a" * 64,
        bytes=17,
    )
    assert one["path"] == two["path"] and one["id"] != two["id"]
