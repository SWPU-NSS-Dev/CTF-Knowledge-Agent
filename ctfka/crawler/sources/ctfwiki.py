from __future__ import annotations

from collections.abc import AsyncIterator

from ctfka.contracts import DiscoveredItem
from ctfka.crawler.sources.html import HTMLAdapter


class CTFWikiAdapter(HTMLAdapter):
    """CTF Wiki's public documentation tree, using the generic HTML crawler."""

    async def discover(self, cursor=None) -> AsyncIterator[DiscoveredItem]:
        if not self.options.get("start_urls") and not self.options.get("url"):
            self.options["start_urls"] = ["https://ctf-wiki.org/"]
        async for item in super().discover(cursor):
            yield item
