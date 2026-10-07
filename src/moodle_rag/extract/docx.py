from pathlib import Path

from docx import Document
from docx.table import Table

from .base import Page


def extract_docx(path: Path) -> list[Page]:
    """Paragraphs and tables in document order. Word files have no fixed pages, so one page."""
    parts = []
    for block in Document(str(path)).iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                # Merged cells repeat; keep each distinct cell once.
                cells = list(dict.fromkeys(cell.text.strip() for cell in row.cells))
                parts.append(" | ".join(cells))
        elif block.text.strip():
            parts.append(block.text.strip())
    return [Page(number=1, text="\n".join(parts))]
