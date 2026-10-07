"""Local ingest server. Receives files from the browser extension."""

import argparse
import hashlib
import json
import logging
import uuid
from collections import Counter
from dataclasses import asdict
from datetime import datetime

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from . import config, db, embed
from .extract import EXTRACTORS
from .ingest import Metadata, ingest_file, parse_week, prune, safe_name

log = logging.getLogger("moodle_rag.server")

app = FastAPI(title="Moodle RAG ingest")


@app.middleware("http")
async def check_origin(request: Request, call_next):
    # Browsers always send Origin on cross-origin POSTs, so this keeps other
    # web pages out. Requests with no Origin (curl, local scripts) are allowed.
    origin = request.headers.get("origin")
    if origin and origin not in config.ALLOWED_ORIGINS:
        log.warning("rejected request from origin %s", origin)
        return JSONResponse({"detail": "origin not allowed"}, status_code=403)
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)
# Only answer to localhost names, which blocks DNS rebinding.
app.add_middleware(
    TrustedHostMiddleware,
    allowed_hosts=["127.0.0.1", "localhost"],
)


@app.get("/health")
def health():
    conn = db.connect()
    try:
        count = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()
    # The extension only downloads file types listed here.
    return {"status": "ok", "documents": count, "supported_types": sorted(EXTRACTORS)}


class CheckRequest(BaseModel):
    urls: list[str]


@app.post("/check")
def check(request: CheckRequest):
    """What the server already holds for these Moodle links, so a sync can skip them."""
    conn = db.connect()
    try:
        known = {}
        # Chunked to stay under SQLite's bound-parameter limit.
        for start in range(0, len(request.urls), 500):
            urls = request.urls[start : start + 500]
            rows = conn.execute(
                "SELECT l.source_url, l.resolved_url, d.id AS doc_id, d.sha256, d.status"
                " FROM locations l JOIN documents d ON d.id = l.doc_id"
                f" WHERE l.source_url IN ({', '.join('?' * len(urls))})",
                urls,
            ).fetchall()
            known.update({row["source_url"]: dict(row) for row in rows})
    finally:
        conn.close()
    log.info("sync check: %d resources, %d already ingested", len(request.urls), len(known))
    return {"known": known}


@app.post("/ingest")
def ingest(
    file: UploadFile = File(...),
    source_url: str = Form(...),
    unit_code: str = Form(...),
    title: str = Form(...),
    unit_name: str | None = Form(None),
    section: str | None = Form(None),
    week: int | None = Form(None),
    resolved_url: str | None = Form(None),
    resource_type: str | None = Form(None),
    scope: str | None = Form(None),
):
    tmp_dir = config.RAW_DIR / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    upload_path = tmp_dir / uuid.uuid4().hex

    digest = hashlib.sha256()
    with upload_path.open("wb") as out:
        while block := file.file.read(1024 * 1024):
            digest.update(block)
            out.write(block)

    meta = Metadata(
        source_url=source_url,
        unit_code=unit_code,
        title=title,
        unit_name=unit_name,
        section=section,
        week=week,
        resolved_url=resolved_url,
        resource_type=resource_type,
        scope=scope,
    )
    conn = db.connect()
    try:
        result = ingest_file(
            conn,
            upload_path,
            digest.hexdigest(),
            file.filename or title,
            file.content_type,
            meta,
        )
    finally:
        conn.close()
    return asdict(result)


class NotIngested(BaseModel):
    title: str
    section: str = ""
    url: str
    reason: str


class FinishRequest(BaseModel):
    unit_code: str
    scopes: list[str]
    seen: list[str]
    counts: dict[str, int] = {}
    skipped: list[NotIngested] = []
    failed: list[NotIngested] = []


def _skip_summary(skipped: list[NotIngested]) -> str:
    """'71 external links, 4 external tools, 2 video files' from the skip reasons."""
    parts = []
    for reason, count in Counter(item.reason for item in skipped).most_common():
        if reason.endswith(" not supported"):
            reason = f"{reason.removesuffix(' not supported')} file"
        plural = "" if count == 1 or reason.endswith(("s", "yet")) else "s"
        parts.append(f"{count} {reason}{plural}")
    return ", ".join(parts)


@app.post("/sync/finish")
def finish_sync(request: FinishRequest):
    """End of a sync: record what the extension did not send, then drop
    resources the scan covered but no longer found."""
    unit = safe_name(request.unit_code.upper(), "UNKNOWN")

    if request.skipped:
        log.info("skipped %s", _skip_summary(request.skipped))
    for item in request.failed:
        week = parse_week(item.section)
        name = f"{unit}{f' W{week}' if week is not None else ''} {item.title}"
        log.error("failed %s: %s (%s)", name, item.reason, item.url)

    conn = db.connect()
    try:
        removed = prune(conn, request.unit_code, request.scopes, request.seen)
    finally:
        conn.close()

    counts = dict(request.counts)
    if removed:
        counts["removed"] = len(removed)

    # The terminal gets one line per kind of skip; the full list goes here.
    report = config.DATA_DIR / f"sync-report-{unit}.json"
    report.write_text(
        json.dumps(
            {
                "unit": unit,
                "finished_at": datetime.now().isoformat(timespec="seconds"),
                "counts": counts,
                "failed": [item.model_dump() for item in request.failed],
                "skipped": [item.model_dump() for item in request.skipped],
                "removed": removed,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = ", ".join(f"{count} {state}" for state, count in counts.items())
    log.info("sync finished %s: %s  (details: %s)", unit, summary or "nothing to do", report.name)
    return {"removed": removed}


@app.get("/documents")
def documents(unit: str | None = None):
    """Each document once, with every place it appears."""
    conn = db.connect()
    try:
        docs = {
            row["id"]: {**dict(row), "locations": []}
            for row in conn.execute(
                "SELECT id, title, filename, file_type, status, status_detail, page_count,"
                " sha256, updated_at FROM documents WHERE :unit IS NULL OR id IN"
                " (SELECT doc_id FROM locations WHERE unit_code = upper(:unit))"
                " ORDER BY id",
                {"unit": unit},
            )
        }
        for row in conn.execute(
            "SELECT doc_id, unit_code, week, section, title, source_url, resolved_url"
            " FROM locations ORDER BY unit_code, week, section"
        ):
            if row["doc_id"] in docs:
                location = dict(row)
                docs[location.pop("doc_id")]["locations"].append(location)
    finally:
        conn.close()
    return list(docs.values())


def main() -> None:
    parser = argparse.ArgumentParser(description="Moodle RAG ingest server")
    parser.add_argument(
        "--reset-baseline",
        metavar="UNIT",
        help="forget the size of UNIT's last scan, so its next sync may remove resources"
        " even if it finds far fewer than before, then exit",
    )
    args = parser.parse_args()

    config.setup_logging()
    db.init()
    if args.reset_baseline:
        unit = safe_name(args.reset_baseline.upper(), "UNKNOWN")
        conn = db.connect()
        with conn:
            cleared = conn.execute("DELETE FROM scans WHERE unit_code = ?", (unit,)).rowcount
        conn.close()
        log.info("baseline reset for %s" if cleared else "no baseline recorded for %s", unit)
        return
    embed.model()  # load now, so the first upload of a sync is not slow
    log.info("ingest server on http://%s:%d", config.HOST, config.PORT)
    log.info("database: %s", config.DB_PATH)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
