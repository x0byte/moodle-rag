"""Paths and settings. Everything can be overridden with MOODLE_RAG_* env vars."""

import logging
import os
import sys
from pathlib import Path

# src/moodle_rag/config.py -> repo root (the project is installed editable by uv).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("MOODLE_RAG_DATA", PROJECT_ROOT / "data")).resolve()
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

# A PDF with fewer non-whitespace characters than this is treated as scanned.
MIN_TEXT_CHARS = 20


def setup_logging() -> None:
    # stderr, so the same setup is safe for the stdio MCP server later.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
