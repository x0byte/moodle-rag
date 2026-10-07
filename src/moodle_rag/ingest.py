"""Ingest pipeline: store the raw file, extract its text, record it in SQLite."""

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import config
from .extract import EXTRACTORS, detect_file_type
from .index import index_document

log = logging.getLogger("moodle_rag.ingest")

WEEK_RE = re.compile(r"\bw(?:ee)?k\s*0*(\d{1,2})\b", re.IGNORECASE)

# Statuses that mean the file has been dealt with and need not be extracted again.
DONE = ("ok", "no_text")

# One ingest at a time: the extension uploads in parallel, and two copies of the
# same file arriving together must not both create a document.
_lock = threading.Lock()


@dataclass
class Metadata:
    source_url: str
    unit_code: str
    title: str
    unit_name: str | None = None
    section: str | None = None
    week: int | None = None
    resolved_url: str | None = None
    resource_type: str | None = None
    scope: str | None = None


@dataclass
class Result:
    doc_id: int
    action: str  # created | updated | linked | unchanged
    status: str  # ok | no_text | unsupported | error
    pages: int = 0
    chunks: int = 0
    detail: str | None = None
    # Other places the same file appears, e.g. ["Week 3: Replication"].
    also_in: list[str] = field(default_factory=list)


def parse_week(section: str | None) -> int | None:
    match = WEEK_RE.search(section or "")
    return int(match.group(1)) if match else None


def safe_name(value: str, fallback: str) -> str:
    name = re.sub(r"[^\w.\- ]+", "_", value).strip(" .")
    return name[:150] or fallback


def label(meta: Metadata, filename: str) -> str:
    week = f" W{meta.week}" if meta.week is not None else ""
    return f"{meta.unit_code}{week} {filename}"


def ingest_file(
    conn: sqlite3.Connection,
    upload_path: Path,
    sha256: str,
    filename: str,
    content_type: str | None,
    meta: Metadata,
) -> Result:
    """Ingest one uploaded file. Takes ownership of upload_path (moves or deletes it)."""
    try:
        with _lock:
            return _ingest(conn, upload_path, sha256, filename, content_type, meta)
    finally:
        upload_path.unlink(missing_ok=True)


def _extract(extractor, path: Path, file_type: str, filename: str):
    """Returns (pages, page_count, status, detail)."""
    if extractor is None:
        return [], 0, "unsupported", f"no extractor for '{file_type or 'unknown'}' files"
    try:
        pages = extractor(path)
    except Exception as error:  # a broken file must not take the server down
        # Libraries quote the temporary upload path; show the file's own name.
        message = str(error).replace(str(path), filename)
        return [], 0, "error", f"{type(error).__name__}: {message}"
    if sum(len("".join(page.text.split())) for page in pages) < config.MIN_TEXT_CHARS:
        detail = "no text layer (scanned?)" if file_type == "pdf" else "no text"
        return [], len(pages), "no_text", detail
    return pages, len(pages), "ok", None


def _delete_document(conn: sqlite3.Connection, doc_id: int) -> None:
    row = conn.execute("SELECT raw_path FROM documents WHERE id = ?", (doc_id,)).fetchone()
    conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
    if row and row["raw_path"]:
        (config.DATA_DIR / row["raw_path"]).unlink(missing_ok=True)


def _ingest(conn, upload_path, sha256, filename, content_type, meta) -> Result:
    filename = safe_name(filename, "file")
    meta.unit_code = safe_name(meta.unit_code.upper(), "UNKNOWN")
    if meta.week is None:
        meta.week = parse_week(meta.section)
    name = label(meta, filename)

    with upload_path.open("rb") as handle:
        file_type = detect_file_type(filename, content_type, handle.read(8))
    extractor = EXTRACTORS.get(file_type)

    location = conn.execute(
        "SELECT doc_id FROM locations WHERE source_url = ?", (meta.source_url,)
    ).fetchone()
    previous_doc_id = location["doc_id"] if location else None
    doc = conn.execute("SELECT * FROM documents WHERE sha256 = ?", (sha256,)).fetchone()

    extracted = False
    chunk_count = 0
    with conn:
        # A known file that was skipped as unsupported (or failed) gets another
        # go once an extractor for its type exists.
        if doc and (doc["status"] in DONE or extractor is None):
            doc_id, status, detail = doc["id"], doc["status"], doc["status_detail"]
            page_count = doc["page_count"]
            action = "unchanged" if previous_doc_id == doc_id else "linked"
        else:
            extracted = True
            pages, page_count, status, detail = _extract(
                extractor, upload_path, file_type, filename
            )
            fields = {
                "sha256": sha256,
                "title": meta.title,
                "filename": filename,
                "file_type": file_type,
                "status": status,
                "status_detail": detail,
                "page_count": page_count,
            }
            # The file at this URL changed: update its document in place, unless
            # other locations still point at the old content.
            reuse_id = doc["id"] if doc else None
            if reuse_id is None and previous_doc_id is not None:
                shared = conn.execute(
                    "SELECT COUNT(*) FROM locations WHERE doc_id = ?", (previous_doc_id,)
                ).fetchone()[0]
                if shared == 1:
                    reuse_id = previous_doc_id

            if reuse_id is not None:
                doc_id, action = reuse_id, "updated"
                old = conn.execute(
                    "SELECT raw_path FROM documents WHERE id = ?", (doc_id,)
                ).fetchone()
                old_raw = config.DATA_DIR / old["raw_path"] if old["raw_path"] else None
                assignments = ", ".join(f"{column} = :{column}" for column in fields)
                conn.execute(
                    f"UPDATE documents SET {assignments}, updated_at = datetime('now')"
                    " WHERE id = :id",
                    {**fields, "id": doc_id},
                )
                conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
            else:
                action, old_raw = "created", None
                columns = ", ".join(fields)
                placeholders = ", ".join(f":{column}" for column in fields)
                doc_id = conn.execute(
                    f"INSERT INTO documents ({columns}) VALUES ({placeholders})", fields
                ).lastrowid

            # Unsupported files (videos etc.) are not kept on disk.
            raw_path = None
            if status != "unsupported":
                target = config.RAW_DIR / meta.unit_code / f"{doc_id}_{filename}"
                target.parent.mkdir(parents=True, exist_ok=True)
                upload_path.replace(target)
                raw_path = str(target.relative_to(config.DATA_DIR))
            if old_raw and (raw_path is None or old_raw != config.DATA_DIR / raw_path):
                old_raw.unlink(missing_ok=True)

            conn.execute("UPDATE documents SET raw_path = ? WHERE id = ?", (raw_path, doc_id))
            conn.executemany(
                "INSERT INTO pages (doc_id, page_no, text) VALUES (?, ?, ?)",
                [(doc_id, page.number, page.text) for page in pages],
            )

        conn.execute(
            "INSERT INTO locations (doc_id, source_url, resolved_url, unit_code, unit_name,"
            " section, week, title, resource_type, scope)"
            " VALUES (:doc_id, :source_url, :resolved_url, :unit_code, :unit_name,"
            " :section, :week, :title, :resource_type, :scope)"
            " ON CONFLICT (source_url) DO UPDATE SET"
            " doc_id = excluded.doc_id, resolved_url = excluded.resolved_url,"
            " unit_code = excluded.unit_code, unit_name = excluded.unit_name,"
            " section = excluded.section, week = excluded.week, title = excluded.title,"
            " resource_type = excluded.resource_type, scope = excluded.scope,"
            " last_seen = datetime('now')",
            {
                "doc_id": doc_id,
                "source_url": meta.source_url,
                "resolved_url": meta.resolved_url,
                "unit_code": meta.unit_code,
                "unit_name": meta.unit_name,
                "section": meta.section,
                "week": meta.week,
                "title": meta.title,
                "resource_type": meta.resource_type,
                "scope": meta.scope,
            },
        )

        # After the location is stored: a chunk's context names its unit and section.
        if extracted:
            chunk_count = index_document(conn, doc_id)

        # This URL used to point at a different document; drop it if nothing else does.
        if previous_doc_id is not None and previous_doc_id != doc_id:
            remaining = conn.execute(
                "SELECT COUNT(*) FROM locations WHERE doc_id = ?", (previous_doc_id,)
            ).fetchone()[0]
            if remaining == 0:
                _delete_document(conn, previous_doc_id)

        also_in = [
            f"{row['unit_code']} {row['section'] or 'no section'}"
            for row in conn.execute(
                "SELECT unit_code, section FROM locations"
                " WHERE doc_id = ? AND source_url != ? ORDER BY week, section",
                (doc_id, meta.source_url),
            )
        ]

    if action == "unchanged":
        log.info("unchanged %s", name)
    elif action == "linked":
        log.info("linked %s: same file as doc %d (%s)", name, doc_id, "; ".join(also_in))
    elif status == "ok":
        verb = "re-ingested" if action == "updated" else "ingested"
        log.info("%s %s: %d pages, %d chunks", verb, name, page_count, chunk_count)
    elif status == "no_text":
        log.warning("no text extracted %s: %d pages, %s", name, page_count, detail)
    elif status == "unsupported":
        log.info("skipped %s: %s", name, detail)
    else:
        log.error("failed %s: %s", name, detail)
    pages_stored = page_count if status == "ok" else 0
    return Result(doc_id, action, status, pages_stored, chunk_count, detail, also_in)


def reindex(conn: sqlite3.Connection, extract: bool = False) -> None:
    """Rebuild the search index for every document.

    With extract=True the text is first extracted again from the raw files,
    which picks up extractor improvements without another sync.
    """
    docs = conn.execute("SELECT * FROM documents ORDER BY id").fetchall()
    total = 0
    for doc in docs:
        raw = config.DATA_DIR / doc["raw_path"] if doc["raw_path"] else None
        with _lock, conn:
            if extract and raw and raw.exists() and doc["file_type"] in EXTRACTORS:
                pages, page_count, status, detail = _extract(
                    EXTRACTORS[doc["file_type"]], raw, doc["file_type"], doc["filename"]
                )
                conn.execute(
                    "UPDATE documents SET status = ?, status_detail = ?, page_count = ?"
                    " WHERE id = ?",
                    (status, detail, page_count, doc["id"]),
                )
                conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc["id"],))
                conn.executemany(
                    "INSERT INTO pages (doc_id, page_no, text) VALUES (?, ?, ?)",
                    [(doc["id"], page.number, page.text) for page in pages],
                )
                if status != doc["status"]:
                    log.info("%s: %s -> %s", doc["filename"], doc["status"], status)
            chunks = index_document(conn, doc["id"])
        total += chunks
        log.info("indexed %s: %d chunks", doc["filename"], chunks)
    log.info("reindexed %d documents: %d chunks", len(docs), total)


def prune(conn: sqlite3.Connection, unit_code: str, scopes: list[str], seen: list[str]) -> list[str]:
    """Remove locations that a scan covered but no longer found on Moodle.

    Only locations recorded under one of the scanned scopes are candidates, so
    a scan of part of a course never removes anything it did not look at.
    Documents left with no location are deleted. Returns what was removed.

    A scan that found nothing, or far less than the unit's last accepted scan,
    is taken to be incomplete (a half-loaded page): nothing is removed.
    """
    unit_code = safe_name(unit_code.upper(), "UNKNOWN")
    seen_urls = set(seen)
    removed = []
    with _lock, conn:
        last = conn.execute(
            "SELECT resource_count FROM scans WHERE unit_code = ?", (unit_code,)
        ).fetchone()
        previous = last["resource_count"] if last else 0
        if not seen_urls or len(seen_urls) < previous * config.PRUNE_MIN_RATIO:
            log.warning(
                "prune skipped %s: scan found %d resources, last scan found %d;"
                " nothing removed",
                unit_code, len(seen_urls), previous,
            )
            return removed
        conn.execute(
            "INSERT INTO scans (unit_code, resource_count) VALUES (?, ?)"
            " ON CONFLICT (unit_code) DO UPDATE SET"
            " resource_count = excluded.resource_count, finished_at = datetime('now')",
            (unit_code, len(seen_urls)),
        )
        for scope in scopes:
            rows = conn.execute(
                "SELECT l.id, l.doc_id, l.source_url, l.week, d.filename FROM locations l"
                " JOIN documents d ON d.id = l.doc_id WHERE l.unit_code = ? AND l.scope = ?",
                (unit_code, scope),
            ).fetchall()
            for row in rows:
                if row["source_url"] in seen_urls:
                    continue
                conn.execute("DELETE FROM locations WHERE id = ?", (row["id"],))
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM locations WHERE doc_id = ?", (row["doc_id"],)
                ).fetchone()[0]
                if remaining == 0:
                    _delete_document(conn, row["doc_id"])
                week = f" W{row['week']}" if row["week"] is not None else ""
                name = f"{unit_code}{week} {row['filename']}"
                log.info("removed %s: no longer on Moodle", name)
                removed.append(name)
    return removed
