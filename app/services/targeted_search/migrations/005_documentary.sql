CREATE TABLE case_documentaries (
 id TEXT PRIMARY KEY,case_id TEXT NOT NULL REFERENCES cases(id),title TEXT NOT NULL,status TEXT NOT NULL,
 revision INTEGER NOT NULL,content_hash TEXT NOT NULL,record_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 CHECK(revision>0)
);
CREATE TABLE documentary_revisions (
 id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES case_documentaries(id),case_id TEXT NOT NULL REFERENCES cases(id),
 revision INTEGER NOT NULL,content_hash TEXT NOT NULL,record_json TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(document_id,revision)
);
CREATE TABLE documentary_exports (
 id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES case_documentaries(id),case_id TEXT NOT NULL REFERENCES cases(id),
 revision INTEGER NOT NULL,final INTEGER NOT NULL,record_json TEXT NOT NULL,created_at TEXT NOT NULL,
 UNIQUE(document_id,revision,final)
);
CREATE INDEX case_documentaries_case ON case_documentaries(case_id,updated_at);
