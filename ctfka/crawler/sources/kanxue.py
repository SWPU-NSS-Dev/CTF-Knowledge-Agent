"""看雪安全社区 (bbs.kanxue.com) source.

Kanxue runs Xiuno BBS. Boards are addressed as ``forum-<id>.htm`` and paginate as
``forum-<id>-<page>___.htm``; deep page numbers past the end answer 200 while
serving the last page again, so repeated threads are the end-of-board signal.
Thread pages carry the first post in ``div.message.message_rich_type`` together
with replies in further ``message_card`` blocks; only the original post is article
content.

Kanxue wraps links in ``mark.encrypted`` blobs that client-side JavaScript decodes.
Those blobs are not article text, so they are preserved verbatim in a fenced block
rather than dropped, keeping the payload available without executing it.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from ctfka.contracts import DiscoveredItem, FetchedDocument, ParsedDocument
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.http import FetchError
from ctfka.crawler.sources.common import _as_urls
from ctfka.ingestion.parser import _convert

DEFAULT_BOARDS = (4, 37, 150, 171)  # 逆向工程, CTF对抗, 二进制漏洞, Pwn
THREAD_RE = re.compile(r"thread-(\d+)(?:-\d+)?\.htm$")
FORUM_RE = re.compile(r"forum-(\d+)\.htm$")


class KanxueAdapter(SourceAdapter):
    """Board listing traversal with first-post extraction."""

    def _boards(self) -> list[int]:
        configured = _as_urls(self.options.get("forum_ids") or self.options.get("forums") or [])
        if not configured:
            raw = self.options.get("forum_ids") or self.options.get("forums")
            if isinstance(raw, (list, tuple)):
                configured = [str(value) for value in raw]
        if not configured:
            return list(DEFAULT_BOARDS)
        boards = []
        for value in configured:
            if not str(value).strip().isdigit():
                raise ValueError(f"kanxue forum id must be numeric: {value!r}")
            boards.append(int(str(value).strip()))
        return boards

    async def discover(self, cursor: dict | None = None) -> AsyncIterator[DiscoveredItem]:
        # The worker passes a cursor, but a standalone call may instead rely on
        # set_state(); both describe the same durable position.
        previous = dict(cursor or {}) or dict(self._state)
        boards = self._boards()
        resume_board = int(previous.get("next_board") or 0)
        resume_page = int(previous.get("next_page") or 1)
        resume_skip = int(previous.get("next_index") or 0)
        if not 0 <= resume_board < len(boards):
            resume_board, resume_page, resume_skip = 0, 1, 0
        resume_page = max(resume_page, 1)
        resume_skip = max(resume_skip, 0)
        deepest = int(previous.get("deepest_page") or 0)
        limit = self.config.max_items
        page_limit = self.options.get("max_pages_per_board")
        page_limit = int(page_limit) if page_limit is not None else None
        emitted = 0
        self._state = {}

        for offset in range(resume_board, len(boards)):
            board = boards[offset]
            page = resume_page if offset == resume_board else 1
            skip = resume_skip if offset == resume_board else 0
            seen: set[str] = set()
            while page_limit is None or page <= page_limit:
                try:
                    response = await self.client.get(self._board_url(board, page))
                except FetchError as exc:
                    if exc.category == "permanent" and exc.status_code in {404, 410}:
                        break
                    raise
                rows = self._thread_rows(response.content)
                ids = {row["thread_id"] for row in rows}
                # Out-of-range pages answer 200 with an already-seen thread set.
                if not ids or ids <= seen:
                    break
                seen |= ids
                deepest = max(deepest, page)
                for index, row in enumerate(rows):
                    if index < skip:
                        continue
                    emitted += 1
                    yield DiscoveredItem(
                        url=row["url"],
                        identity=f"kanxue:thread:{row['thread_id']}",
                        metadata={
                            "thread_id": row["thread_id"],
                            "forum_id": board,
                            "title": row["title"],
                            "page": page,
                            "source": "kanxue",
                        },
                    )
                    if limit is not None and emitted >= limit:
                        # Resume inside the same page; the rows already yielded are
                        # durable in the job queue, so they must not be re-emitted.
                        self._state = {
                            "next_board": offset,
                            "next_page": page,
                            "next_index": index + 1,
                            "deepest_page": deepest,
                            "complete": False,
                        }
                        return
                skip = 0
                page += 1
        self._state = {
            "next_board": 0,
            "next_page": 1,
            "next_index": 0,
            "deepest_page": deepest,
            "complete": True,
        }

    def _board_url(self, board: int, page: int) -> str:
        base = str(self.options.get("base_url") or "https://bbs.kanxue.com").rstrip("/")
        if page == 1:
            return f"{base}/forum-{board}.htm"
        # Verified format; ``forum-<id>-1-<page>___.htm`` in the page markup serves
        # page 1 again and must not be used for traversal.
        return f"{base}/forum-{board}-{page}.htm"

    def _thread_rows(self, raw: bytes) -> list[dict]:
        soup = BeautifulSoup(raw, "html.parser")
        rows: list[dict] = []
        seen: set[str] = set()
        for anchor in soup.select("a[href]"):
            path = urlsplit(str(anchor.get("href"))).path
            if "thread-create" in path:
                continue
            match = THREAD_RE.search(path)
            if match is None:
                continue
            thread_id = match.group(1)
            title = anchor.get_text(" ", strip=True)
            if not title or thread_id in seen:
                # Pagination anchors such as thread-123-2.htm carry only a number.
                continue
            seen.add(thread_id)
            rows.append(
                {
                    "thread_id": thread_id,
                    "title": title,
                    "url": f"https://bbs.kanxue.com/thread-{thread_id}.htm",
                }
            )
        return rows

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
        first = soup.select_one("div.message.message_rich_type")
        metadata = {**document.metadata}
        if first is not None:
            encrypted = [node.get_text(" ", strip=True) for node in first.select("mark.encrypted")]
            for node in first.select("mark.encrypted"):
                # Keep the payload as inert text; never evaluate the decoder script.
                node.replace_with(f"\n\n```text\n{node.get_text(' ', strip=True)}\n```\n\n")
            for junk in first.select("script, style, .advert, .ad, .qrcode, .attachment-hide"):
                junk.decompose()
            content = _convert(first).strip()
            if encrypted:
                metadata["encrypted_link_blocks"] = len(encrypted)
        else:
            content = ""
        title = self._title(soup, metadata)
        metadata["has_body"] = bool(content)
        return ParsedDocument(title=title, content=content, metadata=metadata)

    @staticmethod
    def _title(soup: BeautifulSoup, metadata: dict) -> str:
        heading = soup.select_one("h1")
        if heading is not None:
            text = heading.get_text(" ", strip=True)
            if text:
                return text
        declared = metadata.get("title")
        if isinstance(declared, str) and declared.strip():
            return declared.strip()
        if soup.title is not None:
            text = soup.title.get_text(" ", strip=True)
            # "标题-板块-看雪安全社区｜专业技术交流与安全研究论坛"
            head = re.split(r"[-–—]", text, maxsplit=1)[0].strip()
            return head or "Untitled"
        return "Untitled"

    def validate(self, parsed: ParsedDocument) -> None:
        if not parsed.title.strip():
            raise ValueError("kanxue thread has no title")
        if not parsed.content.strip():
            raise ValueError("kanxue thread has no first-post body")

    def normalize(self, parsed: ParsedDocument) -> ParsedDocument:
        parsed = super().normalize(parsed)
        parsed.content = re.sub(r"\n{3,}", "\n\n", parsed.content).strip()
        return parsed
