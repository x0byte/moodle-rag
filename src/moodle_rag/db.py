"""SQLite storage: one row per document, one row per extracted page/slide."""

import sqlite3
from pathlib import Path

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    source_url    TEXT NOT NULL UNIQUE,   -- the Moodle link the file was found at
    resolved_url  TEXT,                   -- where it actually downloaded from (pluginfile.php)
    unit_code     TEXT NOT NULL,
    unit_name     TEXT,
    section       TEXT,                   -- section/topic title as shown on the course page
    week          INTEGER,                -- parsed from the section title when it names a week
    title         TEXT NOT NULL,
    resource_type TEXT,                   -- Moodle module type: resource, folder, page, ...
    filename      TEXT,
    file_type     TEXT,                   -- pdf, pptx, docx, html
    sha256        TEXT NOT NULL,
    raw_path      TEXT,                   -- relative to the data dir; NULL if the file was not kept
    status        TEXT NOT NULL,          -- ok | no_text | unsupported | error
    status_detail TEXT,
    page_count    INTEGER NOT NULL DEFAULT 0,
    ingested_at   TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_documents_unit ON documents (unit_code, week);

CREATE TABLE IF NOT EXISTS pages (
    doc_id  INTEGER NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    page_no INTEGER NOT NULL,             -- 1-based page or slide number
    text    TEXT NOT NULL,
    PRIMARY KEY (doc_id, page_no)
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init(path: Path | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
