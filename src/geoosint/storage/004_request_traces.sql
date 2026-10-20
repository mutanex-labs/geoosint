CREATE TABLE request_traces(
 id TEXT PRIMARY KEY,
 case_id TEXT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
 owner TEXT NOT NULL,
 created_at TEXT NOT NULL,
 content TEXT NOT NULL
);
CREATE INDEX request_traces_case ON request_traces(case_id,created_at);
UPDATE schema_version SET version=4 WHERE version=3;
