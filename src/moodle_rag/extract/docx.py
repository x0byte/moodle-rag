from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table

from .base import Page


def extract_docx(path: Path) -> list[Page]:
    """Paragraphs and tables in document order, then the text of text boxes and
    shapes. Word files have no fixed pages, so one page."""
    document = Document(str(path))
    parts = []
    for block in document.iter_inner_content():
        if isinstance(block, Table):
            for row in block.rows:
                # Merged cells repeat; keep each distinct cell once.
                cells = list(dict.fromkeys(cell.text.strip() for cell in row.cells))
                parts.append(" | ".join(cells))
        elif block.text.strip():
            parts.append(block.text.strip())

    # Text boxes and shapes hold their text in w:txbxContent, outside the body
    # flow. Word stores each one twice (drawing and legacy fallback), so
    # repeated paragraphs are kept once.
    boxed = []
    for box in document.element.body.iter(qn("w:txbxContent")):
        for paragraph in box.iter(qn("w:p")):
            text = "".join(node.text or "" for node in paragraph.iter(qn("w:t"))).strip()
            if text:
                boxed.append(text)
    parts.extend(dict.fromkeys(boxed))
    return [Page(number=1, text="\n".join(parts))]
