"""CTFtime writeup source.

CTFtime publishes a paginated writeup table (``/writeups``) whose rows link to
writeup detail pages (``/writeup/<id>``). The detail body lives in
``#id_description``; the surrounding chrome (navigation, breadcrumbs, rating
widgets) is not article content. This adapter therefore extracts the description
element directly instead of relying on the generic HTML body heuristic.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ctfka.contracts import DiscoveredItem, FetchedDocument, ParsedDocument
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.http import FetchError
from ctfka.ingestion.parser import _convert

BASE = "https://ctftime.org"
LIST_PATH = "/writeups"
WRITEUP_RE = re.compile(r"^/writeup/(\d+)/?$")
EVENT_RE = re.compile(r"^/event/(\d+)/?$")
TASK_RE = re.compile(r"^/task/(\d+)/?$")


class CTFtimeAdapter(SourceAdapter):
    """Paginated CTFtime writeup listings with stable writeup-id identity."""

    async def discover(self, cursor: dict | None = None) -> AsyncIterator[DiscoveredItem]:
        # The worker passes a cursor, but a standalone call may instead rely on
        # set_state(); both describe the same durable position.
        previous = dict(cursor or {}) or dict(self._state)
        first_page = int(previous.get("next_page") or 1)
        first_page = max(first_page, 1)
        # A previous run may have recorded the highest page it reached; keep going
        # only while the listing still returns rows so a growing archive is covered.
        last_known = int(previous.get("last_page") or 0)
        base = str(self.options.get("list_url") or BASE + LIST_PATH)
        limit = self.config.max_items
        emitted = 0
        seen: set[str] = set()
        page = first_page
        highest = last_known
        stop_page = self.options.get("max_pages")
        stop_page = int(stop_page) if stop_page is not None else None
        self._complete_scan = False
        self._state = {}

        while True:
            if stop_page is not None and page > stop_page:
                break
            url = base if page == 1 else f"{base}?page={page}"
            try:
                response = await self.client.get(url)
            except FetchError as exc:
                if exc.category == "permanent" and exc.status_code == 404:
                    # Pages beyond the end of the archive answer 404; this is the
                    # only reliable end-of-listing signal.
                    self._complete_scan = True
                    break
                raise
            items = self._parse_listing(response.content, page)
            if not items:
                self._complete_scan = True
                break
            for item in items:
                if item.identity in seen:
                    continue
                seen.add(item.identity)
                emitted += 1
                highest = max(highest, page)
                yield item
                if limit is not None and emitted >= limit:
                    # Deliberately truncated: the cursor must not claim completeness.
                    self._state = {"next_page": page, "last_page": highest, "complete": False}
                    return
            page += 1
        self._state = {"next_page": 1, "last_page": highest, "complete": self._complete_scan}

    def _parse_listing(self, raw: bytes, page: int) -> list[DiscoveredItem]:
        soup = BeautifulSoup(raw, "html.parser")
        table = soup.select_one("table.tablesorter") or soup.select_one("table")
        if table is None:
            return []
        body = table.select_one("tbody") or table
        items: list[DiscoveredItem] = []
        for row in body.select("tr"):
            cells = row.select("td")
            if len(cells) < 4:
                continue
            writeup_link = None
            for anchor in row.select("a[href]"):
                href = str(anchor.get("href"))
                if WRITEUP_RE.match(urlsplit(href).path):
                    writeup_link = href
                    break
            if writeup_link is None:
                continue
            writeup_id = WRITEUP_RE.match(urlsplit(writeup_link).path).group(1)
            event = self._linked(cells[0], EVENT_RE)
            task = self._linked(cells[1], TASK_RE)
            topics = [span.get_text(" ", strip=True) for span in cells[2].select("span")]
            team = cells[3].get_text(" ", strip=True)
            metadata = {
                "page": page,
                "writeup_id": writeup_id,
                "event_id": event[0] if event else None,
                "event_name": event[1] if event else None,
                "task_id": task[0] if task else None,
                "task_name": task[1] if task else None,
                "topics": topics,
                "team": team,
                "source": "ctftime",
            }
            items.append(
                DiscoveredItem(
                    url=urljoin(BASE, writeup_link),
                    identity=f"ctftime:writeup:{writeup_id}",
                    metadata=metadata,
                )
            )
        return items

    @staticmethod
    def _linked(cell, pattern: re.Pattern[str]) -> tuple[str, str] | None:
        for anchor in cell.select("a[href]"):
            path = urlsplit(str(anchor.get("href"))).path
            match = pattern.match(path)
            if match:
                return match.group(1), anchor.get_text(" ", strip=True)
        return None

    async def fetch(self, item: DiscoveredItem) -> FetchedDocument:
        response = await self.client.get(item.url)
        metadata = dict(item.metadata)
        metadata["identity"] = item.identity
        return FetchedDocument(
            str(response.url),
            response.content,
            response.headers.get("Content-Type", "text/html").split(";", 1)[0],
            metadata,
        )

    def parse(self, document: FetchedDocument) -> ParsedDocument:
        soup = BeautifulSoup(document.raw, "html.parser")
        body = soup.select_one("#id_description")
        content = _convert(body).strip() if body is not None else ""
        title = self._title(soup, document.metadata)
        metadata = {**document.metadata, "has_body": bool(content)}
        external = []
        if body is not None:
            for anchor in body.select("a[href]"):
                href = str(anchor.get("href"))
                if href.startswith(("http://", "https://")) and "ctftime.org" not in href:
                    external.append(href)
        if external:
            metadata["original_writeup_urls"] = list(dict.fromkeys(external))
        return ParsedDocument(title=title, content=content, metadata=metadata)

    @staticmethod
    def _title(soup: BeautifulSoup, metadata: dict) -> str:
        heading = soup.select_one("h2")
        if heading is not None:
            text = heading.get_text(" ", strip=True)
            if text:
                return text
        for key in ("task_name", "title"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        if soup.title is not None:
            # "CTFtime.org / Event / Task / Writeup" -> "Task"
            parts = [p.strip() for p in soup.title.get_text().split("/") if p.strip()]
            if len(parts) >= 3:
                return parts[-2]
            return parts[0] if parts else "Untitled"
        return "Untitled"

    def validate(self, parsed: ParsedDocument) -> None:
        if not parsed.title.strip():
            raise ValueError("CTFtime writeup has no title")
        if not parsed.content.strip():
            raise ValueError(
                "CTFtime writeup has no body in #id_description; "
                "the writeup may only link to an external article"
            )

    def normalize(self, parsed: ParsedDocument) -> ParsedDocument:
        parsed = super().normalize(parsed)
        # CTFtime descriptions are stored as paragraph-wrapped Markdown, so the
        # converter output carries doubled blank lines between blocks.
        parsed.content = re.sub(r"\n{3,}", "\n\n", parsed.content).strip()
        return parsed

    @property
    def discovery_complete(self) -> bool:
        """True only when the listing ran past its last page.

        Deliberately *not* named ``complete_scan``: the worker treats that name as
        permission to archive every known identity that was not re-discovered,
        which is unsafe for a listing that can be truncated by ``max_items``.
        """
        return bool(getattr(self, "_complete_scan", False))
