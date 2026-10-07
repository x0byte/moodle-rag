"""Text extraction. Each extractor returns one Page per page/slide, numbered from 1."""

from pathlib import Path
from typing import Callable

from .base import Page
from .docx import extract_docx
from .html import extract_html
from .pdf import extract_pdf
from .pptx import extract_pptx

MIME_TYPES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/html": "html",
}

Extractor = Callable[[Path], list[Page]]

# file type -> extractor. Anything not listed here is logged and skipped.
EXTRACTORS: dict[str, Extractor] = {
    "pdf": extract_pdf,
    "pptx": extract_pptx,
    "docx": extract_docx,
    "html": extract_html,
}


def detect_file_type(filename: str, content_type: str | None, head: bytes) -> str:
    """Best guess at the file type, as a lowercase extension without the dot."""
    if head.startswith(b"%PDF"):
        return "pdf"
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix:
        return "html" if suffix == "htm" else suffix
    return MIME_TYPES.get((content_type or "").split(";")[0].strip().lower(), "")
