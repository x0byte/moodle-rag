"""Splits a document's pages into chunks for retrieval.

Chunks never cross documents. A chunk is a run of whole pages (or slides) when
they fit; a page too long for one chunk is split on line boundaries, then
sentences, with a small overlap between its pieces. Every chunk records the
page range it covers.
"""

import re
from dataclasses import dataclass
from typing import Callable

from . import config
from .extract import Page

SENTENCE_END = re.compile(r"(?<=[.!?;:])\s+")


@dataclass
class Chunk:
    page_start: int
    page_end: int
    text: str
    tokens: int


def _split_long(text: str, limit: int, count: Callable[[str], int]) -> list[str]:
    """Break one over-long line into sentences, and a sentence into words, to fit the limit."""
    pieces = []
    for sentence in SENTENCE_END.split(text):
        if count(sentence) <= limit:
            pieces.append(sentence)
            continue
        current: list[str] = []
        for word in sentence.split():
            if current and count(" ".join([*current, word])) > limit:
                pieces.append(" ".join(current))
                current = []
            current.append(word)
        if current:
            pieces.append(" ".join(current))
    return [piece for piece in pieces if piece.strip()]


def _split_page(page: Page, count: Callable[[str], int]) -> list[Chunk]:
    """Chunks for a page that does not fit in one: packed lines, overlapping by a few lines."""
    limit, overlap = config.CHUNK_TOKENS, config.CHUNK_OVERLAP_TOKENS
    lines: list[tuple[str, int]] = []
    for line in page.text.splitlines():
        if not line.strip():
            continue
        tokens = count(line)
        if tokens <= limit:
            lines.append((line, tokens))
        else:
            lines.extend((piece, count(piece)) for piece in _split_long(line, limit, count))

    chunks: list[Chunk] = []
    current: list[tuple[str, int]] = []
    carried = 0  # how many lines at the start of `current` repeat the previous chunk

    def close() -> None:
        text = "\n".join(line for line, _ in current)
        chunks.append(Chunk(page.number, page.number, text, count(text)))

    for line, tokens in lines:
        if len(current) > carried and sum(t for _, t in current) + tokens > limit:
            close()
            # Start the next chunk with the tail of this one.
            tail: list[tuple[str, int]] = []
            for previous in reversed(current):
                if sum(t for _, t in tail) + previous[1] > overlap:
                    break
                tail.insert(0, previous)
            current, carried = tail, len(tail)
            # The overlap must never push the new line out of the chunk.
            while current and sum(t for _, t in current) + tokens > limit:
                current.pop(0)
                carried -= 1
        current.append((line, tokens))
    if len(current) > carried:
        close()
    return chunks


def chunk_pages(pages: list[Page], count: Callable[[str], int]) -> list[Chunk]:
    """count(text) returns the number of embedding-model tokens in text."""
    limit = config.CHUNK_TOKENS
    chunks: list[Chunk] = []
    current: list[Page] = []
    current_tokens = 0

    def close() -> None:
        nonlocal current, current_tokens
        if current:
            text = "\n\n".join(page.text for page in current)
            chunks.append(Chunk(current[0].number, current[-1].number, text, count(text)))
        current, current_tokens = [], 0

    for page in pages:
        if not page.text.strip():
            continue
        tokens = count(page.text)
        if tokens > limit:
            # Too long for one chunk: split it on its own, between page boundaries.
            close()
            chunks.extend(_split_page(page, count))
            continue
        if current_tokens + tokens > limit:
            close()
        current.append(page)
        current_tokens += tokens
    close()
    return chunks
