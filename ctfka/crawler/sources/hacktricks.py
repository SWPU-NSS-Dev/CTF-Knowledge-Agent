"""HackTricks source.

HackTricks is an mdBook site. Each language publishes a complete page list at
``/<lang>/sitemap.xml``; the English book alone lists ~1,200 pages covering web,
binary exploitation, privilege escalation and AWDP-relevant hardening topics.

mdBook wraps every page in ``#body-container > #content``; the page's real body is
``#content main``. The generic body heuristic stops at the outer ``<main>`` shell
and yields almost nothing, so this adapter extracts ``#content main`` directly and
removes the book chrome (sponsor blocks, edit links, page navigation).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree

from ctfka.contracts import DiscoveredItem, FetchedDocument, ParsedDocument
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.sources.common import _as_urls, _canonical
from ctfka.ingestion.parser import _convert

DEFAULT_SEEDS = ("https://hacktricks.wiki/en/sitemap.xml",)
# mdBook chrome inside #content main that is never article text.
CHROME_SELECTORS = (
    ".sponsor",
    ".sponsors",
    ".topsponsor",
    ".topsponsor-bsa",
    ".ht-sponsor-card",
    ".advert",
    ".ad",
    "nav",
    ".page-nav",
    ".nav-wrapper",
    ".nav-chapters",
    ".mobile-nav-chapters",
    ".page-footer",
    "#page-footer",
    ".edit-link",
    ".langs",
    "script",
    "style",
)
# mdBook wraps each heading in <a class="header" href="#slug">, which the shared
# converter would otherwise render as a redundant Markdown link.
HEADING_ANCHOR_LINK = "a.header"
# Sponsor/community boilerplate blocks that appear at the top of every page.
SPONSOR_BLOCK = re.compile(
    r"(?:\n|^)(?:> )?(?:Tip|Support HackTricks|Learn & practice|HackTricks Training|"
    r"Sponsored|Join the).*?(?=\n\S|\Z)",
    re.DOTALL,
)


def _local(element) -> str:
    return str(element.tag).rsplit("}", 1)[-1].lower()


class HackTricksAdapter(SourceAdapter):
    """Sitemap-driven mdBook traversal with ``#content main`` extraction."""

    async def discover(self, cursor: dict | None = None) -> AsyncIterator[DiscoveredItem]:
        # The worker passes a cursor, but a standalone call may instead rely on
        # set_state(); both describe the same durable position.
        previous = dict(cursor or {}) or dict(self._state)
        seeds = _as_urls(
            self.options.get("sitemap_urls") or self.options.get("sitemap_url")
        ) or list(DEFAULT_SEEDS)
        include = self._regexes(self.options.get("include_regex"))
        exclude = self._regexes(self.options.get("exclude_regex"))
        allow_cross = bool(self.options.get("allow_cross_origin_sitemaps", False))
        origins = {self._origin(url) for url in seeds}
        done: set[str] = set(previous.get("done_sitemaps") or [])
        known: set[str] = set(previous.get("pages") or [])
        limit = self.config.max_items
        emitted = 0
        pending: list[str] = [_canonical(url) for url in seeds]
        self._state = {}

        def snapshot(complete: bool) -> dict:
            return {
                "done_sitemaps": sorted(done),
                "pages": sorted(known),
                "complete": complete,
            }

        while pending:
            sitemap_url = pending.pop(0)
            if sitemap_url in done:
                continue
            if not allow_cross and self._origin(sitemap_url) not in origins:
                continue
            response = await self.client.get(sitemap_url)
            done.add(sitemap_url)
            try:
                root = ElementTree.fromstring(response.content)
            except Exception as exc:
                raise ValueError(f"invalid HackTricks sitemap at {sitemap_url}: {exc}") from exc
            kind = _local(root)
            if kind == "sitemapindex":
                for node in root.iter():
                    if _local(node) != "loc" or not node.text:
                        continue
                    child = _canonical(node.text.strip())
                    if not child or child in done:
                        continue
                    if not allow_cross and self._origin(child) not in origins:
                        continue
                    if child not in pending:
                        pending.append(child)
            elif kind == "urlset":
                for node in root.iter():
                    if _local(node) != "loc" or not node.text:
                        continue
                    url = _canonical(node.text.strip())
                    if url in known:
                        continue
                    if include and not any(pattern.search(url) for pattern in include):
                        continue
                    if any(pattern.search(url) for pattern in exclude):
                        continue
                    known.add(url)
                    emitted += 1
                    yield DiscoveredItem(
                        url=url,
                        identity=f"hacktricks:{self._slug(url)}",
                        metadata={"sitemap": sitemap_url, "source": "hacktricks"},
                    )
                    if limit is not None and emitted >= limit:
                        # Partial pass: drop the sitemap marker so a later run
                        # re-traverses it instead of treating it as done.
                        done.discard(sitemap_url)
                        self._state = snapshot(False)
                        return
            else:
                raise ValueError(
                    f"HackTricks sitemap root must be urlset or sitemapindex: {sitemap_url}"
                )
            self._state = snapshot(False)
        self._state = snapshot(not pending)

    @staticmethod
    def _slug(url: str) -> str:
        path = urlsplit(url).path
        return path.lstrip("/") or "index"

    @staticmethod
    def _regexes(value: object) -> list[re.Pattern[str]]:
        values = _as_urls(value)
        try:
            return [re.compile(item) for item in values]
        except re.error as exc:
            raise ValueError(f"invalid HackTricks URL filter regex: {exc}") from exc

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int | None]:
        parsed = urlsplit(url)
        scheme = parsed.scheme.lower()
        port = parsed.port
        if port == (443 if scheme == "https" else 80):
            port = None
        return scheme, (parsed.hostname or "").lower(), port

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
        body = soup.select_one("#content main") or soup.select_one("main")
        metadata = {**document.metadata}
        if body is None:
            return ParsedDocument(
                title=self._title(soup, metadata),
                content="",
                metadata={**metadata, "has_body": False},
            )
        for selector in CHROME_SELECTORS:
            for node in body.select(selector):
                node.decompose()
        # Unwrap heading anchors so the heading text survives without a self-link.
        for anchor in body.select(HEADING_ANCHOR_LINK):
            anchor.unwrap()
        # Remove training/sponsor callouts only. ``note``/``warning`` callouts carry
        # real technical content (fixed versions, mitigations) and must be kept.
        for callout in body.select("blockquote.blockquote-tag-tip"):
            text = callout.get_text(" ", strip=True)
            if "Learn & practice" in text or "HackTricks Training" in text or "Support HackTricks" in text:
                callout.decompose()
        # The sponsor banner sits directly in the body as bare links/paragraphs.
        for anchor in body.select('a[href*="hacktricks-training.com"], a[href*="sponsors"]'):
            parent = anchor.parent
            if (
                parent is not None
                and parent.name in {"p", "div", "li"}
                and len(parent.get_text(strip=True)) < 400
            ):
                parent.decompose()
            else:
                anchor.decompose()
        content = _convert(body)
        content = SPONSOR_BLOCK.sub("", content)
        content = re.sub(r"\[\s*\]\([^)]*\)", "", content)
        content = re.sub(r"^#+\s*$\n?", "", content, flags=re.MULTILINE)
        content = re.sub(r"^>\s*$\n?", "", content, flags=re.MULTILINE)
        content = re.sub(r"\n{3,}", "\n\n", content).strip()
        return ParsedDocument(
            title=self._title(soup, metadata),
            content=content,
            metadata={**metadata, "has_body": bool(content)},
        )

    @staticmethod
    def _title(soup: BeautifulSoup, metadata: dict) -> str:
        heading = soup.select_one("#content main h1") or soup.select_one("h1")
        if heading is not None:
            # mdBook nests <a class="header"> inside the heading; use its text only.
            text = heading.get_text(" ", strip=True)
            if text:
                return text
        if soup.title is not None:
            text = soup.title.get_text(" ", strip=True)
            return re.sub(r"\s*[-–—]\s*HackTricks\s*$", "", text).strip() or "Untitled"
        return "Untitled"

    def validate(self, parsed: ParsedDocument) -> None:
        if not parsed.title.strip():
            raise ValueError("HackTricks page has no title")
        if not parsed.content.strip():
            raise ValueError("HackTricks page has no #content main body")

    def normalize(self, parsed: ParsedDocument) -> ParsedDocument:
        parsed = super().normalize(parsed)
        parsed.content = re.sub(r"\n{3,}", "\n\n", parsed.content).strip()
        return parsed

