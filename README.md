# Moodle RAG

A personal study assistant over Monash Moodle. A browser extension downloads
your course material with your own logged-in session, a local server indexes
it, and an MCP server lets Claude search it and answer with citations to the
exact file and page:

> "What did week 10 say about the primacy of the public interest?"
> → *"You will place the interests of the public above those of personal,
> business or sectional interests." (FIT5122 Week 10 · ACS
> Code-of-Professional-Conduct_v2.1.pdf, p. 5)*

**For personal use.** Course material stays on your machine: files, text and
the search index live under `data/`, which is in `.gitignore` and is never
committed. Embeddings are computed locally. The only thing that goes anywhere
is what Claude reads through the MCP tools when you ask it a question.

## How it fits together

```
 Browser (your Moodle login)                 Your machine
┌───────────────────────────┐      ┌──────────────────────────────────────┐
│ Extension side panel      │      │ Ingest server  (uv run moodle-ingest)│
│  1. read the course index │      │  127.0.0.1:8765                      │
│  2. read each section page│ POST │  extract text  PDF · PPTX · DOCX ·   │
│  3. download each file    ├─────▶│                HTML                  │
│     (3 at a time)         │      │  chunk → embed (bge-small, local)    │
└───────────────────────────┘      │  store                               │
                                   └──────────────────┬───────────────────┘
                                                      ▼
                                   ┌──────────────────────────────────────┐
                                   │ data/moodle.db   SQLite: documents,  │
                                   │                  pages, chunks,      │
                                   │                  FTS5 + vectors      │
                                   │ data/raw/<unit>/ the original files  │
                                   └──────────────────┬───────────────────┘
                                                      ▼
 Claude Desktop / Claude Code      ┌──────────────────────────────────────┐
┌───────────────────────────┐ stdio│ MCP server  (uv run moodle-mcp)      │
│ "what's due in week 8?"   │◀────▶│  search_course_material · list_units │
└───────────────────────────┘      │  list_documents · get_document_text  │
                                   └──────────────────────────────────────┘
```

- **Documents** are distinct files, identified by content hash. The same file
  linked from two weeks is stored once and cited with both.
- **Section notes** are the text written directly on each Moodle section page
  (labels, content blocks, activity descriptions), one document per section.
- **Search** is hybrid: BM25 keyword search (SQLite FTS5) and vector
  similarity, merged with reciprocal rank fusion, at most two passages per
  document.

## Setup

Needs Python 3.11+ and [uv](https://docs.astral.sh/uv/). Dependencies are in
`pyproject.toml`.

```bash
uv sync
uv run moodle-ingest
```

The first start downloads the embedding model (`BAAI/bge-small-en-v1.5`,
about 130 MB) once; after that it loads from the local cache with no network
access. The server logs the database path it is using:

```
01:37:17  embedding model: BAAI/bge-small-en-v1.5
01:37:17  ingest server on http://127.0.0.1:8765
01:37:17  database: /…/moodle-rag/data/moodle.db
```

## Loading the extension

1. Open `chrome://extensions` (or `brave://extensions`) and turn on
   **Developer mode**.
2. **Load unpacked** → choose the `extension/` folder.
3. The extension ID should be `fnjbpimchjnkcfddddnkeibgkojhfhoj`. It is fixed
   by the `key` in `manifest.json`, and the ingest server only accepts
   requests from that ID.

After changing extension files, reload it on the extensions page and refresh
the Moodle tab.

## Syncing a course

1. Start the ingest server and log in to Moodle.
2. Open any page of the unit, click the extension icon to open the side panel,
   and click **Sync this course**.

The panel reads the section pages, then shows each resource as it is checked,
downloaded and ingested. The server terminal shows the same thing:

```
01:16:53  sync check: 177 resources, 48 already ingested
01:16:56  ingested FIT5122 W1 Section notes_ Week 1 … _ Own-time.html: 1 pages, 2 chunks
01:16:57  skipped 71 external links, 4 m4a files, 4 external tools, 2 video files, 1 xlsx file
01:16:57  sync finished FIT5122: 82 skipped, 48 current, 47 ingested  (details: sync-report-FIT5122.json)
```

Syncing again is cheap: unchanged files are recognised before they are
downloaded, changed files replace their old text, and files that have gone
from Moodle are removed.

What happens to what:

| On Moodle | Result |
|---|---|
| PDF, PPTX, DOCX, Moodle pages, text on section pages | ingested |
| Same file in several places | stored once, every location recorded |
| Scanned PDF or empty file | kept, flagged `no_text`, not searchable (no OCR) |
| Videos, audio, images, spreadsheets, external links and tools | skipped without downloading; listed in `data/sync-report-<unit>.json` |

If your Moodle session expires mid-sync, the sync stops and asks you to log in
again. Nothing is marked failed or removed; sync again to pick up the rest.

If a sync finds far fewer resources than the last one for that unit (under
70%), nothing is removed, in case the page only half loaded. If the unit
really did shrink, clear the baseline and sync again:

```bash
uv run moodle-ingest --reset-baseline FIT5122
```

## Connecting Claude

The MCP server reads the database directly, so the ingest server does not
need to be running. Replace the paths with your own (`which uv`, and this
repo's absolute path).

**Claude Code**

```bash
claude mcp add --scope user moodle -- /Users/you/.local/bin/uv run --directory /Users/you/moodle-rag moodle-mcp
```

**Claude Desktop** — add to
`~/Library/Application Support/Claude/claude_desktop_config.json`, then
restart Claude Desktop:

```json
{
  "mcpServers": {
    "moodle": {
      "command": "/Users/you/.local/bin/uv",
      "args": ["run", "--directory", "/Users/you/moodle-rag", "moodle-mcp"]
    }
  }
}
```

Use the absolute path to `uv`: Claude Desktop does not see your shell's
`PATH`.

Then ask things like *"Using my course material, what's due in FIT5120 week
8?"* The tools:

| Tool | Does |
|---|---|
| `search_course_material(query, unit?, week?, k=8)` | passages with unit, week, section, file, page range and Moodle URL |
| `list_units()` | units, document counts, weeks |
| `list_documents(unit, week?)` | what is ingested for a unit or week |
| `get_document_text(doc_id, page_range?)` | the text around a hit, e.g. pages `"10-14"` |

## Command line

```bash
uv run moodle-rag search "ACS code of ethics" -k 3
uv run moodle-rag search "effort estimation" --unit FIT5120 --week 8 --full
uv run moodle-rag reindex            # rebuild chunks and embeddings
uv run moodle-rag reindex --extract  # also re-extract text from data/raw
uv run moodle-rag eval               # retrieval eval, see below
```

## Checking retrieval

`uv run moodle-rag eval` runs each question in `data/eval-questions.json` and
reports whether its expected source is in the top 3 and top 8 results. Use it
before and after changing chunking or search settings.

The file is a list of questions, each with the file names or titles that
count as a correct source (any one of them, matched as a case-insensitive
substring), and optional `unit` and `week` filters. See
`evals/questions.example.json`. The real questions live under `data/` because
they name course files.

## Settings

| Variable | Default | |
|---|---|---|
| `MOODLE_RAG_DATA_DIR` | `<repo>/data` | where the database and raw files live |
| `MOODLE_RAG_PORT` | `8765` | ingest server port (also change it in the extension) |
| `MOODLE_RAG_ALLOWED_ORIGINS` | the extension's origin | origins the ingest server accepts |
| `MOODLE_RAG_EMBEDDING_MODEL` | `BAAI/bge-small-en-v1.5` | run `reindex` after changing it |

The data directory is anchored to the repo, not the current folder, so the
ingest server and the MCP server always use the same database wherever they
are launched from.

## Layout

```
extension/          side panel, course scanner (scan.js), sync (sync.js)
src/moodle_rag/
  server.py         ingest server (FastAPI)
  ingest.py         store, dedupe, replace, remove
  extract/          PDF, PPTX, DOCX, HTML → text per page or slide
  chunking.py       pages → ~450-token chunks with page ranges
  embed.py          local embeddings
  search.py         hybrid search
  mcp_server.py     MCP tools
  cli.py            search, reindex, eval
data/               everything ingested (gitignored)
```

## Monash specifics

The scanner is written for Monash's course format, where the unit page is a
dashboard and the content sits in nested sections listed in the course index.
On a Moodle without a course index it falls back to scanning the page you are
on. Files are served from a CloudFront host that is listed in the extension's
`host_permissions` and in `FILE_HOSTS` in `sync.js`; a different Moodle would
need its own host there.
