from __future__ import annotations

import re
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from ctfka.contracts import FetchedDocument, ParsedDocument

PARSER_VERSION = "2"
_DROP = {"script", "style", "nav", "aside", "noscript", "svg", "form", "button", "footer"}
_BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "header",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "ul",
    "ol",
    "li",
    "blockquote",
    "pre",
    "table",
    "tr",
}


def _inline(node: Any) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag) or node.name in _DROP:
        return ""
    name = node.name.lower()
    if name == "br":
        return "\n"
    if name == "img":
        alt = node.get("alt", "")
        return f"![{alt}]({node.get('src', '')})"
    inner = "".join(_inline(c) for c in node.children)
    if name == "a":
        return f"[{inner}]({node.get('href', '')})" if node.get("href") else inner
    if name in {"strong", "b"}:
        return f"**{inner}**"
    if name in {"em", "i"}:
        return f"*{inner}*"
    if name == "del":
        return f"~~{inner}~~"
    if name == "code":
        return f"`{inner}`"
    return inner


def _convert(node: Any) -> str:
    if isinstance(node, NavigableString):
        return str(node)
    if not isinstance(node, Tag) or node.name in _DROP:
        return ""
    name = node.name.lower()
    if name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
        level = int(name[1])
        return "#" * level + " " + _inline(node).strip() + "\n\n"
    if name == "pre":
        code = node.find("code")
        language = ""
        if code:
            classes = code.get("class", [])
            language = next(
                (c.split("language-", 1)[1] for c in classes if c.startswith("language-")), ""
            )
            text = code.get_text()
        else:
            text = node.get_text()
        text = text.strip("\n")
        return f"```{language}\n{text}\n```\n\n"
    if name == "table":
        rows = []
        for tr in node.find_all("tr"):
            cells = [
                re.sub(r"\s+", " ", _inline(cell).strip()).replace("|", "\\|")
                for cell in tr.find_all(["th", "td"], recursive=False)
            ]
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(map(len, rows))
        rows = [r + [""] * (width - len(r)) for r in rows]
        output = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        output.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return "\n".join(output) + "\n\n"
    if name == "p":
        return _inline(node).strip() + "\n\n"
    if name in {"ul", "ol"}:
        items = []
        for i, li in enumerate(node.find_all("li", recursive=False), 1):
            marker = f"{i}. " if name == "ol" else "- "
            items.append(marker + _inline(li).strip())
        return "\n".join(items) + "\n\n"
    if name == "blockquote":
        content = "".join(
            _convert(c) if isinstance(c, Tag) else str(c) for c in node.children
        ).strip()
        return "\n".join("> " + line for line in content.splitlines()) + "\n\n"
    if name == "hr":
        return "\n---\n\n"
    content = "".join(_convert(c) if isinstance(c, Tag) else str(c) for c in node.children)
    if name in _BLOCK:
        return content.strip("\n") + "\n\n" if content.strip() else ""
    return content


def parse_document(document: FetchedDocument) -> ParsedDocument:
    raw = document.raw.decode("utf-8", errors="replace")
    content_type = document.content_type.lower()
    is_html = "html" in content_type or raw.lstrip().lower().startswith(
        ("<!doctype html", "<html", "<article", "<body")
    )
    if is_html:
        soup = BeautifulSoup(raw, "html.parser")
        html_title = soup.title.get_text(" ", strip=True) if soup.title else ""
        h1 = soup.find("h1")
        heading_title = h1.get_text(" ", strip=True) if h1 else ""
        candidates = []
        for node in soup.find_all(True):
            classes = " ".join(node.get("class") or []).lower()
            if node.name in _DROP or any(
                marker in classes for marker in ("navigation", "sidebar", "advert", "cookie")
            ):
                candidates.append(node)
        candidate_ids = {id(node) for node in candidates}
        removal_roots = [
            node
            for node in candidates
            if not any(id(parent) in candidate_ids for parent in node.parents)
        ]
        for node in removal_roots:
            node.decompose()
        body = soup.find("main") or soup.find("article") or soup.body or soup
        text = _convert(body).strip()
        title = heading_title
        title = title or html_title
    else:
        # Markdown is source data: preserve whitespace, code, entities and blank lines.
        text = raw
        title = ""
        for line in text.splitlines():
            m = re.match(r"^#\s+(.+?)\s*#*\s*$", line)
            if m:
                title = m.group(1)
                break
        title = title or str(document.metadata.get("title") or "")
    if not title:
        title = next((line.strip() for line in text.splitlines() if line.strip()), "Untitled")
        title = re.sub(r"^#{1,6}\s*", "", title)[:300]
    return ParsedDocument(
        title=title[:500],
        content=text,
        metadata={**document.metadata, "parser_version": PARSER_VERSION},
    )
