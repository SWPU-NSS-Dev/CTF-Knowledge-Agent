"""安全客 (anquanke.com) source.

Anquanke renders article bodies server-side inside ``div.content#js-article`` but
wraps them in a full site shell (top navigation, weekly digest, related reading).
The generic HTML body heuristic keeps that chrome, so this adapter extracts the
article container directly. Discovery uses the site's published sitemap index
(``sitemap.xml`` -> up to 28 ``sitemapN.xml`` children), which is the complete
listing the site exposes; category pages only ever render their first page.

Every sitemap ``<url>`` entry carries a ``<lastmod>`` timestamp. The cursor keeps,
per sitemap, the highest ``lastmod`` already handed to the job queue, so later
passes re-yield only entries at or above that watermark (an article that is edited
gets a newer ``lastmod``). The watermark is bounded by the number of sitemaps
rather than the number of articles. Entries without a usable ``lastmod`` are
always yielded and rely on ``Store`` idempotence.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit

from bs4 import BeautifulSoup
from defusedxml import ElementTree

from ctfka.contracts import DiscoveredItem, FetchedDocument, ParsedDocument
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.http import FetchError
from ctfka.crawler.sources.common import _as_urls, _canonical
from ctfka.ingestion.parser import _convert

DEFAULT_SEEDS = ("https://www.anquanke.com/sitemap.xml",)
POST_RE = re.compile(r"/post/id/(\d+)/?$")
SITE_SUFFIX = re.compile(r"\s*[-–—|]\s*安全KER\s*[-–—|]?\s*安全资讯平台\s*$")


def _local(element) -> str:
    return str(element.tag).rsplit("}", 1)[-1].lower()


def _timestamp(value: str) -> float | None:
    """Parse a sitemap lastmod into a comparable epoch, or None if unusable.

    Comparing the raw strings would be wrong: ``2026-10-26T14:54:01+08:00`` and
    ``2026-10-26T06:54:01Z`` are the same instant but sort differently.
    """
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    if len(text) == 10:  # date-only lastmod
        text += "T00:00:00+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


class AnquankeAdapter(SourceAdapter):
    """Sitemap-driven discovery with article-container extraction."""

    async def discover(self, cursor: dict | None = None) -> AsyncIterator[DiscoveredItem]:
        # The worker passes a cursor, but a standalone call may instead rely on
        # set_state(); both describe the same durable position.
        previous = dict(cursor or {}) or dict(self._state)
        seeds = _as_urls(
            self.options.get("sitemap_urls") or self.options.get("sitemap_url")
        ) or list(DEFAULT_SEEDS)
        # ``done`` only marks children finished *within the current pass*; it is not
        # persisted, so every run re-reads the children and their lastmod watermarks
        # can surface articles that were edited since the previous run.
        done: set[str] = set()
        # Per-sitemap watermark: highest lastmod already enqueued from that child,
        # plus the post ids seen at exactly that watermark. Without the id set an
        # article whose lastmod equals the watermark would be re-yielded on every
        # pass, because the watermark advances only past the newest entry.
        watermarks: dict[str, str] = {
            str(key): str(value) for key, value in (previous.get("lastmod") or {}).items()
        }
        watermark_ids: dict[str, list[str]] = {
            str(key): [str(item) for item in value]
            for key, value in (previous.get("lastmod_ids") or {}).items()
        }
        resume_url = previous.get("resume_url")
        resume_index = int(previous.get("resume_index") or 0)
        allow_cross = bool(self.options.get("allow_cross_origin_sitemaps", False))
        origins = {self._origin(url) for url in seeds}
        limit = self.config.max_items
        emitted = 0
        seen_posts: set[str] = set()
        pending: list[str] = [_canonical(url) for url in seeds]
        # A partial pass stops inside one child; that child is visited first so the
        # saved index can be honoured instead of restarting it.
        if resume_url and resume_url not in pending:
            pending.insert(0, resume_url)
        self._state = {}

        def snapshot(is_complete: bool, resume: tuple[str, int] | None = None) -> dict:
            state: dict = {
                "lastmod": watermarks,
                "lastmod_ids": watermark_ids,
                "complete": is_complete,
            }
            if resume is not None:
                state["resume_url"], state["resume_index"] = resume[0], resume[1]
            return state

        while pending:
            sitemap_url = pending.pop(0)
            if sitemap_url in done:
                continue
            if not allow_cross and self._origin(sitemap_url) not in origins:
                continue
            try:
                response = await self.client.get(sitemap_url)
            except FetchError as exc:
                if exc.category == "permanent" and exc.status_code in {404, 410}:
                    # A retired sitemap child must not stall the whole traversal.
                    done.add(sitemap_url)
                    continue
                raise
            try:
                root = ElementTree.fromstring(response.content)
            except Exception as exc:
                raise ValueError(f"invalid anquanke sitemap at {sitemap_url}: {exc}") from exc
            kind = _local(root)
            skip_index = resume_index if sitemap_url == resume_url else 0
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
                watermark_raw = watermarks.get(sitemap_url, "")
                watermark = _timestamp(watermark_raw)
                # Ids already enqueued at exactly this watermark; lastmod has
                # one-second resolution, so equality is the common case.
                seen_at_watermark = set(watermark_ids.get(sitemap_url, []))
                highest_raw = watermark_raw
                highest = watermark
                highest_ids: set[str] = set(seen_at_watermark)
                position = -1
                for entry in self._url_entries(root):
                    position += 1
                    if position < skip_index:
                        continue
                    url, lastmod = entry
                    match = POST_RE.search(urlsplit(url).path)
                    if match is None:
                        continue
                    post_id = match.group(1)
                    lastmod_epoch = _timestamp(lastmod) if lastmod else None
                    if watermark is not None and lastmod_epoch is not None:
                        if lastmod_epoch < watermark:
                            # Older than what this child already handed to the queue.
                            continue
                        if lastmod_epoch == watermark and post_id in seen_at_watermark:
                            # Same instant and already handed over: not a new version.
                            continue
                    if post_id in seen_posts:
                        # The same article can appear in several sitemap children.
                        continue
                    seen_posts.add(post_id)
                    emitted += 1
                    if lastmod_epoch is not None and (highest is None or lastmod_epoch > highest):
                        highest = lastmod_epoch
                        highest_raw = lastmod
                        highest_ids = {post_id}
                    elif lastmod_epoch is not None and highest is not None and lastmod_epoch == highest:
                        highest_ids.add(post_id)
                    metadata = {
                        "post_id": post_id,
                        "sitemap": sitemap_url,
                        "source": "anquanke",
                    }
                    if lastmod:
                        metadata["lastmod"] = lastmod
                    yield DiscoveredItem(
                        url=url,
                        identity=f"anquanke:post:{post_id}",
                        metadata=metadata,
                    )
                    if limit is not None and emitted >= limit:
                        # Resume inside this sitemap. The watermark is intentionally
                        # left untouched so the next run re-reads the whole child and
                        # relies on resume_index, not on a premature watermark.
                        self._state = snapshot(False, (sitemap_url, position + 1))
                        return
                # Advance the watermark only after the child was read to its end in
                # this pass; a truncated pass keeps the old value so nothing is lost.
                if highest_raw:
                    watermarks[sitemap_url] = highest_raw
                    watermark_ids[sitemap_url] = sorted(highest_ids)
            else:
                raise ValueError(
                    f"anquanke sitemap root must be urlset or sitemapindex: {sitemap_url}"
                )
            done.add(sitemap_url)
            self._state = snapshot(False)

        self._state = snapshot(True)

    @staticmethod
    def _url_entries(root) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        for node in root:
            if _local(node) != "url":
                continue
            loc = ""
            lastmod = ""
            for child in node:
                tag = _local(child)
                text = (child.text or "").strip()
                if tag == "loc":
                    loc = text
                elif tag == "lastmod":
                    lastmod = text
            if loc:
                entries.append((_canonical(loc), lastmod))
        return entries

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
        body = soup.select_one("div.content#js-article") or soup.select_one("#js-article")
        if body is not None:
            # Article bodies embed tracked adverts and "related reading" blocks.
            for junk in body.select(
                "script, style, .recommend, .related, .advert, .ad, .share, .qrcode"
            ):
                junk.decompose()
        content = _convert(body).strip() if body is not None else ""
        metadata = {**document.metadata, "has_body": bool(content)}
        published = self._meta(soup, "article:published_time") or self._meta(soup, "publishdate")
        if published:
            metadata["published"] = published
        tags = [a.get_text(" ", strip=True) for a in soup.select("a[href*='/tag/']")]
        tags = [tag for tag in dict.fromkeys(tags) if tag]
        if tags:
            metadata["tags"] = tags[:20]
        if content and "登录吧" in content[:200]:
            metadata["login_gated"] = True
        return ParsedDocument(title=self._title(soup), content=content, metadata=metadata)

    @staticmethod
    def _meta(soup: BeautifulSoup, key: str) -> str | None:
        node = soup.select_one(f'meta[property="{key}"]') or soup.select_one(f'meta[name="{key}"]')
        if node is None:
            return None
        value = node.get("content")
        return str(value).strip() if value else None

    @staticmethod
    def _title(soup: BeautifulSoup) -> str:
        heading = soup.select_one("h1")
        if heading is not None:
            text = heading.get_text(" ", strip=True)
            if text:
                return text
        if soup.title is not None:
            return SITE_SUFFIX.sub("", soup.title.get_text(" ", strip=True)).strip() or "Untitled"
        return "Untitled"

    def validate(self, parsed: ParsedDocument) -> None:
        if not parsed.title.strip():
            raise ValueError("anquanke article has no title")
        if not parsed.content.strip():
            raise ValueError("anquanke article body container #js-article is empty")
        if parsed.metadata.get("login_gated"):
            raise ValueError("anquanke article requires login; refusing to store a stub")

    def normalize(self, parsed: ParsedDocument) -> ParsedDocument:
        parsed = super().normalize(parsed)
        parsed.content = re.sub(r"\n{3,}", "\n\n", parsed.content).strip()
        return parsed
