CREATE TABLE cases (
 id TEXT PRIMARY KEY,collection_id TEXT NOT NULL UNIQUE REFERENCES collections(id),name TEXT NOT NULL,topic TEXT NOT NULL DEFAULT '',
 metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL
);
CREATE TABLE case_assets (
 id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),source_id TEXT NOT NULL REFERENCES sources(id),artifact_id TEXT REFERENCES artifacts(id),
 asset_kind TEXT NOT NULL,category TEXT NOT NULL DEFAULT '',relative_path TEXT NOT NULL,filename TEXT NOT NULL,sha256 TEXT,
 state TEXT NOT NULL DEFAULT 'imported',metadata_json TEXT NOT NULL DEFAULT '{}',version INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 UNIQUE(case_id,relative_path),CHECK(version>0)
);
CREATE TABLE case_asset_versions (
 id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES case_assets(id),artifact_id TEXT REFERENCES artifacts(id),sha256 TEXT,
 version INTEGER NOT NULL,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,UNIQUE(asset_id,version)
);
CREATE TABLE evidence_units (
 id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),asset_id TEXT NOT NULL REFERENCES case_assets(id),asset_version_id TEXT NOT NULL REFERENCES case_asset_versions(id),
 source_id TEXT NOT NULL REFERENCES sources(id),artifact_id TEXT REFERENCES artifacts(id),unit_kind TEXT NOT NULL,text TEXT NOT NULL,
 locator_type TEXT NOT NULL,locator_json TEXT NOT NULL,content_hash TEXT NOT NULL,origin TEXT NOT NULL,confidence REAL,metadata_json TEXT NOT NULL DEFAULT '{}',
 is_active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,UNIQUE(asset_version_id,content_hash),CHECK(confidence IS NULL OR (confidence>=0 AND confidence<=1))
);
CREATE VIRTUAL TABLE evidence_units_fts USING fts5(text,content='evidence_units',content_rowid='rowid',tokenize='unicode61');
CREATE TRIGGER evidence_units_ai AFTER INSERT ON evidence_units BEGIN INSERT INTO evidence_units_fts(rowid,text) VALUES(new.rowid,new.text); END;
CREATE TRIGGER evidence_units_ad AFTER DELETE ON evidence_units BEGIN INSERT INTO evidence_units_fts(evidence_units_fts,rowid,text) VALUES('delete',old.rowid,old.text); END;
CREATE TRIGGER evidence_units_au AFTER UPDATE ON evidence_units BEGIN
 INSERT INTO evidence_units_fts(evidence_units_fts,rowid,text) VALUES('delete',old.rowid,old.text);
 INSERT INTO evidence_units_fts(rowid,text) VALUES(new.rowid,new.text); END;
CREATE TABLE document_pages (
 id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES case_assets(id),asset_version_id TEXT NOT NULL REFERENCES case_asset_versions(id),
 page_index INTEGER NOT NULL,page_label TEXT,text TEXT NOT NULL,origin TEXT NOT NULL,confidence REAL,render_artifact_id TEXT REFERENCES artifacts(id),
 metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,UNIQUE(asset_version_id,page_index),CHECK(page_index>=0)
);
CREATE TABLE transcript_words (
 id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES case_assets(id),asset_version_id TEXT NOT NULL REFERENCES case_asset_versions(id),
 transcript_artifact_id TEXT NOT NULL REFERENCES artifacts(id),word_index INTEGER NOT NULL,text TEXT NOT NULL,start_ms INTEGER,end_ms INTEGER,
 speaker TEXT,channel TEXT,confidence REAL,alignment_confidence REAL,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,
 UNIQUE(asset_version_id,transcript_artifact_id,word_index),CHECK(word_index>=0),CHECK((start_ms IS NULL AND end_ms IS NULL) OR (start_ms>=0 AND end_ms>=start_ms))
);
CREATE TABLE case_transcripts (
 id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES case_assets(id),asset_version_id TEXT NOT NULL REFERENCES case_asset_versions(id),
 transcript_artifact_id TEXT REFERENCES artifacts(id),scope TEXT NOT NULL DEFAULT 'source',record_json TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(asset_version_id,transcript_artifact_id,scope)
);
CREATE TABLE case_derivatives (
 id TEXT PRIMARY KEY,asset_id TEXT NOT NULL REFERENCES case_assets(id),asset_version_id TEXT NOT NULL REFERENCES case_asset_versions(id),
 artifact_id TEXT NOT NULL REFERENCES artifacts(id),kind TEXT NOT NULL,metadata_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,UNIQUE(asset_version_id,artifact_id,kind)
);
CREATE TABLE case_requests (id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),title TEXT NOT NULL,status TEXT NOT NULL,record_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE case_claims (id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),text TEXT NOT NULL,status TEXT NOT NULL,record_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE case_events (id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),title TEXT NOT NULL,event_at TEXT,record_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE case_entities (id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),entity_type TEXT NOT NULL,name TEXT NOT NULL,record_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE case_mentions (id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),entity_id TEXT NOT NULL REFERENCES case_entities(id),unit_id TEXT NOT NULL REFERENCES evidence_units(id),char_start INTEGER,char_end INTEGER,record_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE case_citations (
 id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),record_type TEXT NOT NULL,record_id TEXT NOT NULL,asset_id TEXT NOT NULL REFERENCES case_assets(id),
 asset_version_id TEXT NOT NULL REFERENCES case_asset_versions(id),unit_id TEXT REFERENCES evidence_units(id),locator_json TEXT NOT NULL,quote TEXT,relation TEXT NOT NULL,
 evidence_hash TEXT,created_at TEXT NOT NULL
);
CREATE TABLE case_storyboards (id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),title TEXT NOT NULL,record_json TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE case_record_versions (id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),record_type TEXT NOT NULL,record_id TEXT NOT NULL,record_json TEXT NOT NULL,content_hash TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX case_assets_source ON case_assets(source_id,case_id);
CREATE INDEX evidence_units_case ON evidence_units(case_id,is_active,asset_id);
CREATE INDEX case_citations_record ON case_citations(record_type,record_id);
CREATE INDEX case_requests_case ON case_requests(case_id,status);
CREATE INDEX case_claims_case ON case_claims(case_id,status);
CREATE INDEX case_events_case ON case_events(case_id,event_at);
