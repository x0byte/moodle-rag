"""Builds the search index for a document: chunks, their embeddings and FTS rows."""

import sqlite3

from . import config, embed
from .chunking import chunk_pages
from .extract import Page


def document_context(conn: sqlite3.Connection, doc_id: int) -> str:
    """'FIT5122 | Week 1 - Meetings › Own-time | Notes on Presentations | notes.pdf'.

    Prepended to each chunk for embedding, and searchable as the FTS title, so
    a chunk can be found by its unit, section or file name as well as its text.
    """
    doc = conn.execute("SELECT title, filename FROM documents WHERE id = ?", (doc_id,)).fetchone()
    location = conn.execute(
        "SELECT unit_code, section, title FROM locations WHERE doc_id = ? ORDER BY id LIMIT 1",
        (doc_id,),
    ).fetchone()
    parts = []
    if location:
        # The last two levels: "Week 1 - Meetings › Own-time", without "Learning ›".
        section = " › ".join((location["section"] or "").split(" › ")[-2:])
        parts += [location["unit_code"], section, location["title"]]
    parts += [doc["title"], doc["filename"]]
    return " | ".join(dict.fromkeys(part for part in parts if part))


def index_document(conn: sqlite3.Connection, doc_id: int) -> int:
    """(Re)build the chunks for one document from its stored pages. Returns the chunk count.

    Runs inside the caller's transaction. FTS rows follow the chunks table
    through triggers (see db.py).
    """
    conn.execute("DELETE FROM chunks WHERE doc_id = ?", (doc_id,))
    pages = [
        Page(number=row["page_no"], text=row["text"])
        for row in conn.execute(
            "SELECT page_no, text FROM pages WHERE doc_id = ? ORDER BY page_no", (doc_id,)
        )
    ]
    chunks = chunk_pages(pages, embed.count_tokens)
    if not chunks:
        return 0
    context = embed.truncate_tokens(document_context(conn, doc_id), config.CONTEXT_TOKENS)
    vectors = embed.embed_passages([f"{context}\n{chunk.text}" for chunk in chunks])
    conn.executemany(
        "INSERT INTO chunks (doc_id, seq, page_start, page_end, text, context, tokens, embedding)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (doc_id, seq, chunk.page_start, chunk.page_end, chunk.text, context, chunk.tokens,
             vector.tobytes())
            for seq, (chunk, vector) in enumerate(zip(chunks, vectors))
        ],
    )
    return len(chunks)
