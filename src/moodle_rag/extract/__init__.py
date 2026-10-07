"""Text extraction. Each extractor returns one Page per page/slide, numbered from 1."""

from pathlib import Path
from typing import Callable

from .base import Page
from .pdf import extract_pdf

Extractor = Callable[[Path], list[Page]]

# file type -> extractor. Anything not listed here is logged and skipped.
EXTRACTORS: dict[str, Extractor] = {
    "pdf": extract_pdf,
}


def detect_file_type(filename: str, content_type: str | None, head: bytes) -> str:
    """Best guess at the file type, as a lowercase extension without the dot."""
    if head.startswith(b"%PDF"):
        return "pdf"
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix:
        return suffix
    if content_type == "application/pdf":
        return "pdf"
    return ""
