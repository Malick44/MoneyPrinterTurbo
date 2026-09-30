CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE sources (
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, platform_video_id TEXT, canonical_url TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL DEFAULT '', description TEXT NOT NULL DEFAULT '', creator_name TEXT NOT NULL DEFAULT '',
 creator_id TEXT, duration_ms INTEGER, language TEXT, published_at TEXT, state TEXT NOT NULL DEFAULT 'registered',
 metadata_json TEXT NOT NULL DEFAULT '{}', metadata_hash TEXT, local_path TEXT, discovered_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(platform,platform_video_id), CHECK(duration_ms IS NULL OR duration_ms>0)
);
CREATE TABLE metadata_snapshots (
 id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES sources(id), raw_json TEXT NOT NULL,
 sha256 TEXT NOT NULL, extractor_name TEXT NOT NULL, extractor_version TEXT, created_at TEXT NOT NULL,
 UNIQUE(source_id,sha256)
);
CREATE TABLE collections(id TEXT PRIMARY KEY,name TEXT NOT NULL,topic TEXT NOT NULL DEFAULT '',queries_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL);
CREATE TABLE collection_sources(collection_id TEXT NOT NULL REFERENCES collections(id),source_id TEXT NOT NULL REFERENCES sources(id),PRIMARY KEY(collection_id,source_id));
CREATE TABLE captions (
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),language TEXT NOT NULL DEFAULT '',kind TEXT NOT NULL,
 provider TEXT NOT NULL,sha256 TEXT NOT NULL,raw_text TEXT NOT NULL,is_active INTEGER NOT NULL DEFAULT 1,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
 UNIQUE(source_id,language,kind,sha256)
);
CREATE TABLE caption_cues (
 id TEXT PRIMARY KEY,caption_id TEXT NOT NULL REFERENCES captions(id),source_id TEXT NOT NULL REFERENCES sources(id),
 start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,text TEXT NOT NULL,cue_hash TEXT NOT NULL,created_at TEXT NOT NULL,
 CHECK(start_ms>=0 AND end_ms>start_ms), UNIQUE(caption_id,cue_hash)
);
CREATE TABLE transcript_chunks (
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),caption_id TEXT NOT NULL REFERENCES captions(id),
 start_ms INTEGER NOT NULL,end_ms INTEGER NOT NULL,text TEXT NOT NULL,language TEXT NOT NULL DEFAULT '',chunk_hash TEXT NOT NULL,
 cue_ids_json TEXT NOT NULL DEFAULT '[]',created_at TEXT NOT NULL,CHECK(start_ms>=0 AND end_ms>start_ms),
 UNIQUE(source_id,caption_id,start_ms,end_ms,chunk_hash)
);
CREATE VIRTUAL TABLE transcript_chunks_fts USING fts5(text,content='transcript_chunks',content_rowid='rowid',tokenize='unicode61');
CREATE TRIGGER chunks_ai AFTER INSERT ON transcript_chunks BEGIN INSERT INTO transcript_chunks_fts(rowid,text) VALUES(new.rowid,new.text); END;
CREATE TRIGGER chunks_ad AFTER DELETE ON transcript_chunks BEGIN INSERT INTO transcript_chunks_fts(transcript_chunks_fts,rowid,text) VALUES('delete',old.rowid,old.text); END;
CREATE TRIGGER chunks_au AFTER UPDATE ON transcript_chunks BEGIN
 INSERT INTO transcript_chunks_fts(transcript_chunks_fts,rowid,text) VALUES('delete',old.rowid,old.text);
 INSERT INTO transcript_chunks_fts(rowid,text) VALUES(new.rowid,new.text); END;
CREATE VIRTUAL TABLE source_metadata_fts USING fts5(title,description,creator_name,content='sources',content_rowid='rowid',tokenize='unicode61');
CREATE TRIGGER sources_ai AFTER INSERT ON sources BEGIN INSERT INTO source_metadata_fts(rowid,title,description,creator_name) VALUES(new.rowid,new.title,new.description,new.creator_name); END;
CREATE TRIGGER sources_ad AFTER DELETE ON sources BEGIN INSERT INTO source_metadata_fts(source_metadata_fts,rowid,title,description,creator_name) VALUES('delete',old.rowid,old.title,old.description,old.creator_name); END;
CREATE TRIGGER sources_au AFTER UPDATE ON sources BEGIN
 INSERT INTO source_metadata_fts(source_metadata_fts,rowid,title,description,creator_name) VALUES('delete',old.rowid,old.title,old.description,old.creator_name);
 INSERT INTO source_metadata_fts(rowid,title,description,creator_name) VALUES(new.rowid,new.title,new.description,new.creator_name); END;
CREATE TABLE embeddings (
 id TEXT PRIMARY KEY,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,source_id TEXT NOT NULL REFERENCES sources(id),
 modality TEXT NOT NULL,model_name TEXT NOT NULL,model_revision TEXT NOT NULL,dimensions INTEGER NOT NULL,
 vector_json TEXT NOT NULL,input_hash TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(entity_type,entity_id,modality,model_name,model_revision,input_hash)
);
CREATE TABLE search_runs(id TEXT PRIMARY KEY,query TEXT NOT NULL,filters_json TEXT NOT NULL,model_versions_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE candidates (
 id TEXT PRIMARY KEY,search_id TEXT NOT NULL REFERENCES search_runs(id),source_id TEXT NOT NULL REFERENCES sources(id),
 start_ms INTEGER,end_ms INTEGER,evidence TEXT NOT NULL,evidence_type TEXT NOT NULL,evidence_ids_json TEXT NOT NULL DEFAULT '[]',
 evidence_hash TEXT NOT NULL,scores_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'ranked',validation_json TEXT,
 created_at TEXT NOT NULL,CHECK((start_ms IS NULL AND end_ms IS NULL) OR (start_ms>=0 AND end_ms>start_ms))
);
CREATE TABLE policies (
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),rights_status TEXT NOT NULL,permitted_use TEXT NOT NULL,
 reason TEXT NOT NULL,reviewed_by TEXT NOT NULL,expires_at TEXT,version INTEGER NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(source_id,version)
);
CREATE TABLE approvals (
 id TEXT PRIMARY KEY,candidate_id TEXT NOT NULL REFERENCES candidates(id),source_id TEXT NOT NULL REFERENCES sources(id),
 requested_use TEXT NOT NULL,start_ms INTEGER,end_ms INTEGER,reviewed_by TEXT NOT NULL,policy_id TEXT NOT NULL REFERENCES policies(id),
 policy_version INTEGER NOT NULL,evidence_hash TEXT NOT NULL,metadata_hash TEXT,expires_at TEXT,created_at TEXT NOT NULL,
 CHECK((start_ms IS NULL AND end_ms IS NULL) OR (start_ms>=0 AND end_ms>start_ms))
);
CREATE TABLE jobs (
 id TEXT PRIMARY KEY,job_type TEXT NOT NULL,idempotency_key TEXT NOT NULL UNIQUE,payload_json TEXT NOT NULL,status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0,max_attempts INTEGER NOT NULL DEFAULT 3,locked_at TEXT,locked_by TEXT,lease_until TEXT,
 available_at TEXT NOT NULL,last_error TEXT,result_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE events (
 id TEXT PRIMARY KEY,source_id TEXT REFERENCES sources(id),artifact_id TEXT,job_id TEXT REFERENCES jobs(id),event_type TEXT NOT NULL,
 level TEXT NOT NULL DEFAULT 'info',payload_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL
);
CREATE TABLE artifacts (
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),kind TEXT NOT NULL,profile TEXT NOT NULL DEFAULT '',
 path TEXT NOT NULL,sha256 TEXT NOT NULL,bytes INTEGER NOT NULL,metadata_json TEXT NOT NULL DEFAULT '{}',
 parent_artifact_id TEXT REFERENCES artifacts(id),start_ms INTEGER,end_ms INTEGER,approval_id TEXT REFERENCES approvals(id),created_at TEXT NOT NULL
);
CREATE TABLE clip_provenance (
 id TEXT PRIMARY KEY,clip_id TEXT NOT NULL REFERENCES artifacts(id),source_id TEXT NOT NULL REFERENCES sources(id),
 candidate_id TEXT REFERENCES candidates(id),approval_id TEXT NOT NULL REFERENCES approvals(id),source_start_ms INTEGER NOT NULL,
 source_end_ms INTEGER NOT NULL,requested_use TEXT NOT NULL,rights_status TEXT NOT NULL,manifest_artifact_id TEXT REFERENCES artifacts(id),
 metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL
);
CREATE TABLE attachments(id TEXT PRIMARY KEY,artifact_id TEXT NOT NULL REFERENCES artifacts(id),task_id TEXT,local_path TEXT NOT NULL,sha256 TEXT NOT NULL,created_at TEXT NOT NULL,UNIQUE(artifact_id,task_id,local_path));
CREATE TABLE visual_frames (
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),artifact_id TEXT REFERENCES artifacts(id),timestamp_ms INTEGER NOT NULL,
 start_ms INTEGER,end_ms INTEGER,path TEXT NOT NULL,sha256 TEXT NOT NULL,perceptual_hash TEXT,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
 UNIQUE(source_id,timestamp_ms,sha256)
);
CREATE TABLE ocr_blocks (
 id TEXT PRIMARY KEY,source_id TEXT NOT NULL REFERENCES sources(id),frame_id TEXT REFERENCES visual_frames(id),start_ms INTEGER NOT NULL,
 end_ms INTEGER NOT NULL,text TEXT NOT NULL,confidence REAL,model_name TEXT NOT NULL,model_revision TEXT NOT NULL,created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE ocr_fts USING fts5(text,content='ocr_blocks',content_rowid='rowid',tokenize='unicode61');
CREATE TRIGGER ocr_ai AFTER INSERT ON ocr_blocks BEGIN INSERT INTO ocr_fts(rowid,text) VALUES(new.rowid,new.text); END;
CREATE TRIGGER ocr_ad AFTER DELETE ON ocr_blocks BEGIN INSERT INTO ocr_fts(ocr_fts,rowid,text) VALUES('delete',old.rowid,old.text); END;
CREATE TRIGGER ocr_au AFTER UPDATE ON ocr_blocks BEGIN INSERT INTO ocr_fts(ocr_fts,rowid,text) VALUES('delete',old.rowid,old.text); INSERT INTO ocr_fts(rowid,text) VALUES(new.rowid,new.text); END;
CREATE INDEX chunks_source_time ON transcript_chunks(source_id,start_ms,end_ms);
CREATE INDEX candidates_source ON candidates(source_id,created_at);
CREATE INDEX policies_source ON policies(source_id,version);
CREATE INDEX approvals_scope ON approvals(candidate_id,requested_use,start_ms,end_ms);
CREATE INDEX jobs_available ON jobs(status,available_at,lease_until);
CREATE INDEX artifacts_source ON artifacts(source_id,kind);
CREATE INDEX embeddings_source ON embeddings(source_id,modality);
CREATE INDEX events_source ON events(source_id,created_at);
