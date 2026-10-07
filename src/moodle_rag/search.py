"""Hybrid retrieval: BM25 (FTS5) and vector similarity, merged with reciprocal rank fusion."""

import logging
import re
import sqlite3
import time

from . import config, embed

log = logging.getLogger("moodle_rag.search")

WORD = re.compile(r"[A-Za-z0-9]+")
STOPWORDS = frozenset(
    "a an and are as at be but by did do does for from how i in is it me my of on or say said"
    " tell that the this to was what when where which who why with about".split()
)


def fts_query(query: str) -> str | None:
    """An FTS5 query matching any of the query's content words."""
    words = [word for word in WORD.findall(query.lower()) if word not in STOPWORDS]
    words = list(dict.fromkeys(words)) or WORD.findall(query.lower())
    return " OR ".join(f'"{word}"' for word in words) if words else None


def _pages(file_type: str, start: int, end: int) -> str | None:
    if file_type not in ("pdf", "pptx"):
        return None  # Word and web content have no fixed pages
    unit = "slide" if file_type == "pptx" else "p."
    if start == end:
        return f"{unit} {start}"
    return f"slides {start}–{end}" if file_type == "pptx" else f"pp. {start}–{end}"


def citation(row, locations: list[dict]) -> str:
    """'FIT5122 Week 1, Week 7 · Notes on Presentations.pdf, pp. 3–4'."""
    units = list(dict.fromkeys(location["unit_code"] for location in locations))
    weeks = sorted({location["week"] for location in locations if location["week"] is not None})
    where = " ".join(units)
    if weeks:
        where += " " + ", ".join(f"Week {week}" for week in weeks)
    elif locations and locations[0]["section"]:
        where += f" {locations[0]['section'].split(' › ')[-1]}"
    # A file is cited by its name; section notes and Moodle pages by their title.
    name = row["title"] if row["file_type"] == "html" else row["filename"]
    pages = _pages(row["file_type"], row["page_start"], row["page_end"])
    return f"{where} · {name}" + (f", {pages}" if pages else "")


def search(
    conn: sqlite3.Connection,
    query: str,
    unit: str | None = None,
    week: int | None = None,
    k: int = 8,
) -> list[dict]:
    started = time.perf_counter()
    filters = {"unit": unit.upper() if unit else None, "week": week}
    # Chunks of documents that appear in the requested unit / week.
    scope = (
        "c.doc_id IN (SELECT doc_id FROM locations"
        " WHERE (:unit IS NULL OR unit_code = :unit) AND (:week IS NULL OR week = :week))"
    )
    depth = config.SEARCH_CANDIDATES

    keyword_ids = []
    match = fts_query(query)
    if match:
        keyword_ids = [
            row["id"]
            for row in conn.execute(
                "SELECT c.id FROM chunks_fts f JOIN chunks c ON c.id = f.rowid"
                f" WHERE chunks_fts MATCH :match AND {scope}"
                " ORDER BY bm25(chunks_fts, 1.0, 0.6) LIMIT :depth",
                {**filters, "match": match, "depth": depth},
            )
        ]
    vector_ids = [
        row["id"]
        for row in conn.execute(
            f"SELECT c.id FROM chunks c WHERE {scope}"
            " ORDER BY vec_distance_cosine(c.embedding, :query) LIMIT :depth",
            {**filters, "query": embed.embed_query(query).tobytes(), "depth": depth},
        )
    ]

    # Reciprocal rank fusion: a chunk scores 1 / (K + rank) in each list it appears in.
    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}
    for name, ids in (("keyword", keyword_ids), ("vector", vector_ids)):
        for rank, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (config.RRF_K + rank)
            ranks.setdefault(chunk_id, {})[name] = rank
    results = []
    per_doc: dict[int, int] = {}
    for chunk_id in sorted(scores, key=scores.get, reverse=True):
        if len(results) == k:
            break
        row = conn.execute(
            "SELECT c.id, c.doc_id, c.page_start, c.page_end, c.text, d.title, d.filename,"
            " d.file_type FROM chunks c JOIN documents d ON d.id = c.doc_id WHERE c.id = ?",
            (chunk_id,),
        ).fetchone()
        if per_doc.get(row["doc_id"], 0) >= config.SEARCH_MAX_PER_DOC:
            continue
        per_doc[row["doc_id"]] = per_doc.get(row["doc_id"], 0) + 1
        locations = [
            dict(location)
            for location in conn.execute(
                "SELECT unit_code, week, section, title, source_url FROM locations"
                " WHERE doc_id = ? ORDER BY week, id",
                (row["doc_id"],),
            )
        ]
        results.append(
            {
                "citation": citation(row, locations),
                "pages": _pages(row["file_type"], row["page_start"], row["page_end"]),
                "doc_id": row["doc_id"],
                "title": row["title"],
                "filename": row["filename"],
                "file_type": row["file_type"],
                "page_start": row["page_start"],
                "page_end": row["page_end"],
                "text": row["text"],
                "locations": locations,
                "score": round(scores[chunk_id], 5),
                "keyword_rank": ranks[chunk_id].get("keyword"),
                "vector_rank": ranks[chunk_id].get("vector"),
            }
        )

    scoped = "".join(f" {name}={value}" for name, value in filters.items() if value is not None)
    log.info(
        'search "%s"%s: %d results in %d ms%s',
        query, scoped, len(results), (time.perf_counter() - started) * 1000,
        f"  (top: {results[0]['citation']})" if results else "",
    )
    return results
