"""Command line: search the index, or rebuild it."""

import argparse
import textwrap

from . import config, db, embed
from .ingest import reindex
from .search import search


def _search(args: argparse.Namespace) -> None:
    conn = db.connect()
    embed.model()  # loaded first, so the logged search time is the search itself
    results = search(conn, args.query, unit=args.unit, week=args.week, k=args.k)
    conn.close()
    for number, result in enumerate(results, start=1):
        ranks = ", ".join(
            f"{name} #{result[f'{name}_rank']}"
            for name in ("keyword", "vector")
            if result[f"{name}_rank"]
        )
        print(f"\n{number}. {result['citation']}")
        print(f"   doc {result['doc_id']} · score {result['score']} ({ranks})")
        for location in result["locations"]:
            print(f"   {location['section']}  {location['source_url']}")
        text = " ".join(result["text"].split())
        if not args.full:
            text = textwrap.shorten(text, width=args.width, placeholder=" …")
        print(textwrap.indent(textwrap.fill(text, width=100), "   | "))
    if not results:
        print("No results.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="moodle-rag", description="Search ingested course material.")
    commands = parser.add_subparsers(dest="command", required=True)

    find = commands.add_parser("search", help="hybrid search over the ingested material")
    find.add_argument("query")
    find.add_argument("--unit", help="unit code, e.g. FIT5122")
    find.add_argument("--week", type=int)
    find.add_argument("-k", type=int, default=8, help="number of results (default 8)")
    find.add_argument("--full", action="store_true", help="print whole chunks")
    find.add_argument("--width", type=int, default=400, help="characters of each chunk to show")
    find.set_defaults(run=_search)

    rebuild = commands.add_parser("reindex", help="rebuild chunks and embeddings for every document")
    rebuild.add_argument(
        "--extract", action="store_true", help="extract text again from the raw files first"
    )
    rebuild.set_defaults(run=lambda args: _reindex(args.extract))

    args = parser.parse_args()
    config.setup_logging()
    db.init()
    args.run(args)


def _reindex(extract: bool) -> None:
    conn = db.connect()
    reindex(conn, extract=extract)
    conn.close()


if __name__ == "__main__":
    main()
