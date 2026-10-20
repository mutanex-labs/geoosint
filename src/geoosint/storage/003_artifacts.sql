CREATE TABLE artifacts(
 id TEXT PRIMARY KEY, case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
 owner TEXT NOT NULL, created_at TEXT NOT NULL, sha256 TEXT NOT NULL,
 size_bytes INTEGER NOT NULL, detected_format TEXT NOT NULL);
CREATE INDEX artifacts_owner_case ON artifacts(owner,case_id);
UPDATE schema_version SET version=3 WHERE version=2;
