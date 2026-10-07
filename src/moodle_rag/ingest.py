"""Ingest pipeline: store the raw file, extract its text, record it in SQLite."""

import logging
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from . import config
from .extract import EXTRACTORS, detect_file_type

log = logging.getLogger("moodle_rag.ingest")

WEEK_RE = re.compile(r"\bw(?:ee)?k\s*0*(\d{1,2})\b", re.IGNORECASE)


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


@dataclass
class Result:
    doc_id: int
    action: str  # created | updated | unchanged
    status: str  # ok | no_text | unsupported | error
    pages: int = 0
    detail: str | None = None


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
        return _ingest(conn, upload_path, sha256, filename, content_type, meta)
    finally:
        upload_path.unlink(missing_ok=True)


def _ingest(conn, upload_path, sha256, filename, content_type, meta) -> Result:
    filename = safe_name(filename, "file")
    meta.unit_code = safe_name(meta.unit_code.upper(), "UNKNOWN")
    if meta.week is None:
        meta.week = parse_week(meta.section)
    name = label(meta, filename)

    with upload_path.open("rb") as handle:
        file_type = detect_file_type(filename, content_type, handle.read(8))

    existing = conn.execute(
        "SELECT id, sha256, status, raw_path, page_count FROM documents WHERE source_url = ?",
        (meta.source_url,),
    ).fetchone()

    extractor = EXTRACTORS.get(file_type)
    unchanged = existing is not None and existing["sha256"] == sha256
    # An unchanged file that was skipped as unsupported gets another go once
    # an extractor for its type exists.
    if unchanged and (existing["status"] in ("ok", "no_text") or extractor is None):
        log.info("unchanged %s", name)
        return Result(existing["id"], "unchanged", existing["status"], existing["page_count"])

    pages, status, detail = [], "ok", None
    if extractor is None:
        status, detail = "unsupported", f"no extractor for '{file_type or 'unknown'}' files"
    else:
        try:
            pages = extractor(upload_path)
        except Exception as error:  # a broken file must not take the server down
            status, detail = "error", f"{type(error).__name__}: {error}"
        else:
            if sum(len("".join(page.text.split())) for page in pages) < config.MIN_TEXT_CHARS:
                status, detail = "no_text", "no text layer (scanned?)"
    page_count = len(pages)
    if status != "ok":
        pages = []

    fields = {
        "resolved_url": meta.resolved_url,
        "unit_code": meta.unit_code,
        "unit_name": meta.unit_name,
        "section": meta.section,
        "week": meta.week,
        "title": meta.title,
        "resource_type": meta.resource_type,
        "filename": filename,
        "file_type": file_type,
        "sha256": sha256,
        "status": status,
        "status_detail": detail,
        "page_count": page_count,
    }

    old_raw = config.DATA_DIR / existing["raw_path"] if existing and existing["raw_path"] else None
    with conn:
        if existing:
            doc_id = existing["id"]
            assignments = ", ".join(f"{column} = :{column}" for column in fields)
            conn.execute(
                f"UPDATE documents SET {assignments}, updated_at = datetime('now') WHERE id = :id",
                {**fields, "id": doc_id},
            )
            conn.execute("DELETE FROM pages WHERE doc_id = ?", (doc_id,))
        else:
            columns = ", ".join(["source_url", *fields])
            placeholders = ", ".join(f":{column}" for column in ["source_url", *fields])
            doc_id = conn.execute(
                f"INSERT INTO documents ({columns}) VALUES ({placeholders})",
                {**fields, "source_url": meta.source_url},
            ).lastrowid

        # Unsupported files (videos etc.) are not kept on disk.
        raw_path = None
        if status != "unsupported":
            target = config.RAW_DIR / meta.unit_code / f"{doc_id}_{filename}"
            target.parent.mkdir(parents=True, exist_ok=True)
            upload_path.replace(target)
            raw_path = str(target.relative_to(config.DATA_DIR))
            if old_raw and old_raw != target:
                old_raw.unlink(missing_ok=True)
        elif old_raw:
            old_raw.unlink(missing_ok=True)

        conn.execute("UPDATE documents SET raw_path = ? WHERE id = ?", (raw_path, doc_id))
        conn.executemany(
            "INSERT INTO pages (doc_id, page_no, text) VALUES (?, ?, ?)",
            [(doc_id, page.number, page.text) for page in pages],
        )

    action = "updated" if existing else "created"
    if status == "ok":
        verb = "re-ingested" if existing else "ingested"
        log.info("%s %s: %d pages", verb, name, page_count)
    elif status == "no_text":
        log.warning("no text extracted %s: %d pages, %s", name, page_count, detail)
    elif status == "unsupported":
        log.info("skipped %s: %s", name, detail)
    else:
        log.error("failed %s: %s", name, detail)
    return Result(doc_id, action, status, len(pages), detail)
