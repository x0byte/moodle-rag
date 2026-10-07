from pathlib import Path

import pymupdf

from .base import Page


def extract_pdf(path: Path) -> list[Page]:
    with pymupdf.open(path, filetype="pdf") as doc:
        return [
            Page(number=index + 1, text=page.get_text("text").strip())
            for index, page in enumerate(doc)
        ]
