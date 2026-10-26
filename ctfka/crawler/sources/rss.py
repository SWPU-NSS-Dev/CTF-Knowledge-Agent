from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from urllib.parse import urljoin

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.sources.common import _as_urls, _canonical


class RSSAdapter(SourceAdapter):
    """RSS/Atom feed discovery with article URLs fetched as ordinary documents."""

    async def discover(self, cursor=None) -> AsyncIterator[DiscoveredItem]:
        feed_urls = _as_urls(self.options.get("feed_urls") or self.options.get("feed_url"))
        if not feed_urls:
            raise ValueError("rss adapter requires options.feed_url")
        remaining = self.config.max_items
        for feed_url in feed_urls:
            response = await self.client.get(feed_url)
            try:
                root = ET.fromstring(response.content)
            except ET.ParseError as exc:
                raise ValueError(f"invalid RSS/Atom XML at {feed_url}: {exc}") from exc
            for element in root.iter():
                tag = element.tag.rsplit("}", 1)[-1].lower()
                if tag != "item" and tag != "entry":
                    continue
                values: dict[str, str] = {}
                for child in element:
                    key = child.tag.rsplit("}", 1)[-1].lower()
                    if key == "link" and child.attrib.get("href"):
                        values[key] = child.attrib["href"]
                    elif child.text:
                        values[key] = child.text.strip()
                link = values.get("link")
                if not link:
                    continue
                url = _canonical(urljoin(feed_url, link))
                yield DiscoveredItem(
                    url=url,
                    identity=url,
                    metadata={
                        k: values[k] for k in ("title", "published", "updated") if k in values
                    },
                )
                if remaining is not None:
                    remaining -= 1
                    if remaining <= 0:
                        return

    async def fetch(self, item: DiscoveredItem) -> FetchedDocument:
        response = await self.client.get(item.url)
        return FetchedDocument(
            str(response.url),
            response.content,
            response.headers.get("Content-Type", "text/html").split(";", 1)[0],
            dict(item.metadata),
        )
