"""Generic sitemap source.

Many sites publish no paginated listing but a complete, machine-readable index:
``/sitemap.xml`` (a ``sitemapindex``) pointing at ``urlset`` children. For those
sites the generic ``html`` adapter cannot be used, because there is nothing to
crawl from — discovery has to start from the sitemap rather than by following
links out of a page.

This adapter is deliberately *not* site-specific. It only owns discovery; the
article body is parsed with the shared parser, so a site whose body needs
site-specific extraction should still get its own adapter (as HackTricks and
Anquanke do) even when its discovery is sitemap-driven.

Incremental behaviour
---------------------
Every ``<url>`` may carry a ``<lastmod>``. The cursor keeps, per sitemap child,
the highest ``lastmod`` already handed to the job queue plus the URLs seen at
exactly that instant. A later pass re-reads each child and re-yields only entries
at or above that watermark, so an article that is edited (newer ``lastmod``) is
picked up again while unchanged articles are not re-queued. The watermark is
bounded by the number of sitemaps, not by the number of articles. Entries with no
usable ``lastmod`` are always yielded and rely on ``Store`` idempotence.

The watermark only advances after a child was read to its end in this pass. A
pass truncated by ``max_items`` therefore never moves it, and records
``resume_url`` / ``resume_index`` so the next run continues inside the same child
instead of restarting it. Such a pass is reported as ``complete: False`` and,
like every non-GitHub adapter here, must not claim ``complete_scan`` — that flag
makes the worker archive every article the current pass did not enumerate.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import urlsplit

from defusedxml import ElementTree

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.http import FetchError
from ctfka.crawler.sources.common import _as_urls, _canonical


def _local(element) -> str:
    return str(element.tag).rsplit("}", 1)[-1].lower()


def _timestamp(value: str) -> float | None:
    """Parse a sitemap lastmod into a comparable epoch, or None if unusable.

    Comparing the raw strings would be wrong: ``2026-10-26T14:54:01+08:00`` and
    ``2026-10-26T06:54:01Z`` are the same instant but sort differently, and a
    date-only lastmod is not comparable with a full timestamp at all.
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


class SitemapAdapter(SourceAdapter):
    """Sitemap/sitemapindex traversal with a resumable lastmod cursor."""

    async def discover(self, cursor: dict | None = None) -> AsyncIterator[DiscoveredItem]:
        # The worker passes a cursor, but a standalone call may instead rely on
        # set_state(); both describe the same durable position.
        previous = dict(cursor or {}) or dict(self._state)
        seeds = _as_urls(
            self.options.get("sitemap_urls")
            or self.options.get("sitemap_url")
            or self.options.get("start_urls")
        )
        if not seeds:
            raise ValueError("sitemap adapter requires options.sitemap_urls")
        include = self._regexes(self.options.get("include_regex"), "include")
        exclude = self._regexes(self.options.get("exclude_regex"), "exclude")
        allow_cross = bool(self.options.get("allow_cross_origin_sitemaps", False))
        origins = {self._origin(url) for url in seeds}
        # ``done`` only marks children finished *within the current pass*; it is not
        # persisted, so every run re-reads the children and their lastmod watermarks
        # can surface articles that were edited since the previous run.
        done: set[str] = set()
        # Per-sitemap watermark: highest lastmod already enqueued from that child,
        # plus the URLs seen at exactly that watermark. Without the URL set an entry
        # whose lastmod equals the watermark would be re-yielded on every pass,
        # because the watermark advances only past the newest entry.
        watermarks: dict[str, str] = {
            str(key): str(value) for key, value in (previous.get("lastmod") or {}).items()
        }
        watermark_urls: dict[str, list[str]] = {
            str(key): [str(item) for item in value]
            for key, value in (previous.get("lastmod_urls") or {}).items()
        }
        resume_url = previous.get("resume_url")
        resume_index = int(previous.get("resume_index") or 0)
        limit = self.config.max_items
        emitted = 0
        seen: set[str] = set()
        pending: list[str] = [_canonical(url) for url in seeds]
        # A partial pass stops inside one child; that child is visited first so the
        # saved index can be honoured instead of restarting it.
        if resume_url and resume_url not in pending:
            pending.insert(0, resume_url)
        self._state = {}

        def snapshot(is_complete: bool, resume: tuple[str, int] | None = None) -> dict:
            state: dict = {
                "lastmod": watermarks,
                "lastmod_urls": watermark_urls,
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
                # Transient failures are the worker's business; never fake success.
                raise
            try:
                root = ElementTree.fromstring(response.content)
            except Exception as exc:
                raise ValueError(f"invalid sitemap at {sitemap_url}: {exc}") from exc
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
                watermark = _timestamp(watermarks.get(sitemap_url, ""))
                # URLs already enqueued at exactly this watermark; lastmod has
                # one-second resolution, so equality is the common case.
                seen_at_watermark = set(watermark_urls.get(sitemap_url, []))
                highest_raw = watermarks.get(sitemap_url, "")
                highest = watermark
                highest_urls: set[str] = set(seen_at_watermark)
                position = -1
                for entry in self._url_entries(root):
                    position += 1
                    # The watermark covers every entry in the child, including ones the
                    # URL filters reject: they are outside this source's selection, and
                    # holding the watermark back on their account would stall the pass.
                    url, lastmod = entry
                    lastmod_epoch = _timestamp(lastmod) if lastmod else None
                    if lastmod_epoch is not None and (highest is None or lastmod_epoch > highest):
                        highest = lastmod_epoch
                        highest_raw = lastmod
                        highest_urls = {url}
                    elif (
                        lastmod_epoch is not None
                        and highest is not None
                        and lastmod_epoch == highest
                    ):
                        highest_urls.add(url)
                    if position < skip_index:
                        continue
                    if watermark is not None and lastmod_epoch is not None:
                        if lastmod_epoch < watermark:
                            # Older than what this child already handed to the queue.
                            continue
                        if lastmod_epoch == watermark and url in seen_at_watermark:
                            # Same instant and already handed over: not a new version.
                            continue
                    if url in seen:
                        # The same page can appear in several sitemap children.
                        continue
                    if include and not any(pattern.search(url) for pattern in include):
                        continue
                    if any(pattern.search(url) for pattern in exclude):
                        continue
                    seen.add(url)
                    emitted += 1
                    metadata: dict = {"sitemap": sitemap_url, "source": "sitemap"}
                    if lastmod:
                        metadata["lastmod"] = lastmod
                    yield DiscoveredItem(url=url, identity=url, metadata=metadata)
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
                    watermark_urls[sitemap_url] = sorted(highest_urls)
            else:
                raise ValueError(
                    f"sitemap root must be urlset or sitemapindex: {sitemap_url}"
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
    def _regexes(value: object, kind: str) -> list[re.Pattern[str]]:
        values = _as_urls(value)
        try:
            return [re.compile(item) for item in values]
        except re.error as exc:
            raise ValueError(f"invalid sitemap {kind} URL filter regex: {exc}") from exc

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
