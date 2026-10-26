from __future__ import annotations

import asyncio
import re
import tempfile
from collections import deque
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ctfka.contracts import DiscoveredItem, FetchedDocument
from ctfka.crawler.adapters import SourceAdapter
from ctfka.crawler.http import FetchError
from ctfka.crawler.sources.common import _as_urls, _canonical


class HTMLAdapter(SourceAdapter):
    """A configurable same-origin HTML link crawler."""

    async def discover(self, cursor=None) -> AsyncIterator[DiscoveredItem]:
        starts = _as_urls(self.options.get("start_urls") or self.options.get("url"))
        if not starts:
            raise ValueError("html adapter requires options.start_urls")
        raw_depth = self.options.get("max_depth")
        max_depth = int(raw_depth) if raw_depth is not None else None
        if max_depth is not None and max_depth < 0:
            raise ValueError("max_depth must be non-negative")
        allowed_hosts = set(self.options.get("allowed_hosts", []))
        if not allowed_hosts:
            allowed_hosts = {urlsplit(u).hostname for u in starts if urlsplit(u).hostname}
        limit = self.config.max_items
        pending = deque((_canonical(url), 0) for url in starts)
        seen: set[str] = set()
        if not hasattr(self, "_discovery_cache"):
            self._cache_dir = tempfile.TemporaryDirectory(prefix="ctfka-html-")
            self._discovery_cache: dict[str, tuple[str, str, str]] = {}
            self._cached_bytes = 0
            self._cache_counter = 0
        cache_budget = int(self.options.get("discovery_cache_bytes", 134217728))
        while pending and (limit is None or len(seen) < limit):
            url, depth = pending.popleft()
            host = urlsplit(url).hostname
            if url in seen or host not in allowed_hosts:
                continue
            seen.add(url)
            yield DiscoveredItem(url=url, identity=url, metadata={"depth": depth})
            if max_depth is not None and depth >= max_depth:
                continue
            try:
                response = await self.client.get(url)
            except FetchError:
                # Discovery keeps the failed URL queued; fetch will record its durable retry.
                continue
            if "html" not in response.headers.get("Content-Type", "").lower():
                continue
            if self._cached_bytes + len(response.content) <= cache_budget:
                cache_path = f"{self._cache_dir.name}/{self._cache_counter}.body"
                self._cache_counter += 1
                await asyncio.to_thread(Path(cache_path).write_bytes, response.content)
                self._discovery_cache[url] = (
                    cache_path,
                    response.headers.get("Content-Type", "text/html").split(";", 1)[0],
                    str(response.url),
                )
                self._cached_bytes += len(response.content)
            for match in re.finditer(r"(?is)<a\b[^>]*?href\s*=\s*(['\"])(.*?)\1", response.text):
                target = _canonical(urljoin(str(response.url), match.group(2).strip()))
                if urlsplit(target).hostname in allowed_hosts and target not in seen:
                    pending.append((target, depth + 1))

    async def fetch(self, item: DiscoveredItem) -> FetchedDocument:
        cache = getattr(self, "_discovery_cache", {}).pop(item.url, None)
        if cache is not None:
            cache_path, content_type, final_url = cache
            try:
                body = await asyncio.to_thread(Path(cache_path).read_bytes)
                return FetchedDocument(final_url, body, content_type, dict(item.metadata))
            except OSError:
                pass
        response = await self.client.get(item.url)
        content_type = response.headers.get("Content-Type", "text/html").split(";", 1)[0]
        return FetchedDocument(
            str(response.url), response.content, content_type, dict(item.metadata)
        )

    def close(self) -> None:
        cache_dir = getattr(self, "_cache_dir", None)
        if cache_dir is not None:
            cache_dir.cleanup()
            self._cache_dir = None
