"""Retrieval eval: does the expected source show up in the top results for each question?"""

import json
import logging
from pathlib import Path

from . import db, embed
from .search import search

CUTOFFS = (3, 8)


def _matches(result: dict, expected: list[str]) -> bool:
    """True if the result's file name or title contains any of the expected strings."""
    haystack = f"{result['filename']} {result['title']}".lower()
    return any(item.lower() in haystack for item in expected)


def run(path: Path) -> None:
    questions = json.loads(path.read_text(encoding="utf-8"))
    logging.getLogger("moodle_rag.search").setLevel(logging.WARNING)  # one table, not a line per query
    embed.model()
    conn = db.connect(read_only=True)

    hits = {cutoff: 0 for cutoff in CUTOFFS}
    print(f"\n{'rank':>4}  question")
    for item in questions:
        results = search(
            conn, item["question"], unit=item.get("unit"), week=item.get("week"), k=max(CUTOFFS)
        )
        rank = next(
            (position for position, result in enumerate(results, start=1)
             if _matches(result, item["expected"])),
            None,
        )
        for cutoff in CUTOFFS:
            hits[cutoff] += rank is not None and rank <= cutoff
        print(f"{rank if rank else '-':>4}  {item['question']}")
        if rank != 1:
            # Show what won instead, and what was wanted.
            top = results[0]["citation"] if results else "no results"
            print(f"      top result: {top}")
            print(f"      expected:   {' | '.join(item['expected'])}")
    conn.close()

    total = len(questions)
    print()
    for cutoff in CUTOFFS:
        print(f"hit@{cutoff}: {hits[cutoff]}/{total} ({hits[cutoff] / total:.0%})")
