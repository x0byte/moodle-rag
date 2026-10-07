"""Local ingest server. Receives files from the browser extension."""

import hashlib
import logging
import uuid
from dataclasses import asdict

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from . import config, db
from .ingest import Metadata, ingest_file

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
    return {"status": "ok", "documents": count}


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


@app.get("/documents")
def documents(unit: str | None = None):
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT id, unit_code, week, section, title, filename, file_type, status,"
            " status_detail, page_count, sha256, source_url, resolved_url, updated_at"
            " FROM documents WHERE (:unit IS NULL OR unit_code = upper(:unit))"
            " ORDER BY unit_code, week, title",
            {"unit": unit},
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def main() -> None:
    config.setup_logging()
    db.init()
    log.info("ingest server on http://%s:%d  (data: %s)", config.HOST, config.PORT, config.DATA_DIR)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")


if __name__ == "__main__":
    main()
