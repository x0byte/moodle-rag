import re
from pathlib import Path

from bs4 import BeautifulSoup

from .base import Page

# Not content: dropped with everything inside them.
BOILERPLATE = [
    "script", "style", "noscript", "template", "svg", "iframe", "object", "form", "button",
    "nav", "header", "footer", "aside",
]

BLOCKS = [
    "p", "div", "section", "article", "main", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "table", "tr", "blockquote", "pre", "figure", "figcaption", "dl", "dt", "dd",
]


def html_to_text(html: str) -> str:
    """Readable text from HTML: boilerplate removed, one line per block element."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(BOILERPLATE):
        tag.decompose()
    root = soup.find("main") or soup.find("article") or soup.body or soup
    for tag in root.find_all("br"):
        tag.replace_with("\n")
    for tag in root.find_all(["td", "th"]):
        tag.append(" | ")
    for tag in root.find_all(BLOCKS):
        tag.insert_before("\n")
        tag.append("\n")
    lines = (re.sub(r"\s+", " ", line).strip() for line in root.get_text().splitlines())
    return "\n".join(line.rstrip(" |") for line in lines if line.strip(" |"))


def extract_html(path: Path) -> list[Page]:
    return [Page(number=1, text=html_to_text(path.read_text(encoding="utf-8", errors="replace")))]
