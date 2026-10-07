"""MCP server (stdio): lets Claude search and read the ingested course material.

stdout carries the protocol, so everything here logs to stderr.
"""

import logging
import re
import threading

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import config, db, embed
from .search import search

log = logging.getLogger("moodle_rag.mcp")

# get_document_text returns at most this much text per call.
MAX_TEXT_CHARS = 24_000

server = MCPServer(
    "moodle",
    instructions=(
        "Search and read the user's Monash Moodle course material (lecture slides, readings,"
        " assessment specifications and the notes on each week's Moodle page), stored locally."
        " Use search_course_material for any question about unit content, then answer from the"
        " returned text only. Cite every claim with the result's `citation` (unit, week, file"
        " name and page or slide range) and give its `url` so the user can open the source."
        " If the results do not answer the question, say so rather than guessing."
    ),
    log_level="WARNING",
)


def _read(function, *args):
    """Run a read-only query in a worker thread, retrying if the database is briefly locked."""

    def run():
        conn = db.connect(read_only=True)
        try:
            return db.retry_if_locked(function)(conn, *args)
        finally:
            conn.close()

    return anyio.to_thread.run_sync(run)


def _primary_location(locations: list[dict], unit: str | None, week: int | None) -> dict:
    """The place to cite: the one matching the search filters when there are several."""
    for location in locations:
        if (not unit or location["unit_code"] == unit.upper()) and (
            week is None or location["week"] == week
        ):
            return location
    return locations[0]


def _search(conn, query: str, unit: str | None, week: int | None, k: int) -> dict:
    results = []
    for hit in search(conn, query, unit=unit, week=week, k=max(1, min(k, 20))):
        location = _primary_location(hit["locations"], unit, week)
        others = [other for other in hit["locations"] if other is not location]
        results.append(
            {
                "citation": hit["citation"],
                "unit": location["unit_code"],
                "week": location["week"],
                "section": location["section"],
                "title": location["title"] or hit["title"],
                "filename": hit["filename"],
                "pages": hit["pages"],
                "page_start": hit["page_start"],
                "page_end": hit["page_end"],
                "url": location["source_url"],
                "doc_id": hit["doc_id"],
                "also_in": [
                    {"unit": other["unit_code"], "section": other["section"], "url": other["source_url"]}
                    for other in others
                ],
                "text": hit["text"],
            }
        )
    return {"query": query, "unit": unit, "week": week, "results": results}


@server.tool()
async def search_course_material(
    query: str, unit: str | None = None, week: int | None = None, k: int = 8
) -> dict:
    """Search the user's course material and return the most relevant passages.

    Hybrid keyword + semantic search over lecture slides, readings, assessment documents
    and the text of each week's Moodle page ("section notes").

    Args:
        query: What to look for. Exact terms (unit codes, algorithm or document names) and
            natural-language questions both work.
        unit: Restrict to one unit code, e.g. "FIT5122". Use list_units to see what exists.
        week: Restrict to one teaching week, e.g. 6.
        k: Number of passages to return (default 8, at most 20).

    Each result has the passage `text` and where it comes from: `unit`, `week`, `section`
    (the path on the Moodle course page), `title`, `filename`, `pages` (page or slide range;
    null for Word and web content), `url` (the Moodle link) and `also_in` (other places the
    same file appears).

    When you answer, cite the file and page for every claim, using the result's `citation`
    string, e.g. "(FIT5122 Week 10 · ACS Code-of-Professional-Conduct_v2.1.pdf, p. 4)", and
    include the `url`. Passages are excerpts: call get_document_text with the `doc_id` to
    read the surrounding pages before relying on a detail that is cut off.
    """
    return await _read(_search, query, unit, week, k)


def _units(conn) -> dict:
    rows = conn.execute(
        "SELECT unit_code, MAX(unit_name) AS unit_name, COUNT(DISTINCT doc_id) AS documents,"
        " GROUP_CONCAT(DISTINCT week) AS weeks FROM locations GROUP BY unit_code ORDER BY unit_code"
    ).fetchall()
    return {
        "units": [
            {
                "unit": row["unit_code"],
                "name": row["unit_name"],
                "documents": row["documents"],
                "weeks": sorted(int(week) for week in (row["weeks"] or "").split(",") if week),
            }
            for row in rows
        ]
    }


@server.tool()
async def list_units() -> dict:
    """List the units that have ingested course material, with document counts and weeks."""
    return await _read(_units)


def _documents(conn, unit: str, week: int | None) -> dict:
    rows = conn.execute(
        "SELECT d.id, d.filename, d.file_type, d.status, d.page_count, l.week, l.section,"
        " l.title, l.source_url FROM locations l JOIN documents d ON d.id = l.doc_id"
        " WHERE l.unit_code = upper(:unit) AND (:week IS NULL OR l.week = :week)"
        " ORDER BY l.week IS NULL, l.week, l.id",
        {"unit": unit, "week": week},
    ).fetchall()
    return {
        "unit": unit.upper(),
        "week": week,
        "documents": [
            {
                "doc_id": row["id"],
                "title": row["title"],
                "filename": row["filename"],
                "type": row["file_type"],
                "pages": row["page_count"],
                "week": row["week"],
                "section": row["section"],
                "url": row["source_url"],
                # "no_text" marks a scanned or empty file: listed, but not searchable.
                "status": row["status"],
            }
            for row in rows
        ],
    }


@server.tool()
async def list_documents(unit: str, week: int | None = None) -> dict:
    """List the documents ingested for a unit, optionally for one week.

    Args:
        unit: Unit code, e.g. "FIT5122".
        week: Teaching week number. Omit for the whole unit.

    Returns each document's `doc_id` (for get_document_text), title, file name, type, page
    count, week, section path and Moodle `url`. A file that appears in several sections is
    listed once per section. Documents with status "no_text" could not be read (scanned or
    empty) and are not searchable.
    """
    return await _read(_documents, unit, week)


def _parse_range(page_range: str | None) -> tuple[int, int] | None:
    if not page_range:
        return None
    match = re.fullmatch(r"\s*(\d+)\s*(?:[-–]\s*(\d+))?\s*", page_range)
    if not match:
        raise ToolError(f'page_range must look like "5" or "3-7", not "{page_range}"')
    start = int(match.group(1))
    end = int(match.group(2) or start)
    return min(start, end), max(start, end)


def _document_text(conn, doc_id: int, page_range: str | None) -> dict:
    doc = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if doc is None:
        raise ToolError(f"No document with doc_id {doc_id}. Use list_documents to find one.")
    wanted = _parse_range(page_range)
    locations = conn.execute(
        "SELECT unit_code, week, section, title, source_url FROM locations WHERE doc_id = ?"
        " ORDER BY week, id",
        (doc_id,),
    ).fetchall()

    pages, used, truncated = [], 0, False
    for row in conn.execute(
        "SELECT page_no, text FROM pages WHERE doc_id = ? ORDER BY page_no", (doc_id,)
    ):
        if wanted and not wanted[0] <= row["page_no"] <= wanted[1]:
            continue
        text = row["text"]
        if used + len(text) > MAX_TEXT_CHARS:
            text, truncated = text[: MAX_TEXT_CHARS - used], True
        pages.append({"page": row["page_no"], "text": text})
        used += len(text)
        if truncated:
            break

    result = {
        "doc_id": doc_id,
        "filename": doc["filename"],
        "type": doc["file_type"],
        "page_count": doc["page_count"],
        "status": doc["status"],
        "locations": [
            {
                "unit": location["unit_code"],
                "week": location["week"],
                "section": location["section"],
                "title": location["title"],
                "url": location["source_url"],
            }
            for location in locations
        ],
        "pages": pages,
    }
    if truncated:
        result["truncated"] = (
            f"Output stopped at page {pages[-1]['page']} after {MAX_TEXT_CHARS} characters."
            " Ask for a narrower page_range to read the rest."
        )
    return result


@server.tool()
async def get_document_text(doc_id: int, page_range: str | None = None) -> dict:
    """Read the extracted text of one document, to see the context around a search hit.

    Args:
        doc_id: The `doc_id` from a search result or from list_documents.
        page_range: Pages or slides to return, e.g. "12" or "10-14". Omit for the whole
            document. Word documents, Moodle pages and section notes are a single page 1.

    Returns the text page by page, with the file name and the Moodle locations to cite.
    Long documents are cut off at about 24,000 characters; ask for a narrower range.
    """
    return await _read(_document_text, doc_id, page_range)


def _warm_up() -> None:
    try:
        embed.model()
        log.info("model ready")
    except Exception:
        log.exception("could not load the embedding model")


def main() -> None:
    config.setup_logging()
    if not config.DB_PATH.exists():
        db.init()
    log.info("moodle MCP server starting (database: %s)", config.DB_PATH)
    # Loaded in the background so the MCP handshake is immediate. A search that
    # arrives first simply waits for it.
    threading.Thread(target=_warm_up, name="model-warm-up", daemon=True).start()
    server.run("stdio")


if __name__ == "__main__":
    main()
