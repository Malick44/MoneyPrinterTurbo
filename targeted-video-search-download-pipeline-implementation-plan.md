# Targeted Video Search & Download Pipeline

## Implementation Plan

**Purpose:** Build a self-hosted, provenance-aware pipeline that discovers video from approved sources, searches subtitles and visual evidence, validates relevance, and only downloads/extracts media after a policy gate.

**Primary design goal:** Return reliable, timestamped moments—not merely a ranked list of whole videos.

---

## 1. Scope and success criteria

### MVP capabilities

- Discover videos from allowlisted channels, playlists, feeds, direct URLs, and owned object storage.
- Fetch metadata and captions before downloading media.
- Search transcript chunks using both exact/full-text and semantic retrieval.
- Return source URL, evidence text, timestamp range, retrieval score, and rights status.
- Allow a user or policy rule to approve a selected source for download.
- Download authorized media, extract an exact clip, create a review proxy, and write a provenance sidecar manifest.
- Resume interrupted work safely and avoid duplicate source or artifact processing.

### Phase-2 capabilities

- Scene-aware keyframe extraction, OCR, and CLIP/SigLIP visual search.
- Automated contextual validation with a cross-encoder and optional LLM evidence gate.
- Batch/queue workers, retry policies, dashboards, and metrics.
- Source-specific adapters, course/library collections, and a React Native-compatible API.

### Non-goals for MVP

- Crawling the open web indiscriminately.
- Downloading sources without clear permission, authorization, or a documented policy.
- Automatically publishing or redistributing extracted clips.
- Building an all-purpose NLE/editor.

### Definition of done

A query such as “show a concise demonstration of subtracting fractions with unlike denominators” should:

1. Search captions and metadata from approved sources without initially downloading video.
2. Return timestamped evidence from the best source segments.
3. Permit approval of a result under a documented rights policy.
4. Download/process only the approved source.
5. Produce a timestamp-accurate clip, a review proxy, hashes, and a `.meta.json` provenance manifest.

---

## 2. System architecture

```text
                        ┌───────────────────────────┐
                        │ Approved source adapters  │
                        │ URLs / playlists / feeds  │
                        └─────────────┬─────────────┘
                                      │
                       metadata + captions only
                                      │
             ┌────────────────────────▼────────────────────────┐
             │ Ingestion worker                                 │
             │ canonicalize, dedupe, snapshot, state transitions│
             └─────────────┬───────────────────────────┬────────┘
                           │                           │
                    SQLite / Postgres            Artifact storage
                           │                           │
        ┌──────────────────▼──────────────────┐        │
        │ Search index                          │        │
        │ FTS5/BM25 + vectors + metadata        │        │
        └──────────────────┬──────────────────┘        │
                           │                           │
                    query and rank fusion              │
                           │                           │
        ┌──────────────────▼──────────────────┐        │
        │ Re-rank / evidence validation         │        │
        │ cross-encoder, optional LLM gate      │        │
        └──────────────────┬──────────────────┘        │
                           │                           │
                     rights/policy decision            │
                           │                           │
        ┌──────────────────▼──────────────────┐        │
        │ Acquisition and media worker          │        │
        │ yt-dlp → FFmpeg → proxy → manifests   │────────┘
        └──────────────────┬──────────────────┘
                           │
        ┌──────────────────▼──────────────────┐
        │ Optional multimodal worker            │
        │ scene detect → keyframes → OCR → CLIP │
        └─────────────────────────────────────┘
```

### Recommended initial stack

| Layer | MVP choice | Scale-up replacement / addition |
|---|---|---|
| API | FastAPI | FastAPI behind a gateway/load balancer |
| Database | SQLite with WAL mode | PostgreSQL |
| Lexical retrieval | SQLite FTS5 | PostgreSQL FTS or OpenSearch |
| Vector retrieval | `sqlite-vec`, USearch, or FAISS | Qdrant |
| Discovery/acquisition | `yt-dlp` and source-specific adapters | Separate adapter services |
| Transcription fallback | `faster-whisper` | Dedicated GPU worker pool |
| Visual index | OpenCLIP or SigLIP | Qdrant multi-vector collections |
| OCR | PaddleOCR | GPU worker / managed scheduling |
| Media operations | FFmpeg, ffprobe | Worker autoscaling and object storage |
| Job execution | Python process + SQLite job table | Dramatiq, Celery, Temporal, or Prefect |
| Object storage | Local disk or MinIO | Cloudflare R2, S3, or MinIO cluster |
| Observability | Structured JSON logs | OpenTelemetry + metrics/log aggregation |

---

## 3. Policy and rights gate

Implement policy before automated acquisition. A technical download capability is not permission to download, store, redistribute, or publish content.

### Required source policy fields

- Source platform and canonical URL.
- Stable platform video ID, channel/creator ID, and uploader name.
- License claim as published by the source, if available.
- `rights_status`: `unknown`, `allowed_internal`, `allowed_export`, `review_required`, `blocked`, or `expired`.
- Policy reason and reviewer identity where applicable.
- Intended usage: discovery only, internal analysis, editorial proxy, permitted delivery, or archival.
- Retention date and deletion requirement.

### Acquisition rule

No worker may invoke a media download unless both conditions hold:

```text
candidate is relevance-approved
AND
rights_status permits this requested artifact/use
```

A user-visible review step should remain available for any source with `unknown` or `review_required` status.

---

## 4. Data model

Use database migrations from day one. Enable foreign keys and WAL mode for SQLite.

### Core tables

#### `sources`

The canonical identity for an external video or internally owned media object.

```sql
CREATE TABLE sources (
  id TEXT PRIMARY KEY,
  platform TEXT NOT NULL,
  platform_video_id TEXT,
  canonical_url TEXT NOT NULL,
  normalized_url TEXT NOT NULL,
  title TEXT,
  creator_name TEXT,
  creator_id TEXT,
  published_at TEXT,
  duration_ms INTEGER,
  language TEXT,
  discovered_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  state TEXT NOT NULL,
  metadata_hash TEXT,
  UNIQUE(platform, platform_video_id),
  UNIQUE(normalized_url)
);
```

#### `source_metadata_snapshots`

Keep immutable metadata snapshots instead of overwriting source history.

```sql
CREATE TABLE source_metadata_snapshots (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  fetched_at TEXT NOT NULL,
  extractor_name TEXT NOT NULL,
  extractor_version TEXT,
  raw_json TEXT NOT NULL,
  snapshot_sha256 TEXT NOT NULL,
  UNIQUE(source_id, snapshot_sha256)
);
```

#### `rights_and_policy`

```sql
CREATE TABLE rights_and_policy (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  license_claim TEXT,
  rights_status TEXT NOT NULL,
  permitted_use TEXT NOT NULL,
  policy_reason TEXT,
  reviewed_by TEXT,
  reviewed_at TEXT,
  expires_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
```

#### `captions`

```sql
CREATE TABLE captions (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  language TEXT,
  kind TEXT NOT NULL,
  provider TEXT,
  is_auto_generated INTEGER NOT NULL DEFAULT 0,
  raw_text TEXT,
  raw_artifact_id TEXT,
  created_at TEXT NOT NULL
);
```

#### `transcript_chunks`

```sql
CREATE TABLE transcript_chunks (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  caption_id TEXT REFERENCES captions(id),
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  token_count INTEGER,
  language TEXT,
  chunk_hash TEXT NOT NULL,
  embedding_model TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(source_id, start_ms, end_ms, chunk_hash)
);
```

Create an external-content FTS5 table linked to `transcript_chunks`:

```sql
CREATE VIRTUAL TABLE transcript_chunks_fts USING fts5(
  text,
  content='transcript_chunks',
  content_rowid='rowid',
  tokenize='unicode61'
);
```

#### `video_scenes`, `keyframes`, and `ocr_blocks`

```sql
CREATE TABLE video_scenes (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  detector TEXT NOT NULL,
  detector_version TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE keyframes (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  scene_id TEXT REFERENCES video_scenes(id),
  timestamp_ms INTEGER NOT NULL,
  artifact_id TEXT,
  perceptual_hash TEXT,
  embedding_model TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE ocr_blocks (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  keyframe_id TEXT REFERENCES keyframes(id),
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  text TEXT NOT NULL,
  confidence REAL,
  language TEXT,
  created_at TEXT NOT NULL
);
```

#### `embeddings`

Keep vectors conceptually separate from entities and modalities. For SQLite, store a vector reference or use `sqlite-vec`; for Qdrant, store the point ID and collection name.

```sql
CREATE TABLE embeddings (
  id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  modality TEXT NOT NULL,
  model_name TEXT NOT NULL,
  model_revision TEXT,
  dimensions INTEGER NOT NULL,
  vector_store TEXT NOT NULL,
  vector_ref TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(entity_type, entity_id, modality, model_name, content_hash)
);
```

#### `candidate_segments` and `validation_runs`

```sql
CREATE TABLE candidate_segments (
  id TEXT PRIMARY KEY,
  search_run_id TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(id),
  start_ms INTEGER NOT NULL,
  end_ms INTEGER NOT NULL,
  lexical_rank INTEGER,
  vector_rank INTEGER,
  fused_score REAL,
  reranker_score REAL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE validation_runs (
  id TEXT PRIMARY KEY,
  candidate_segment_id TEXT NOT NULL REFERENCES candidate_segments(id),
  validator_type TEXT NOT NULL,
  model_name TEXT NOT NULL,
  prompt_version TEXT,
  input_hash TEXT NOT NULL,
  decision TEXT NOT NULL,
  relevance REAL,
  result_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
```

#### `media_artifacts` and `clip_provenance`

```sql
CREATE TABLE media_artifacts (
  id TEXT PRIMARY KEY,
  source_id TEXT NOT NULL REFERENCES sources(id),
  parent_artifact_id TEXT REFERENCES media_artifacts(id),
  artifact_type TEXT NOT NULL,
  storage_uri TEXT NOT NULL,
  sha256 TEXT,
  bytes INTEGER,
  start_ms INTEGER,
  end_ms INTEGER,
  width INTEGER,
  height INTEGER,
  fps_num INTEGER,
  fps_den INTEGER,
  video_codec TEXT,
  audio_codec TEXT,
  profile_name TEXT,
  pipeline_version TEXT,
  created_at TEXT NOT NULL,
  UNIQUE(storage_uri)
);

CREATE TABLE clip_provenance (
  id TEXT PRIMARY KEY,
  clip_artifact_id TEXT NOT NULL REFERENCES media_artifacts(id),
  source_id TEXT NOT NULL REFERENCES sources(id),
  source_start_ms INTEGER NOT NULL,
  source_end_ms INTEGER NOT NULL,
  retrieval_query TEXT,
  candidate_segment_id TEXT REFERENCES candidate_segments(id),
  rights_status_at_export TEXT NOT NULL,
  manifest_artifact_id TEXT REFERENCES media_artifacts(id),
  created_at TEXT NOT NULL
);
```

#### `jobs` and `processing_events`

```sql
CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  job_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL UNIQUE,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 0,
  max_attempts INTEGER NOT NULL DEFAULT 3,
  locked_at TEXT,
  locked_by TEXT,
  available_at TEXT NOT NULL,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE processing_events (
  id TEXT PRIMARY KEY,
  source_id TEXT REFERENCES sources(id),
  artifact_id TEXT REFERENCES media_artifacts(id),
  job_id TEXT REFERENCES jobs(id),
  event_type TEXT NOT NULL,
  level TEXT NOT NULL,
  payload_json TEXT,
  created_at TEXT NOT NULL
);
```

### Indexes and constraints

- Create indexes on `source_id`, `start_ms`, `end_ms`, `state`, `rights_status`, `job.status`, and `jobs.available_at`.
- Enforce a unique stable source key whenever a platform provides one.
- Use an idempotency key for every job: `job_type + source_id + settings_hash + pipeline_version`.
- Store UTC ISO-8601 timestamps.
- Version every model, prompt, pipeline configuration, and artifact profile.

---

## 5. State machine

Do not represent workflow state only in logs. Persist it in `sources.state` and job/artifact rows.

```text
discovered
  → metadata_fetched
  → captions_fetched
  → text_indexed
  → candidate_ranked
  → relevance_verified
  → rights_reviewed
  → download_approved
  → source_downloaded
  → clip_extracted
  → review_proxy_ready
  → visual_indexed
  → ready

Terminal alternatives:
  → rejected
  → blocked_by_policy
  → unavailable
  → failed
  → archived
```

State transition rules:

- Allow retries from a failed job only when its failure category is retryable.
- Never overwrite an earlier metadata snapshot or artifact record.
- A source may be `text_indexed` without media being downloaded.
- A clip may not be extracted unless a rights record permits the requested use.
- Visual indexing can occur before or after approval only when the corresponding media derivative is allowed by policy.

---

## 6. Ingestion and discovery worker

### Responsibilities

- Accept an approved URL, channel, playlist, feed item, or internally owned object.
- Normalize and canonicalize the source URL.
- Extract metadata only first.
- Discover available captions/subtitles.
- Persist source, snapshot, and source state transactionally.
- Enqueue caption parsing/indexing work.
- Do not download full media during discovery.

### `yt-dlp` metadata-only pattern

```bash
yt-dlp \
  --skip-download \
  --write-info-json \
  --write-subs \
  --write-auto-subs \
  --sub-langs 'en.*,en' \
  --sub-format 'vtt/best' \
  --no-playlist \
  --paths 'staging/discovery/%(extractor_key)s/%(id)s' \
  'SOURCE_URL'
```

Notes:

- Use a source allowlist and configured authentication only where authorized.
- Treat extractor output as untrusted input; validate fields before persistence.
- Store raw `info.json` as an immutable metadata snapshot.
- Use a dedicated staging directory and atomically promote completed artifacts.

### URL canonicalization

Implement a per-platform normalizer that:

- Removes tracking parameters.
- Converts alternative URL forms into one canonical representation.
- Extracts a stable platform ID where available.
- Retains the original submitted URL in a separate field or event record.
- Avoids merging records solely because titles match.

### Pseudocode

```python
def ingest_discovery(url: str, policy_context: dict) -> str:
    normalized = canonicalize_url(url)
    key = source_key(normalized)

    source = upsert_source_if_absent(key, normalized, policy_context)
    if source.state in {"captions_fetched", "text_indexed", "ready"}:
        return source.id

    metadata = extract_metadata_only(normalized)
    persist_metadata_snapshot(source.id, metadata)
    update_source_fields(source.id, metadata)

    captions = locate_and_fetch_captions(metadata)
    persist_captions(source.id, captions)
    transition_source(source.id, "captions_fetched")

    enqueue("parse_and_index_captions", source_id=source.id)
    return source.id
```

---

## 7. Transcript processing and hybrid retrieval

### Chunking policy

Create segments from caption timing rather than fixed character counts alone.

Initial defaults:

- Target window: 20–45 seconds.
- Maximum text: approximately 250–450 tokens.
- Overlap: 3–8 seconds, or one caption boundary.
- Break preferentially on sentence endings and speaker turns.
- Include a small preceding/following context window only at query/validation time.

Persist original caption timing separately so every generated chunk can be traced back to source cues.

### Retrieval components

1. **Lexical:** FTS5/BM25 over transcript text, OCR text, title, creator, tags, and descriptions.
2. **Semantic:** local sentence-transformer embeddings over transcript chunks and OCR blocks.
3. **Metadata filters:** language, channel, duration, source collection, date, content type, and rights status.
4. **Fusion:** Reciprocal Rank Fusion (RRF) across lexical and vector rankings.
5. **Re-ranking:** local cross-encoder over the fused top-0 candidates.
6. **Validation:** optional structured LLM evidence gate on the top 3–10 results.

### RRF

For each candidate document `d`:

```text
rrf(d) = Σ 1 / (k + rank_i(d))
```

Start with `k = 60`. Use the result as an ordering signal, then re-rank the small candidate set with a stronger model.

### Retrieval pseudocode

```python
def search(query: str, filters: dict, top_k: int = 20):
    lexical = fts5_search(query, filters, limit=100)
    query_vector = embed_text(query)
    semantic = vector_search(query_vector, filters, limit=100)

    candidates = reciprocal_rank_fusion([lexical, semantic], k=60)
    reranked = cross_encoder_rerank(query, candidates[:50])

    return collapse_adjacent_segments(reranked[:top_k])
```

### Adjacent-segment consolidation

Merge neighboring retrieved chunks when:

- They belong to the same source.
- Their timestamps are within 5–15 seconds.
- Their combined relevance remains above the threshold.
- The merged duration stays within a configured maximum, such as 90 seconds.

This prevents a user from receiving fragmented 20-second results where one coherent 60-second explanation is the better output.

---

## 8. Contextual validation

### Principle

A validator must assess **evidence provided to it**, not infer unseen video content. It should never have authority to bypass policy or publish content.

### Validation order

1. Apply deterministic source and rights filters.
2. Use hybrid retrieval.
3. Apply cross-encoder re-ranking.
4. Call an LLM only when ambiguity remains or a costly action is requested.
5. Require structured output and evidence quotes.

### Validator input

- Original user request.
- Source title, creator, description, duration, and source policy status.
- Candidate time range.
- Candidate transcript plus 30–90 seconds of before/after context.
- OCR text and visual captions where available.
- Explicit evaluation rubric.

### Required output schema

```json
{
  "decision": "approve",
  "relevance": 0.91,
  "primary_topic": "Subtracting fractions with unlike denominators",
  "evidence": [
    {
      "start_ms": 462000,
      "end_ms": 503000,
      "quote": "First, rewrite both fractions using the least common denominator."
    }
  ],
  "recommended_window": {
    "start_ms": 454000,
    "end_ms": 515000
  },
  "reason": "The speaker demonstrates the requested procedure rather than merely mentioning it.",
  "download_justified": true
}
```

### Guardrails

- Reject missing evidence, invalid timestamps, and non-JSON output.
- Require every claimed conclusion to cite supplied transcript/OCR evidence.
- Use a conservative threshold for autonomous progression, such as `relevance >= 0.85`.
- Route borderline decisions to manual review.
- Save prompt version, model ID, input hash, response, latency, and cost estimate.
- Build a labeled evaluation set and measure precision at each stage before automating downloads.

---

## 9. Media acquisition and artifact pipeline

### Artifact profiles

| Artifact | Purpose | Initial profile |
|---|---|---|
| Source record | Canonical provenance | URL + metadata snapshot, with no download required |
| Analysis proxy | ASR/scene/OCR work | 360p–540p H.264, low bitrate; audio-only where valid |
| Review proxy | Browser and quick review | 720p H.264, AAC, source FPS preserved where feasible |
| NLE proxy | Smooth editorial scrubbing | ProRes Proxy or DNxHR LB; use only when requested |
| Delivery clip | Export/share workflow | Destination-specific H.264/H.265 and captions |

### Download workflow

1. Verify candidate relevance and rights/policy status.
2. Create a `download_source` job with an idempotency key.
3. Download to a temporary staging location.
4. Validate file with `ffprobe`.
5. Hash the completed file with SHA-256.
6. Persist a `media_artifacts` row.
7. Move/promote artifact to immutable content-addressed storage.
8. Enqueue analysis proxy, extraction, and/or visual indexing jobs as allowed.

### Clip extraction

For precise requested boundaries, re-encode the clip. Stream-copy can be fast but may shift cuts to nearby keyframes.

```bash
ffmpeg -hide_banner -y \
  -ss 00:07:42.000 \
  -to 00:08:22.000 \
  -i source.mp4 \
  -map 0:v:0 -map 0:a? \
  -c:v libx264 -preset medium -crf 20 \
  -c:a aac -b:a 160k \
  extracted-clip.mp4
```

Store source and output time ranges in milliseconds in the database and manifest.

### Review proxy command

Start with a review-friendly profile; preserve source FPS unless normalization is a deliberate requirement.

```bash
ffmpeg -hide_banner -y \
  -i extracted-clip.mp4 \
  -vf "scale=-2:720" \
  -c:v libx264 -preset veryfast -crf 23 \
  -movflags +faststart \
  -c:a aac -b:a 128k \
  review-720p.mp4
```

### Frame-rate policy

- Preserve the source timing and frame rate by default.
- If generating a constant-frame-rate proxy, record both source and target frame rate in artifact metadata.
- Validate audio duration and A/V sync after every normalization.
- Do not use proxies as preservation masters.

---

## 10. Visual indexing and OCR

### Initial visual pipeline

1. Generate an analysis proxy if authorized.
2. Detect scene boundaries with PySceneDetect or a comparable detector.
3. Sample a representative sharp frame per scene.
4. Add sparse baseline samples every 10–20 seconds for long static scenes.
5. Run OCR on slide-like or text-rich frames.
6. Generate CLIP/SigLIP embeddings for representative frames.
7. Store all results with source/time linkage.

### Adaptive sampling rules

- **Talking-head or static scene:** sample sparsely.
- **Slide/deck recording:** sample each slide transition; OCR every representative slide.
- **Rapid demonstrations or archival montage:** increase density around scene boundaries.
- **Candidate result neighborhood:** sample 1–2 fps in a limited window around high-ranked transcript hits when visual validation is needed.

### Visual query flow

```text
Text query
  ├─ text embedding → keyframe CLIP/SigLIP similarity
  ├─ lexical query → OCR FTS5
  ├─ transcript retrieval
  └─ metadata filters
       ↓
  modality-aware rank fusion
       ↓
  timestamped segment results
```

### Storage controls

- Store thumbnails/keyframes at a configurable quality and maximum edge length.
- Deduplicate visually near-identical frames using perceptual hashes.
- Keep the original timecode and scene identifier for every visual embedding.
- Version OCR and vision models so results can be rebuilt consistently.

---

## 11. Provenance manifest

Write a `.meta.json` sidecar for every exported clip and save its own hash/artifact record.

```json
{
  "schema_version": "1.0",
  "clip_id": "clip_01J...",
  "parent_source_id": "youtube:abc123",
  "canonical_source_url": "https://www.youtube.com/watch?v=abc123",
  "source_title": "Example Lesson",
  "source_creator": "Example Channel",
  "source_published_at": "2026-01-10T00:00:00Z",
  "retrieved_at": "2026-09-29T18:20:00Z",
  "license_claim": "CC BY 4.0",
  "rights_status": "approved_for_internal_review",
  "source_start_ms": 462000,
  "source_end_ms": 515000,
  "output_duration_ms": 53000,
  "transcript_excerpt": "First, rewrite both fractions using the least common denominator...",
  "retrieval_query": "demonstration of subtracting fractions",
  "retrieval_scores": {
    "fts": 8.1,
    "vector": 0.83,
    "reranker": 0.92,
    "validator": 0.91
  },
  "pipeline": {
    "version": "2026.09.29",
    "ffmpeg_version": "recorded-at-runtime",
    "embedding_model": "record-model-and-revision",
    "transcription_model": "record-model-and-revision"
  },
  "input_sha256": "...",
  "output_sha256": "...",
  "artifact_profile": "review-720p-h264",
  "attribution_text": "..."
}
```

Do not treat the sidecar as the canonical operational database. It is a portable export record that must agree with the database.

---

## 12. API contract

### MVP endpoints

| Endpoint | Purpose |
|---|---|
| `POST /sources/discover` | Submit an approved source URL/collection for metadata/caption discovery |
| `GET /sources/{source_id}` | Source status, metadata, policy, artifacts, and processing history |
| `POST /search` | Hybrid search over indexed transcript/OCR/visual evidence |
| `POST /candidates/{candidate_id}/validate` | Run bounded contextual validation |
| `POST /candidates/{candidate_id}/approve-download` | Record authorization and enqueue acquisition |
| `POST /clips` | Extract an approved source time range |
| `GET /clips/{clip_id}` | Artifact links/metadata as allowed by access policy |
| `GET /jobs/{job_id}` | Job status, errors, attempts, and emitted events |

### Search response example

```json
{
  "query": "demonstrate subtracting fractions with unlike denominators",
  "results": [
    {
      "candidate_id": "cand_01J...",
      "source_id": "youtube:abc123",
      "title": "Subtracting Fractions",
      "start_ms": 462000,
      "end_ms": 515000,
      "evidence": "First, rewrite both fractions using the least common denominator...",
      "scores": {
        "rrf": 0.031,
        "reranker": 0.92
      },
      "rights_status": "review_required",
      "actions": {
        "can_download": false,
        "requires_review": true
      }
    }
  ]
}
```

---

## 13. Repository structure

```text
video-pipeline/
  README.md
  pyproject.toml
  compose.yaml
  .env.example
  migrations/
  app/
    api/
    config/
    db/
    domain/
    workers/
      discovery.py
      captions.py
      embeddings.py
      validation.py
      acquisition.py
      media.py
      visual.py
    retrieval/
      fts.py
      vectors.py
      fusion.py
      rerank.py
    adapters/
      youtube.py
      local.py
      rss.py
    media/
      ffmpeg.py
      ffprobe.py
      scenes.py
      ocr.py
    storage/
    observability/
  tests/
    unit/
    integration/
    fixtures/
  scripts/
    dev_seed.py
    backfill_embeddings.py
    evaluate_retrieval.py
  docs/
    architecture.md
    source-policy.md
    runbook.md
    manifest-schema.md
```

---

## 14. Delivery milestones

### Milestone 0: foundations

**Outcome:** reproducible local development environment and source-policy enforcement.

- Initialize repository, linting, tests, migrations, Docker Compose, and configuration management.
- Add SQLite WAL mode, foreign keys, migrations, and core tables.
- Implement source policy/rights record creation.
- Implement structured logging and a basic job table.
- Add test fixtures containing only content you own or are licensed to use.

**Acceptance tests:**

- A source cannot enter download state without policy authorization.
- Duplicate canonical URLs result in one source record.
- Jobs are idempotent and retry only within configured limits.

### Milestone 1: caption-first discovery

**Outcome:** search a known collection without downloading its videos.

- Implement one source adapter, starting with approved direct URLs or a controlled channel/playlist source.
- Run metadata-only and subtitle-only extraction.
- Parse VTT/SRT/JSON subtitles into timestamped caption rows.
- Chunk captions and build FTS5 indexing.
- Return exact keyword-search results with timestamp ranges.

**Acceptance tests:**

- Re-ingesting a source produces no duplicate captions/chunks.
- Search results open at the expected source timestamp.
- The source has no full-media artifact after discovery.

### Milestone 2: local semantic retrieval

**Outcome:** hybrid search handles both exact terms and paraphrases.

- Add a local sentence-transformer embedding worker.
- Add `sqlite-vec`, USearch, or FAISS for vector lookup.
- Implement RRF and metadata filtering.
- Add an offline retrieval-evaluation script using a hand-labeled query set.

**Acceptance tests:**

- A paraphrase query retrieves the correct transcript segment.
- Exact-name queries remain strong through FTS5.
- Hybrid retrieval outperforms either retrieval method alone on the local test set.

### Milestone 3: validation and approval

**Outcome:** costly actions happen only for relevant, approved candidates.

- Add cross-encoder re-ranking.
- Add a strict JSON evidence-validation contract.
- Add manual approval endpoint/UI before acquisition.
- Record model/prompt inputs and decisions for auditability.

**Acceptance tests:**

- A mention-only false positive is rejected or flagged for review.
- Validator outputs cannot alter rights status.
- Download job cannot be enqueued without approval and valid policy.

### Milestone 4: acquisition, clips, and provenance

**Outcome:** approved result becomes a traceable playable clip.

- Implement acquisition worker with staging, `ffprobe`, SHA-256, and artifact persistence.
- Implement accurate clip extraction and review-proxy creation.
- Generate and validate `.meta.json` sidecars.
- Add artifact cleanup/retention policy.

**Acceptance tests:**

- Every exported clip has a linked source, exact time range, hashes, and manifest.
- The generated clip duration and A/V sync fall within defined tolerance.
- Re-running an artifact job returns the existing artifact rather than duplicating it.

### Milestone 5: visual/OCR indexing

**Outcome:** searches can find non-verbal visual material.

- Add scene detection, adaptive keyframes, OCR, and visual embedding workers.
- Add OCR FTS and visual similarity retrieval.
- Fuse transcript, OCR, and visual results.
- Add query/result diagnostics showing why each modality contributed.

**Acceptance tests:**

- A diagram/slide with no matching speech is retrievable through OCR or vision search.
- Duplicate static frames do not dominate results.
- Processing cost stays within configured per-minute media budget.

### Milestone 6: production hardening

**Outcome:** reliable self-hosted deployment.

- Move vectors to Qdrant and metadata/jobs to PostgreSQL if scale requires it.
- Add object storage, worker queue, backpressure, retry classification, and dead-letter handling.
- Add authentication, tenant/user boundaries if required, rate limiting, encryption, and backups.
- Add metrics for ingest rate, queue latency, recall/precision, worker failures, storage, and per-source cost.

---

## 15. Evaluation and observability

### Create a small gold dataset

Before automating decisions, assemble 50–150 representative queries with:

- Intended relevant sources.
- Correct timestamps/time ranges.
- Hard negatives: mentions in passing, misleading titles, irrelevant chapters, duplicate uploads.
- Visual-only retrieval examples.
- Policy/rights cases.

### Metrics

| Layer | Metric |
|---|---|
| Discovery | metadata/caption extraction success rate, duplicate rate |
| Retrieval | Recall@10, MRR, nDCG@10, time-range overlap |
| Validation | precision, false-download rate, manual-review rate |
| Media | acquisition success, clip boundary error, A/V sync failures |
| Visual | visual-only query recall, OCR accuracy on representative content |
| Operations | queue depth, retries, job duration, storage growth, cost/source-minute |

### Minimum logs per job

- Job ID, source ID, artifact ID, idempotency key.
- Worker/version/model/version information.
- Start/end time, retries, error category.
- Input/output hashes.
- Relevant external command exit codes and sanitized stderr.
- Resource timing and estimated cost.

---

## 16. Security and operations

- Never accept arbitrary shell arguments from users; construct `yt-dlp` and FFmpeg command arguments as lists.
- Validate URLs against allowlisted source domains before worker execution.
- Run media extraction in a restricted container/user with quotas on CPU, memory, disk, duration, and network.
- Treat downloaded media and metadata as untrusted.
- Protect source credentials and API keys through environment/secret management, never manifests or logs.
- Use content-addressed storage and atomic file moves to avoid partial artifacts appearing complete.
- Back up the database, manifests, and source-policy records together.
- Plan deletion propagation: deleting a source should identify all derivatives, embeddings, thumbnails, OCR blocks, and exports affected.

---

## 17. First implementation sprint

Build this narrow vertical slice first:

1. Configure a local SQLite database with `sources`, `captions`, `transcript_chunks`, FTS5, `jobs`, `rights_and_policy`, and `processing_events`.
2. Support one approved input URL and one metadata/caption adapter.
3. Normalize the URL, upsert the source, save a metadata snapshot, fetch captions without media download, and create transcript chunks.
4. Implement FTS5 search that returns `source_id`, source title, evidence text, and `[start_ms, end_ms]`.
5. Add a manual “approve download” action that checks rights status.
6. Download only an approved source, extract one selected time window, make a 720p review proxy, and write a provenance sidecar.
7. Add integration tests with a locally hosted fixture video or content you own.

Do not begin with visual embedding, LLM agents, or a distributed worker queue. Establish correct source identity, timestamps, permissions, artifacts, and provenance first. Those foundations make later multimodal and agentic features safe and maintainable.

---

## 18. Decision log

Record these decisions explicitly as the project evolves.

| Decision | Initial choice | Revisit when |
|---|---|---|
| Metadata DB | SQLite | concurrent writers, multi-user access, or operational replication is needed |
| Vector store | sqlite-vec/USearch/FAISS | corpus exceeds local constraints or filtering/multi-vector needs grow |
| Production vector DB | Qdrant | Milvus/OpenSearch becomes justified by scale/search requirements |
| Text embeddings | Local sentence-transformer | measured recall or multilingual/domain requirements indicate a better model |
| Validation | Cross-encoder first, optional LLM | validated precision/cost supports more automation |
| Video acquisition | Rights-gated `yt-dlp` | source-specific APIs or owned storage offer better compliance/reliability |
| Proxies | 720p H.264 review proxy | NLE performance requires ProRes Proxy/DNxHR LB |
| Visual embeddings | CLIP/SigLIP on scene-aware frames | temporal/action understanding requires video-embedding models |
| Orchestration | SQLite jobs | throughput/retry/visibility needs justify a queue/orchestrator |

---

## 19. Final execution sequence

```text
Approved source
→ metadata/captions only
→ deduplicate and persist immutable snapshots
→ transcript chunking
→ FTS5 + local vector indexing
→ hybrid rank fusion
→ cross-encoder / evidence validation
→ rights and human/policy approval
→ authorized download
→ hash + artifact registration
→ precise clip extraction
→ review/NLE proxy on demand
→ provenance sidecar
→ optional scene/OCR/visual index
→ searchable timestamped evidence for future queries
```

This sequence is designed to minimize unnecessary downloads and compute, retain auditability, support educational-video retrieval, and scale gradually from a local developer tool to a self-hosted production service.
