"""Paths and settings. Everything can be overridden with MOODLE_RAG_* env vars."""

import logging
import os
import sys
from pathlib import Path

# src/moodle_rag/config.py -> repo root (the project is installed editable by uv).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Anchored to the project root, never the current working directory, so the
# ingest server and the MCP server always share one database.
DATA_DIR = Path(os.environ.get("MOODLE_RAG_DATA_DIR") or PROJECT_ROOT / "data").expanduser().resolve()
RAW_DIR = DATA_DIR / "raw"
DB_PATH = DATA_DIR / "moodle.db"

HOST = "127.0.0.1"
PORT = int(os.environ.get("MOODLE_RAG_PORT", "8765"))

# The extension ID is fixed by the "key" in extension/manifest.json, so it stays
# the same wherever the unpacked extension is loaded from.
EXTENSION_ID = "fnjbpimchjnkcfddddnkeibgkojhfhoj"
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "MOODLE_RAG_ALLOWED_ORIGINS", f"chrome-extension://{EXTENSION_ID}"
    ).split(",")
    if origin.strip()
]

# A file with fewer non-whitespace characters than this counts as having no text
# (for a PDF: scanned, no text layer).
MIN_TEXT_CHARS = 20

# Embeddings run locally. bge-small reads at most 512 tokens, so a chunk plus
# its context line has to stay under that.
EMBEDDING_MODEL = os.environ.get("MOODLE_RAG_EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
CHUNK_TOKENS = 450
CHUNK_OVERLAP_TOKENS = 60
CONTEXT_TOKENS = 48

# Hybrid search: how many candidates each of BM25 and vector search contributes,
# and the constant in reciprocal rank fusion (score = sum of 1 / (RRF_K + rank)).
SEARCH_CANDIDATES = 50
RRF_K = 60

# A scan that finds fewer than this share of the unit's previous scan is treated
# as incomplete, and nothing is removed on the strength of it.
PRUNE_MIN_RATIO = 0.7


def setup_logging() -> None:
    # stderr, so the same setup is safe for the stdio MCP server later.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    # Libraries that log every HTTP request or model detail at INFO.
    for noisy in ("httpx", "huggingface_hub", "sentence_transformers", "transformers"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
