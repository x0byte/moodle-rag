"""SQLite storage.

A document is one distinct file, identified by its content hash. The same file
linked from several places (two weeks, a folder and a section) is one document
with several locations. Each document has one row per extracted page/slide.
"""

import logging
import sqlite3
from pathlib import Path

import sqlite_vec

from . import config

log = logging.getLogger("moodle_rag.db")

SCHEMA_VERSION = 5

DOCUMENTS = """
CREATE TABLE IF NOT EXISTS {name} (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    title         TEXT NOT NULL,
    filename      TEXT,
    file_type     TEXT,                   -- pdf, pptx, docx, html
    raw_path      TEXT,                   -- relative to the data dir; NULL if the file was not kept
    status        TEXT NOT NULL,          -- ok | no_text | unsupported | error
    status_detail TEXT,
    page_count    INTEGER NOT NULL DEFAULT 0,
    ingested_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

SCHEMA = DOCUMENTS.format(name="documents") + """
CREATE TABLE IF NOT EXISTS locations (
    id            INTEGER PRIMARY KEY,
    doc_id        INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    source_url    TEXT NOT NULL UNIQUE,   -- the Moodle link the file was found at
    resolved_url  TEXT,                   -- where it actually downloaded from (pluginfile.php)
    unit_code     TEXT NOT NULL,
    unit_name     TEXT,
    section       TEXT,                   -- section/topic title as shown on the course page
    week          INTEGER,                -- parsed from the section title when it names a week
    title         TEXT,                   -- the link's title in this location
    resource_type TEXT,                   -- Moodle module type: resource, folder, page, ...
    scope         TEXT,                   -- where a scan found it: 'index' or 'page:<path>'
    first_seen    TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_locations_doc ON locations (doc_id);
CREATE INDEX IF NOT EXISTS idx_locations_unit ON locations (unit_code, week);

-- Size of the last accepted scan of each unit, to spot a scan that came up short.
CREATE TABLE IF NOT EXISTS scans (
    unit_code      TEXT PRIMARY KEY,
    resource_count INTEGER NOT NULL,
    finished_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS pages (
    doc_id  INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,             -- 1-based page or slide number
    text    TEXT NOT NULL,
    PRIMARY KEY (doc_id, page_no)
);

-- Search index. Vectors are float32 blobs compared with sqlite-vec's
-- vec_distance_cosine; chunks_fts mirrors chunks through the triggers below.
CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY,
    doc_id     INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,          -- position within the document
    page_start INTEGER NOT NULL,
    page_end   INTEGER NOT NULL,
    text       TEXT NOT NULL,
    context    TEXT NOT NULL,             -- unit | section | title | filename
    tokens     INTEGER NOT NULL,
    embedding  BLOB NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks (doc_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5 (
    text, title, tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_fts_insert AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, text, title) VALUES (new.id, new.text, new.context);
END;

CREATE TRIGGER IF NOT EXISTS chunks_fts_delete AFTER DELETE ON chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
END;
"""

# v1 kept the location columns on documents, one document per URL. Split them
# out, merging documents that share a content hash into the oldest one.
MIGRATE_V1 = """
PRAGMA foreign_keys = OFF;
BEGIN;
""" + DOCUMENTS.format(name="documents_v2") + """
INSERT INTO documents_v2
    (id, sha256, title, filename, file_type, raw_path, status, status_detail,
     page_count, ingested_at, updated_at)
SELECT id, sha256, title, filename, file_type, raw_path, status, status_detail,
       page_count, ingested_at, updated_at
FROM documents
WHERE id IN (SELECT MIN(id) FROM documents GROUP BY sha256);

CREATE TABLE locations_v2 AS
SELECT (SELECT MIN(k.id) FROM documents k WHERE k.sha256 = d.sha256) AS doc_id,
       d.source_url, d.resolved_url, d.unit_code, d.unit_name, d.section, d.week,
       d.title, d.resource_type, d.ingested_at AS first_seen, d.updated_at AS last_seen
FROM documents d;

DELETE FROM pages WHERE doc_id NOT IN (SELECT id FROM documents_v2);
DROP TABLE documents;
ALTER TABLE documents_v2 RENAME TO documents;
COMMIT;
PRAGMA foreign_keys = ON;
"""

MIGRATE_V1_LOCATIONS = """
INSERT INTO locations
    (doc_id, source_url, resolved_url, unit_code, unit_name, section, week,
     title, resource_type, first_seen, last_seen)
SELECT doc_id, source_url, resolved_url, unit_code, unit_name, section, week,
       title, resource_type, first_seen, last_seen
FROM locations_v2;
DROP TABLE locations_v2;
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn


def init(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(documents)")}
        is_v1 = "source_url" in columns
        if is_v1:
            log.info("migrating database to schema v%d", SCHEMA_VERSION)
            conn.executescript(MIGRATE_V1)
        conn.executescript(SCHEMA)
        if is_v1:
            conn.executescript(MIGRATE_V1_LOCATIONS)
        if "scope" not in {row["name"] for row in conn.execute("PRAGMA table_info(locations)")}:
            conn.execute("ALTER TABLE locations ADD COLUMN scope TEXT")  # v2 -> v3
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()
