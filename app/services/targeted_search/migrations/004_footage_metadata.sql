-- Supporting assets never change the footage metadata index's term statistics.
CREATE VIRTUAL TABLE footage_metadata_fts USING fts5(title,description,creator_name,content='sources',content_rowid='rowid',tokenize='unicode61');
INSERT INTO footage_metadata_fts(rowid,title,description,creator_name) SELECT rowid,title,description,creator_name FROM sources WHERE coalesce(json_extract(metadata_json,'$.asset_kind'),'video')='video';
CREATE TRIGGER footage_sources_ai AFTER INSERT ON sources WHEN coalesce(json_extract(new.metadata_json,'$.asset_kind'),'video')='video' BEGIN
 INSERT INTO footage_metadata_fts(rowid,title,description,creator_name) VALUES(new.rowid,new.title,new.description,new.creator_name);
END;
CREATE TRIGGER footage_sources_ad AFTER DELETE ON sources WHEN coalesce(json_extract(old.metadata_json,'$.asset_kind'),'video')='video' BEGIN
 INSERT INTO footage_metadata_fts(footage_metadata_fts,rowid,title,description,creator_name) VALUES('delete',old.rowid,old.title,old.description,old.creator_name);
END;
CREATE TRIGGER footage_sources_au AFTER UPDATE ON sources BEGIN
 INSERT INTO footage_metadata_fts(footage_metadata_fts,rowid,title,description,creator_name) SELECT 'delete',old.rowid,old.title,old.description,old.creator_name WHERE coalesce(json_extract(old.metadata_json,'$.asset_kind'),'video')='video';
 INSERT INTO footage_metadata_fts(rowid,title,description,creator_name) SELECT new.rowid,new.title,new.description,new.creator_name WHERE coalesce(json_extract(new.metadata_json,'$.asset_kind'),'video')='video';
END;
